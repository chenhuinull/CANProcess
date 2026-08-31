#!/usr/bin/env python3
"""Replace ISO-TP-like 8-byte CAN messages in a Vector ASC file with CAN FD.

The supported first frame is ``10 LL`` and consecutive frames are ``2N``.
The result is one 64-byte CAN FD line at the first-frame timestamp.  Bytes past
the announced length are discarded and unused FD payload bytes are filled with
AA.  All original classic-CAN 0x6F4 records are removed from the result:
complete transfers and short single frames (where byte 0 is the payload length)
are replaced by CAN FD, while incomplete/orphan records are discarded.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from tkinter import DISABLED, END, NORMAL, Listbox, StringVar, Text, Tk, Toplevel
from tkinter import filedialog, messagebox, ttk
from ctypes import wintypes
from uuid import UUID

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DRAG_DROP_AVAILABLE = True
except ImportError:
    DRAG_DROP_AVAILABLE = False


DEFAULT_TARGET_ID = 0x6F4
RECORD = re.compile(
    r"^(?P<time>\s*\d+(?:\.\d+)?)\s+CAN\s+(?P<channel>\S+)\s+"
    r"(?P<can_id>\S+)\s+(?P<direction>Rx|Tx)\s+(?P<tail>.*)$",
    re.IGNORECASE,
)

FOS_FORCEFILESYSTEM = 0x00000040
FOS_ALLOWMULTISELECT = 0x00000200
FOS_PICKFOLDERS = 0x00000020
SIGDN_FILESYSPATH = 0x80058000
CLSCTX_INPROC_SERVER = 1


def center_window(window: Tk) -> None:
    """Place a top-level window in the center of the current screen."""
    window.update_idletasks()
    width, height = window.winfo_width(), window.winfo_height()
    x = max((window.winfo_screenwidth() - width) // 2, 0)
    y = max((window.winfo_screenheight() - height) // 2, 0)
    window.geometry(f"{width}x{height}+{x}+{y}")


@dataclass(frozen=True)
class CanRecord:
    timestamp: str
    can_id_text: str
    direction: str
    data: bytes


def parse_record(line: str, target_id: int = DEFAULT_TARGET_ID) -> CanRecord | None:
    """Parse the conventional Vector ASC CAN line used by this converter."""
    match = RECORD.match(line.rstrip("\r\n"))
    if not match:
        return None
    try:
        if int(match["can_id"], 16) != target_id:
            return None
        fields = match["tail"].split()
        data_marker = next(i for i, field in enumerate(fields) if field.lower() == "d")
        byte_count = int(fields[data_marker + 2], 10)
        data = bytes(int(value, 16) for value in fields[data_marker + 3 : data_marker + 3 + byte_count])
        if len(data) != byte_count:
            return None
    except (ValueError, StopIteration, IndexError):
        return None
    return CanRecord(match["time"].strip(), match["can_id"], match["direction"], data)


def fd_line(record: CanRecord, payload: bytes, fd_channel: str) -> str:
    values = " ".join(f"{value:02x}" for value in payload)
    # In Vector ASC, CAN FD DLC 15 denotes the 64-byte payload length.
    return f"{record.timestamp} CANFD {fd_channel} {record.can_id_text} {record.direction} 0 0 d 15 64 {values}\n"


def is_short_single_frame(record: CanRecord) -> bool:
    """A classic 8-byte frame whose first byte gives 0--7 payload bytes."""
    return len(record.data) == 8 and 0 <= record.data[0] <= 7


def write_short_single_frame(handle, record: CanRecord, fd_channel: str) -> None:
    length = record.data[0]
    handle.write(fd_line(record, record.data[1 : 1 + length].ljust(64, b"\xAA"), fd_channel))


def convert(lines: list[str], fd_channel: str, target_id: int = DEFAULT_TARGET_ID) -> tuple[list[str], int]:
    records = [parse_record(line, target_id) for line in lines]
    replacements: dict[int, str] = {}
    removed: set[int] = set()
    converted = 0

    for first_index, first in enumerate(records):
        if first is None or len(first.data) != 8 or first.data[0] != 0x10 or first_index in removed:
            continue

        expected_length = first.data[1]
        payload = bytearray(first.data[2:])
        frame_indexes = [first_index]
        expected_sequence = 1

        # Traffic on other IDs may be interleaved, so only inspect matching 6F4 frames.
        for index in range(first_index + 1, len(records)):
            candidate = records[index]
            if candidate is None:
                continue
            if len(candidate.data) != 8:
                break
            frame_type = candidate.data[0] >> 4
            if frame_type == 1:  # A new first frame starts a different transfer.
                break
            if frame_type != 2 or (candidate.data[0] & 0x0F) != expected_sequence:
                break
            frame_indexes.append(index)
            payload.extend(candidate.data[1:])
            expected_sequence = (expected_sequence + 1) & 0x0F

        if expected_length > 64 or len(payload) < expected_length:
            continue  # Incomplete or not representable as the requested 64-byte FD frame.

        fd_payload = bytes(payload[:expected_length]).ljust(64, b"\xAA")
        replacements[first_index] = fd_line(first, fd_payload, fd_channel)
        removed.update(frame_indexes)
        converted += 1

    output: list[str] = []
    for index, line in enumerate(lines):
        if index in replacements:
            output.append(replacements[index])
        if index not in removed:
            output.append(line)
    return output, converted


def convert_file(
    source: Path,
    destination: Path,
    fd_channel: str,
    target_id: int = DEFAULT_TARGET_ID,
    progress: Callable[[float], None] | None = None,
) -> tuple[int, int]:
    """Convert incrementally, so large ASC captures do not need to fit in memory.

    Interleaved traffic on other CAN IDs is retained.  It is held only until
    the current 6F4 transfer terminates, then emitted after the replacement FD
    frame, preserving the requested first-frame slot.

    ``progress``, when given, is called with this file's 0.0-1.0 completion
    fraction as it is read.
    """
    converted = 0
    discarded = 0
    active: CanRecord | None = None
    payload = bytearray()
    expected_length = 0
    expected_sequence = 1
    held_lines: list[str] = []
    retained_lines: list[str] = []

    def begin(record: CanRecord, line: str) -> None:
        nonlocal active, payload, expected_length, expected_sequence, held_lines, retained_lines
        active = record
        payload = bytearray(record.data[2:])
        expected_length = record.data[1]
        expected_sequence = 1
        held_lines = [line]
        retained_lines = []

    def flush_unmodified(handle) -> None:
        nonlocal active, held_lines, retained_lines
        handle.writelines(held_lines)
        active = None
        held_lines = []
        retained_lines = []

    def flush_converted(handle) -> None:
        nonlocal active, held_lines, retained_lines, converted
        if active is not None and expected_length <= 64 and len(payload) >= expected_length:
            handle.write(fd_line(active, bytes(payload[:expected_length]).ljust(64, b"\xAA"), fd_channel))
            handle.writelines(retained_lines)
            converted += 1
        else:
            handle.writelines(held_lines)
        active = None
        held_lines = []
        retained_lines = []

    def discard_incomplete(handle) -> None:
        """Drop the source 6F4 frames but retain traffic interleaved with them."""
        nonlocal active, held_lines, retained_lines, discarded
        handle.writelines(retained_lines)
        discarded += 1
        active = None
        held_lines = []
        retained_lines = []

    # newline="" retains the input's newline bytes for lines that are preserved.
    file_size = source.stat().st_size or 1
    bytes_read = 0
    next_report = 0
    with source.open("r", encoding="utf-8-sig", newline="") as input_file, destination.open(
        "w", encoding="utf-8", newline=""
    ) as output_file:
        for line in input_file:
            bytes_read += len(line)
            if progress is not None and bytes_read >= next_report:
                next_report = bytes_read + 256 * 1024
                progress(min(bytes_read / file_size, 1.0))
            record = parse_record(line, target_id)
            if active is None:
                if record is not None and len(record.data) == 8 and record.data[0] == 0x10:
                    begin(record, line)
                elif record is not None and is_short_single_frame(record):
                    write_short_single_frame(output_file, record, fd_channel)
                    converted += 1
                elif record is not None:
                    # Output must contain only the generated CAN FD 6F4 frames;
                    # discard orphan continuations and non-ISO-TP short frames too.
                    discarded += 1
                else:
                    output_file.write(line)
                continue

            # A correct continuation is always consumed.  This also removes a
            # final redundant CF (such as 27 in the supplied sample).
            is_expected_cf = (
                record is not None
                and len(record.data) == 8
                and record.data[0] >> 4 == 2
                and (record.data[0] & 0x0F) == expected_sequence
            )
            if is_expected_cf:
                held_lines.append(line)
                payload.extend(record.data[1:])
                expected_sequence = (expected_sequence + 1) & 0x0F
                continue

            # A non-6F4 line is traffic interleaved with this ISO-TP transfer.
            if record is None:
                held_lines.append(line)
                retained_lines.append(line)
                continue

            if len(payload) >= expected_length:
                flush_converted(output_file)
            else:
                discard_incomplete(output_file)

            # This 6F4 frame ended the preceding transfer.  It can immediately
            # begin a new one, otherwise it remains unchanged.
            if len(record.data) == 8 and record.data[0] == 0x10:
                begin(record, line)
            elif is_short_single_frame(record):
                write_short_single_frame(output_file, record, fd_channel)
                converted += 1
            else:
                # Any remaining classic CAN 6F4 record is intentionally removed.
                discarded += 1

        if active is not None:
            if len(payload) >= expected_length:
                flush_converted(output_file)
            else:
                discard_incomplete(output_file)
    if progress is not None:
        progress(1.0)
    return converted, discarded


class GUID(ctypes.Structure):
    """Windows GUID structure, used by the native multi-folder picker."""

    _fields_ = (("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8))

    @classmethod
    def from_string(cls, value: str) -> "GUID":
        return cls.from_buffer_copy(UUID(value).bytes_le)


def com_method(instance: ctypes.c_void_p, index: int, restype: object, *argtypes: object) -> object:
    vtable = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])


def release_com(instance: ctypes.c_void_p | None) -> None:
    if instance:
        com_method(instance, 2, wintypes.ULONG)(instance)


def select_multiple_folders(owner_handle: int) -> list[Path]:
    """Open the native Windows folder dialog with Ctrl/Shift multi-selection."""
    ole32 = ctypes.windll.ole32
    initialized = ole32.CoInitialize(None) >= 0
    dialog = ctypes.c_void_p()
    results = ctypes.c_void_p()
    try:
        clsid = GUID.from_string("{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}")
        iid = GUID.from_string("{D57C7288-D4AD-4768-BE02-9D969532D960}")
        create = ole32.CoCreateInstance
        create.argtypes = (ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
        create.restype = ctypes.c_long
        if create(ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER, ctypes.byref(iid), ctypes.byref(dialog)) < 0:
            raise OSError("Unable to open the Windows folder picker.")

        options = wintypes.DWORD()
        com_method(dialog, 10, ctypes.c_long, ctypes.POINTER(wintypes.DWORD))(dialog, ctypes.byref(options))
        flags = options.value | FOS_FORCEFILESYSTEM | FOS_ALLOWMULTISELECT | FOS_PICKFOLDERS
        if com_method(dialog, 9, ctypes.c_long, wintypes.DWORD)(dialog, flags) < 0:
            raise OSError("Unable to configure the Windows folder picker.")
        if com_method(dialog, 3, ctypes.c_long, wintypes.HWND)(dialog, owner_handle) < 0:
            return []

        if com_method(dialog, 20, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(dialog, ctypes.byref(results)) < 0:
            return []
        count = wintypes.DWORD()
        com_method(results, 7, ctypes.c_long, ctypes.POINTER(wintypes.DWORD))(results, ctypes.byref(count))
        paths: list[Path] = []
        for index in range(count.value):
            item = ctypes.c_void_p()
            com_method(results, 8, ctypes.c_long, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(results, index, ctypes.byref(item))
            name = ctypes.c_wchar_p()
            if item and com_method(item, 5, ctypes.c_long, wintypes.DWORD, ctypes.POINTER(ctypes.c_wchar_p))(item, SIGDN_FILESYSPATH, ctypes.byref(name)) >= 0:
                paths.append(Path(name.value))
                ole32.CoTaskMemFree(name)
            release_com(item)
        return paths
    finally:
        release_com(results)
        release_com(dialog)
        if initialized:
            ole32.CoUninitialize()


def collect_asc_files(selections: list[Path]) -> list[Path]:
    """Recursively collect unique ASC files from selected files and folders."""
    files: dict[Path, Path] = {}
    for selection in selections:
        if selection.is_file() and selection.suffix.lower() == ".asc":
            files[selection.resolve()] = selection
        elif selection.is_dir():
            for item in selection.rglob("*"):
                if item.is_file() and item.suffix.lower() == ".asc":
                    files[item.resolve()] = item
    return sorted(files.values(), key=lambda item: str(item).lower())


def application_directory() -> Path:
    """Keep the out directory beside the script, or beside the frozen EXE."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def create_output_directory(root: Path | None = None) -> Path:
    """Create a unique out/AscCAN2FD_YYYYmmdd_HHMMSS directory for one conversion run."""
    output_root = root if root is not None else application_directory() / "out"
    output_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = output_root / f"AscCAN2FD_{timestamp}"
    suffix = 1
    while destination.exists():
        destination = output_root / f"AscCAN2FD_{timestamp}_{suffix:02d}"
        suffix += 1
    destination.mkdir()
    return destination


def destination_for(source: Path, output_dir: Path, used_names: set[str]) -> Path:
    """Return a non-conflicting '<source>_CANFD.asc' path inside output_dir."""
    stem = f"{source.stem}_CANFD"
    candidate = output_dir / f"{stem}.asc"
    index = 2
    while candidate.name.casefold() in used_names:
        candidate = output_dir / f"{stem}_{index}.asc"
        index += 1
    used_names.add(candidate.name.casefold())
    return candidate


def parse_target_can_id(value: str) -> int:
    """Parse a standard or extended CAN ID entered as 6F4, 0x6F4, or 6F4x."""
    normalized = value.strip().lower()
    if normalized.startswith("0x"):
        normalized = normalized[2:]
    if normalized.endswith("x"):
        normalized = normalized[:-1]
    try:
        can_id = int(normalized, 16)
    except ValueError as error:
        raise ValueError("输入 CAN ID 必须是十六进制，例如 6F4 或 0x6F4。") from error
    if not 0 <= can_id <= 0x1FFFFFFF:
        raise ValueError("输入 CAN ID 必须位于 0 至 1FFFFFFF 之间。")
    return can_id


def validate_fd_channel(value: str) -> str:
    """Validate the CAN FD channel field because it is written directly to ASC."""
    channel = value.strip()
    if not channel or any(character.isspace() for character in channel):
        raise ValueError("输出 CAN FD 通道不能为空，且不能包含空格。")
    return channel


def convert_sources(
    selections: list[Path],
    output_dir: Path,
    fd_channel: str,
    target_id: int = DEFAULT_TARGET_ID,
    progress: Callable[[float], None] | None = None,
) -> tuple[int, int, int, list[str]]:
    """Convert every discovered ASC file and return file/count/error statistics.

    ``progress``, when given, is called with the overall 0.0-1.0 completion
    fraction, weighted by the input files' sizes.
    """
    sources = collect_asc_files(selections)
    if not sources:
        raise ValueError("No .asc files were found in the selected files or folders.")

    converted_files = 0
    transfers = 0
    discarded = 0
    errors: list[str] = []
    used_names: set[str] = set()
    total_size = sum(source.stat().st_size for source in sources) or 1
    done_size = 0
    for source in sources:
        destination = destination_for(source, output_dir, used_names)
        size = source.stat().st_size or 0
        start_fraction = done_size / total_size
        end_fraction = (done_size + size) / total_size

        def file_progress(fraction: float, start: float = start_fraction, end: float = end_fraction) -> None:
            if progress is not None:
                progress(start + fraction * (end - start))

        try:
            file_transfers, file_discarded = convert_file(source, destination, fd_channel, target_id, progress=file_progress)
            converted_files += 1
            transfers += file_transfers
            discarded += file_discarded
        except (OSError, UnicodeError) as error:
            errors.append(f"{source}: {error}")
        done_size += size
    return converted_files, transfers, discarded, errors


class ConverterApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("CAN 转 CAN FD 工具")
        self.root.geometry("900x720")
        self.root.minsize(720, 560)
        self.sources: list[Path] = []
        self.last_output_dir: Path | None = None
        self.target_can_id = StringVar(value=f"{DEFAULT_TARGET_ID:X}")
        self.fd_channel = StringVar(value="10")

        style = ttk.Style()
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Subtitle.TLabel", foreground="#5b6472")
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"))

        frame = ttk.Frame(root, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=3)
        frame.rowconfigure(4, weight=1)

        source_box = ttk.LabelFrame(frame, text=" 1. 输入文件和文件夹 ", padding=12)
        source_box.grid(row=1, column=0, sticky="nsew")
        source_box.columnconfigure(0, weight=1)
        source_box.rowconfigure(1, weight=1)
        action_bar = ttk.Frame(source_box)
        action_bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Button(action_bar, text="添加 ASC 文件", command=self.choose_files).pack(side="left")
        ttk.Button(action_bar, text="添加文件夹（可多选）", command=self.choose_folders).pack(side="left", padx=(8, 0))
        ttk.Button(action_bar, text="移除选中项", command=self.remove_selected).pack(side="left", padx=(8, 0))
        ttk.Button(action_bar, text="清空", command=self.clear_sources).pack(side="right")

        list_frame = ttk.Frame(source_box)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1, minsize=180)
        self.source_list = Listbox(list_frame, height=14, selectmode="extended", activestyle="none")
        self.source_list.grid(row=0, column=0, sticky="nsew")
        vertical_bar = ttk.Scrollbar(list_frame, orient="vertical", command=self.source_list.yview)
        vertical_bar.grid(row=0, column=1, sticky="ns")
        horizontal_bar = ttk.Scrollbar(list_frame, orient="horizontal", command=self.source_list.xview)
        horizontal_bar.grid(row=1, column=0, sticky="ew")
        self.source_list.configure(yscrollcommand=vertical_bar.set, xscrollcommand=horizontal_bar.set)
        if DRAG_DROP_AVAILABLE:
            self.source_list.drop_target_register(DND_FILES)
            self.source_list.dnd_bind("<<Drop>>", self.drop_sources)
            ttk.Label(source_box, text="提示：也可以将多个 ASC 文件或文件夹直接拖到上方列表中。", style="Subtitle.TLabel").grid(row=2, column=0, sticky="w", pady=(8, 0))

        operation_box = ttk.LabelFrame(frame, text=" 2. 转换 ", padding=12)
        operation_box.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        operation_box.columnconfigure(0, weight=1)
        settings_bar = ttk.Frame(operation_box)
        settings_bar.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        ttk.Label(settings_bar, text="输入 CAN ID（十六进制）：").pack(side="left")
        ttk.Entry(settings_bar, width=14, textvariable=self.target_can_id).pack(side="left", padx=(4, 18))
        ttk.Label(settings_bar, text="输出 CAN FD 通道：").pack(side="left")
        ttk.Entry(settings_bar, width=12, textvariable=self.fd_channel).pack(side="left", padx=(4, 0))
        ttk.Label(
            operation_box,
            text="输出位置：out\\AscCAN2FD_YYYYMMDD_HHMMSS\n每个输入 ASC 文件都会生成一个单独的转换结果。",
            justify="left",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 8))
        button_bar = ttk.Frame(operation_box)
        button_bar.grid(row=2, column=0, columnspan=4, sticky="e")
        ttk.Button(button_bar, text="查看 ASC 文件", width=13, command=self.show_detected_files).pack(side="left", padx=(0, 8))
        ttk.Button(button_bar, text="打开输出文件夹", width=14, command=self.open_output_folder).pack(side="left", padx=(0, 8))
        self.convert_button = ttk.Button(button_bar, text="开始批量转换", width=13, command=self.start_conversion, style="Primary.TButton")
        self.convert_button.pack(side="left")

        status_box = ttk.LabelFrame(frame, text=" 运行状态 ", padding=10)
        status_box.grid(row=4, column=0, sticky="nsew", pady=(14, 0))
        self.progress = ttk.Progressbar(status_box, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(0, 8))
        self.status = Text(status_box, height=6, wrap="word", state=DISABLED, relief="flat", background="#f7f8fa")
        self.status.pack(fill="both", expand=True)
        self.set_status("准备就绪：请先添加 ASC 文件或文件夹。")
        center_window(self.root)

    def update_progress(self, fraction: float) -> None:
        self.progress.configure(value=max(0.0, min(fraction, 1.0)) * 100)

    def set_status(self, text: str) -> None:
        self.status.configure(state=NORMAL)
        self.status.delete("1.0", END)
        self.status.insert(END, text)
        self.status.configure(state=DISABLED)

    def add_sources(self, paths: list[str]) -> None:
        known = {path.resolve() for path in self.sources}
        for raw_path in paths:
            path = Path(raw_path)
            if not (path.is_dir() or (path.is_file() and path.suffix.lower() == ".asc")):
                continue
            if path.resolve() not in known:
                self.sources.append(path)
                known.add(path.resolve())
                self.source_list.insert(END, str(path))

    def choose_files(self) -> None:
        paths = filedialog.askopenfilenames(title="选择 ASC 文件", filetypes=[("ASC 文件", "*.asc")])
        self.add_sources(list(paths))

    def choose_folders(self) -> None:
        try:
            paths = select_multiple_folders(self.root.winfo_id())
        except OSError as error:
            messagebox.showerror("CAN 转 CAN FD 工具", f"无法打开多选文件夹窗口：{error}")
            return
        self.add_sources([str(path) for path in paths])

    def drop_sources(self, event: object) -> str:
        raw_paths = list(self.root.tk.splitlist(event.data))
        before = len(self.sources)
        self.add_sources(raw_paths)
        added = len(self.sources) - before
        if added:
            self.set_status(f"已通过拖放添加 {added} 个来源。")
        return "break"

    def remove_selected(self) -> None:
        for index in reversed(self.source_list.curselection()):
            del self.sources[index]
            self.source_list.delete(index)

    def clear_sources(self) -> None:
        self.sources.clear()
        self.source_list.delete(0, END)

    def show_detected_files(self) -> None:
        files = collect_asc_files(self.sources)
        if not files:
            messagebox.showwarning("CAN 转 CAN FD 工具", "当前来源中未检测到 ASC 文件。")
            return
        dialog = Toplevel(self.root)
        dialog.title("检测到的 ASC 文件")
        dialog.geometry("900x520")
        container = ttk.Frame(dialog, padding=12)
        container.pack(fill="both", expand=True)
        ttk.Label(container, text=f"共检测到 {len(files)} 个不重复的 ASC 文件：").pack(anchor="w", pady=(0, 8))
        file_list = Listbox(container, selectmode="extended")
        file_list.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=file_list.yview)
        scrollbar.pack(side="right", fill="y")
        file_list.configure(yscrollcommand=scrollbar.set)
        for path in files:
            file_list.insert(END, str(path))

    def open_output_folder(self) -> None:
        """Open the latest run's output folder, or the shared out folder."""
        output_dir = self.last_output_dir or (application_directory() / "out")
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(output_dir)  # type: ignore[attr-defined]  # Windows-only application.
        except OSError as error:
            messagebox.showerror("CAN 转 CAN FD 工具", f"无法打开输出文件夹：{error}")

    def start_conversion(self) -> None:
        if not self.sources:
            messagebox.showwarning("CAN 转 CAN FD 工具", "请先添加至少一个 ASC 文件或文件夹。")
            return
        files = collect_asc_files(self.sources)
        if not files:
            messagebox.showwarning("CAN 转 CAN FD 工具", "所选来源中未检测到 ASC 文件。")
            return
        try:
            target_id = parse_target_can_id(self.target_can_id.get())
            fd_channel = validate_fd_channel(self.fd_channel.get())
        except ValueError as error:
            messagebox.showwarning("CAN 转 CAN FD 工具", str(error))
            return
        try:
            output_dir = create_output_directory()
        except OSError as error:
            messagebox.showerror("CAN 转 CAN FD 工具", f"无法创建输出文件夹：{error}")
            return
        self.last_output_dir = output_dir
        self.convert_button.configure(state=DISABLED)
        self.progress.configure(value=0)
        self.set_status(f"正在转换 {len(files)} 个 ASC 文件（输入 ID：{target_id:X}，输出通道：{fd_channel}），请稍候……")
        threading.Thread(target=self.convert_worker, args=(self.sources.copy(), output_dir, fd_channel, target_id), daemon=True).start()

    def convert_worker(self, sources: list[Path], output_dir: Path, fd_channel: str, target_id: int) -> None:
        def report(fraction: float) -> None:
            self.root.after(0, self.update_progress, fraction)

        try:
            files, transfers, discarded, errors = convert_sources(sources, output_dir, fd_channel, target_id, progress=report)
            message = f"转换完成：已处理 {files} 个文件、{transfers} 组输入 ID {target_id:X} 的传输数据；已移除 {discarded} 个不完整或孤立报文。\n输出目录：{output_dir}"
            if errors:
                message += "\n转换失败的文件：\n" + "\n".join(errors)
            self.root.after(0, self.conversion_finished, message, not errors)
        except Exception as error:
            self.root.after(0, self.conversion_finished, f"转换失败：{error}", False)

    def conversion_finished(self, message: str, succeeded: bool) -> None:
        self.progress.configure(value=100 if succeeded else 0)
        self.set_status(message)
        self.convert_button.configure(state=NORMAL)
        (messagebox.showinfo if succeeded else messagebox.showerror)("CAN 转 CAN FD 工具", message)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert selected 8-byte CAN frames into CAN FD records.")
    parser.add_argument("selections", nargs="*", type=Path, help="ASC files and/or folders to convert; omit to open the GUI")
    parser.add_argument("--fd-channel", default="10", help="CAN FD channel field to write (default: 10)")
    parser.add_argument("--can-id", default=f"{DEFAULT_TARGET_ID:X}", help="input CAN ID in hexadecimal (default: 6F4)")
    parser.add_argument("--out-root", type=Path, help="parent folder for timestamped output folders (default: app/out)")
    args = parser.parse_args()

    if args.selections:
        target_id = parse_target_can_id(args.can_id)
        fd_channel = validate_fd_channel(args.fd_channel)
        output_dir = create_output_directory(args.out_root)
        files, transfers, discarded, errors = convert_sources(args.selections, output_dir, fd_channel, target_id)
        print(f"Converted {files} file(s), {transfers} CAN ID {target_id:X} transfer(s); removed {discarded} incomplete/orphan item(s).")
        print(f"Output: {output_dir}")
        if errors:
            print("Failed files:\n" + "\n".join(errors), file=sys.stderr)
            raise SystemExit(1)
        return

    window = TkinterDnD.Tk() if DRAG_DROP_AVAILABLE else Tk()
    ConverterApp(window)
    window.mainloop()


if __name__ == "__main__":
    main()
