"""Replace ISO-TP-like 8-byte classic CAN messages in a Vector BLF file with CAN FD.

The supported first frame is ``10 LL`` and consecutive frames are ``2N``.
The result is one 64-byte CAN FD message at the first-frame timestamp.  Bytes past
the announced length are discarded and unused FD payload bytes are filled with
AA.  All original classic-CAN records on the target ID are removed from the
result: complete transfers and short single frames (where byte 0 is the payload
length) are replaced by CAN FD, while incomplete/orphan records are discarded.
Interleaved traffic on other IDs is retained.
"""

from __future__ import annotations

import argparse
import ctypes
import os
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
    import can
except ImportError:  # pragma: no cover - reported in the GUI/CLI below.
    can = None  # type: ignore[assignment]

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DRAG_DROP_AVAILABLE = True
except ImportError:
    DRAG_DROP_AVAILABLE = False


DEFAULT_TARGET_ID = 0x6F4
DEFAULT_FD_CHANNEL = "10"

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


def application_directory() -> Path:
    """Keep the out directory beside the script, or beside the frozen EXE."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def create_output_directory(root: Path | None = None) -> Path:
    """Create a unique BlfCAN2FD_YYYYmmdd_HHMMSS directory for one conversion run."""
    output_root = root if root is not None else application_directory()
    output_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = output_root / f"BlfCAN2FD_{timestamp}"
    suffix = 1
    while destination.exists():
        destination = output_root / f"BlfCAN2FD_{timestamp}_{suffix:02d}"
        suffix += 1
    destination.mkdir()
    return destination


def collect_blf_files(selections: list[Path]) -> list[Path]:
    """Recursively collect unique BLF files from selected files and folders."""
    files: dict[Path, Path] = {}
    for selection in selections:
        if selection.is_file() and selection.suffix.lower() == ".blf":
            files[selection.resolve()] = selection
        elif selection.is_dir():
            for item in selection.rglob("*"):
                if item.is_file() and item.suffix.lower() == ".blf":
                    files[item.resolve()] = item
    return sorted(files.values(), key=lambda item: str(item).lower())


def destination_for(source: Path, output_dir: Path, used_names: set[str]) -> Path:
    """Return a non-conflicting '<source>_CANFD.blf' path inside output_dir."""
    stem = f"{source.stem}_CANFD"
    candidate = output_dir / f"{stem}.blf"
    index = 2
    while candidate.name.casefold() in used_names:
        candidate = output_dir / f"{stem}_{index}.blf"
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


def parse_fd_channel(value: str) -> int:
    """Parse the CAN FD channel field (python-can numbering, 0-based)."""
    channel = value.strip()
    if not channel:
        raise ValueError("输出 CAN FD 通道不能为空。")
    try:
        return int(channel)
    except ValueError as error:
        raise ValueError("输出 CAN FD 通道必须是整数。") from error


def is_target_message(message: can.Message, target_id: int, source_channel: int | None) -> bool:
    """A classic-CAN 8-byte data frame on the target ID (FD/remote/error excluded)."""
    if message.is_fd or message.is_remote_frame or message.is_error_frame:
        return False
    if message.arbitration_id != target_id:
        return False
    if source_channel is not None and message.channel != source_channel:
        return False
    return len(message.data) == 8


def make_fd_message(first: can.Message, fd_payload: bytes, fd_channel: int) -> can.Message:
    """Build the 64-byte CAN FD replacement for a complete ISO-TP transfer."""
    return can.Message(
        timestamp=first.timestamp,
        arbitration_id=first.arbitration_id,
        is_extended_id=first.is_extended_id,
        is_fd=True,
        bitrate_switch=False,
        error_state_indicator=False,
        is_rx=first.is_rx,
        channel=fd_channel,
        dlc=64,
        data=fd_payload,
    )


def scan_blf_count(path: Path) -> int:
    """Return the number of messages in one BLF file (used for progress)."""
    reader = can.BLFReader(str(path))
    count = 0
    try:
        for _ in reader:
            count += 1
    finally:
        reader.stop()
    return count


def detect_source_channel(sources: list[Path], target_id: int) -> int | None:
    """Return the first source channel carrying ``target_id``, or None."""
    for source in sources:
        reader = can.BLFReader(str(source))
        try:
            for message in reader:
                if (
                    not message.is_fd
                    and not message.is_remote_frame
                    and not message.is_error_frame
                    and message.arbitration_id == target_id
                    and len(message.data) == 8
                ):
                    return message.channel
        finally:
            reader.stop()
    return None


def convert_file(
    source: Path,
    destination: Path,
    fd_channel: int,
    target_id: int = DEFAULT_TARGET_ID,
    source_channel: int | None = None,
    message_total: int = 0,
    progress: Callable[[float], None] | None = None,
) -> tuple[int, int]:
    """Convert one BLF incrementally, so large captures stream through memory.

    Interleaved traffic on other CAN IDs is retained.  A complete ISO-TP
    transfer on the target ID is replaced by one CAN FD message at the
    first-frame timestamp; a final redundant consecutive frame (e.g. 27 in the
    supplied sample) is consumed and removed.  Transfers longer than 64 bytes
    cannot be represented as one FD frame and are kept unchanged.
    """
    converted = 0
    discarded = 0
    total = max(message_total, 1)
    done = 0
    first: can.Message | None = None
    pending: list[can.Message] = []
    keep: list[can.Message] = []
    payload = bytearray()
    expected_length = 0
    expected_sequence = 1

    def begin(message: can.Message) -> None:
        nonlocal first, pending, keep, payload, expected_length, expected_sequence
        first = message
        pending = [message]
        keep = []
        payload = bytearray(message.data[2:])
        expected_length = message.data[1]
        expected_sequence = 1

    reader = can.BLFReader(str(source))
    writer = can.BLFWriter(str(destination))
    try:
        for message in reader:
            if message.timestamp is not None:
                done += 1
                if progress is not None and done % 5000 == 0:
                    progress(min(done / total, 1.0))
            target = is_target_message(message, target_id, source_channel)

            if first is None:
                if target and message.data[0] == 0x10:
                    begin(message)
                elif target and message.data[0] <= 7:
                    length = message.data[0]
                    writer.on_message_received(make_fd_message(
                        message, bytes(message.data[1 : 1 + length]).ljust(64, b"\xAA"), fd_channel
                    ))
                    converted += 1
                elif target:
                    discarded += 1
                else:
                    writer.on_message_received(message)
                continue

            # Inside an active transfer.
            is_expected_cf = (
                target
                and (message.data[0] >> 4) == 2
                and (message.data[0] & 0x0F) == expected_sequence
            )
            if is_expected_cf:
                pending.append(message)
                payload.extend(message.data[1:])
                expected_sequence = (expected_sequence + 1) & 0x0F
                continue

            # Traffic on other IDs (or non-data target frames) is interleaved
            # with this transfer: hold it in order, retain it, keep going.
            if not target:
                pending.append(message)
                keep.append(message)
                continue

            # A target-ID frame ended the preceding transfer.
            if len(payload) >= expected_length and expected_length <= 64:
                writer.on_message_received(make_fd_message(
                    first, bytes(payload[:expected_length]).ljust(64, b"\xAA"), fd_channel
                ))
                converted += 1
                for kept in keep:
                    writer.on_message_received(kept)
            elif len(payload) >= expected_length:
                # Longer than 64 bytes: keep the original frames unchanged.
                for kept in pending:
                    writer.on_message_received(kept)
            else:
                discarded += 1
                for kept in keep:
                    writer.on_message_received(kept)
            first = None
            pending = []
            keep = []

            # The ending message may itself start a new transfer.
            if target and message.data[0] == 0x10:
                begin(message)
            elif target and message.data[0] <= 7:
                length = message.data[0]
                writer.on_message_received(make_fd_message(
                    message, bytes(message.data[1 : 1 + length]).ljust(64, b"\xAA"), fd_channel
                ))
                converted += 1
            elif target:
                discarded += 1
            else:
                writer.on_message_received(message)
            continue

        # A transfer may still be open at the end of the stream.
        if first is not None:
            if len(payload) >= expected_length and expected_length <= 64:
                writer.on_message_received(make_fd_message(
                    first, bytes(payload[:expected_length]).ljust(64, b"\xAA"), fd_channel
                ))
                converted += 1
                for kept in keep:
                    writer.on_message_received(kept)
            elif len(payload) >= expected_length:
                for kept in pending:
                    writer.on_message_received(kept)
            else:
                discarded += 1
                for kept in keep:
                    writer.on_message_received(kept)
    finally:
        writer.stop()
        reader.stop()
    if progress is not None:
        progress(1.0)
    return converted, discarded


def convert_sources(
    selections: list[Path],
    output_dir: Path,
    fd_channel: int,
    target_id: int = DEFAULT_TARGET_ID,
    source_channel: int | None = None,
    progress: Callable[[float], None] | None = None,
) -> tuple[int, int, int, list[str]]:
    """Convert every discovered BLF file and return file/count/error statistics."""
    if can is None:
        raise RuntimeError("缺少 python-can 库，请先运行：pip install python-can")

    sources = collect_blf_files(selections)
    if not sources:
        raise ValueError("所选文件或文件夹中未找到 .blf 文件。")

    used_names: set[str] = set()
    jobs: list[tuple[Path, Path, int]] = []
    errors: list[str] = []
    for source in sources:
        destination = destination_for(source, output_dir, used_names)
        try:
            jobs.append((source, destination, scan_blf_count(source)))
        except Exception as error:
            errors.append(f"{source}: {error}")

    total_messages = sum(count for _, _, count in jobs) or 1
    done_messages = 0
    converted_files = 0
    transfers = 0
    discarded = 0

    for source, destination, count in jobs:
        size_fraction = count / total_messages

        def file_progress(fraction: float, size: float = size_fraction) -> None:
            if progress is not None:
                progress(min(done_messages / total_messages + fraction * size, 1.0))

        try:
            file_transfers, file_discarded = convert_file(
                source, destination, fd_channel, target_id, source_channel,
                message_total=count, progress=file_progress,
            )
            converted_files += 1
            transfers += file_transfers
            discarded += file_discarded
        except Exception as error:
            errors.append(f"{source}: {error}")
        done_messages += count
    if progress is not None:
        progress(1.0)
    return converted_files, transfers, discarded, errors


class ConverterApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("CAN 转 CAN FD 工具（BLF）")
        self.root.geometry("900x680")
        self.root.minsize(720, 540)
        self.sources: list[Path] = []
        self.last_output_dir: Path | None = None
        self.target_can_id = StringVar(value=f"{DEFAULT_TARGET_ID:X}")
        self.fd_channel = StringVar(value=DEFAULT_FD_CHANNEL)

        style = ttk.Style()
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Subtitle.TLabel", foreground="#5b6472")
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Thick.Horizontal.TProgressbar", thickness=20)

        frame = ttk.Frame(root, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=2)
        frame.rowconfigure(4, weight=1)

        source_box = ttk.LabelFrame(frame, text=" 1. 输入文件和文件夹 ", padding=12)
        source_box.grid(row=1, column=0, sticky="nsew")
        source_box.columnconfigure(0, weight=1)
        source_box.rowconfigure(1, weight=1)
        action_bar = ttk.Frame(source_box)
        action_bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Button(action_bar, text="添加 BLF 文件", command=self.choose_files).pack(side="left")
        ttk.Button(action_bar, text="添加文件夹（可多选）", command=self.choose_folders).pack(side="left", padx=(8, 0))
        ttk.Button(action_bar, text="移除选中项", command=self.remove_selected).pack(side="left", padx=(8, 0))
        ttk.Button(action_bar, text="清空", command=self.clear_sources).pack(side="right")

        list_frame = ttk.Frame(source_box)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1, minsize=80)
        self.source_list = Listbox(list_frame, height=7, selectmode="extended", activestyle="none")
        self.source_list.grid(row=0, column=0, sticky="nsew")
        vertical_bar = ttk.Scrollbar(list_frame, orient="vertical", command=self.source_list.yview)
        vertical_bar.grid(row=0, column=1, sticky="ns")
        horizontal_bar = ttk.Scrollbar(list_frame, orient="horizontal", command=self.source_list.xview)
        horizontal_bar.grid(row=1, column=0, sticky="ew")
        self.source_list.configure(yscrollcommand=vertical_bar.set, xscrollcommand=horizontal_bar.set)
        if DRAG_DROP_AVAILABLE:
            self.source_list.drop_target_register(DND_FILES)
            self.source_list.dnd_bind("<<Drop>>", self.drop_sources)
            ttk.Label(source_box, text="提示：也可以将多个 BLF 文件或文件夹直接拖到上方列表中。", style="Subtitle.TLabel").grid(row=2, column=0, sticky="w", pady=(8, 0))

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
            text="输出位置：工具所在目录\\BlfCAN2FD_YYYYMMDD_HHMMSS\n每个输入 BLF 文件都会生成一个单独的转换结果。\n说明：CAN FD 通道按 python-can 编号（0 起）；转换结果仅在下方运行状态区域显示。",
            justify="left",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 8))
        button_bar = ttk.Frame(operation_box)
        button_bar.grid(row=2, column=0, columnspan=4, sticky="e")
        ttk.Button(button_bar, text="查看 BLF 文件", width=13, command=self.show_detected_files).pack(side="left", padx=(0, 8))
        ttk.Button(button_bar, text="打开输出文件夹", width=14, command=self.open_output_folder).pack(side="left", padx=(0, 8))
        self.convert_button = ttk.Button(button_bar, text="开始批量转换", width=13, command=self.start_conversion, style="Primary.TButton")
        self.convert_button.pack(side="left")

        status_box = ttk.LabelFrame(frame, text=" 运行状态 ", padding=10)
        status_box.grid(row=4, column=0, sticky="nsew", pady=(14, 0))
        self.status = Text(status_box, height=6, wrap="word", state=DISABLED, relief="flat", background="#f7f8fa")
        self.status.pack(fill="both", expand=True)
        self.progress = ttk.Progressbar(status_box, mode="determinate", maximum=100, style="Thick.Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(8, 0))
        if can is None:
            self.set_status("缺少 python-can 库，无法转换。请先运行：pip install python-can")
        else:
            self.set_status("准备就绪：请先添加 BLF 文件或文件夹。")
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
            if not (path.is_dir() or (path.is_file() and path.suffix.lower() == ".blf")):
                continue
            if path.resolve() not in known:
                self.sources.append(path)
                known.add(path.resolve())
                self.source_list.insert(END, str(path))

    def choose_files(self) -> None:
        paths = filedialog.askopenfilenames(title="选择 BLF 文件", filetypes=[("BLF 文件", "*.blf")])
        self.add_sources(list(paths))

    def choose_folders(self) -> None:
        try:
            paths = select_multiple_folders(self.root.winfo_id())
        except OSError as error:
            messagebox.showerror("CAN 转 CAN FD 工具（BLF）", f"无法打开多选文件夹窗口：{error}")
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
        files = collect_blf_files(self.sources)
        if not files:
            messagebox.showwarning("CAN 转 CAN FD 工具（BLF）", "当前来源中未检测到 BLF 文件。")
            return
        dialog = Toplevel(self.root)
        dialog.title("检测到的 BLF 文件")
        dialog.geometry("900x520")
        container = ttk.Frame(dialog, padding=12)
        container.pack(fill="both", expand=True)
        ttk.Label(container, text=f"共检测到 {len(files)} 个不重复的 BLF 文件：").pack(anchor="w", pady=(0, 8))
        list_frame = ttk.Frame(container)
        list_frame.pack(fill="both", expand=True)
        file_list = Listbox(list_frame, selectmode="extended")
        file_list.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=file_list.yview)
        scrollbar.pack(side="right", fill="y")
        file_list.configure(yscrollcommand=scrollbar.set)
        for path in files:
            file_list.insert(END, str(path))
        ttk.Button(container, text="关闭", command=dialog.destroy).pack(anchor="e", pady=(10, 0))

    def open_output_folder(self) -> None:
        """Open the latest run's output folder, or the tool's root folder."""
        output_dir = self.last_output_dir or application_directory()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(output_dir)  # type: ignore[attr-defined]  # Windows-only application.
        except OSError as error:
            messagebox.showerror("CAN 转 CAN FD 工具（BLF）", f"无法打开输出文件夹：{error}")

    def start_conversion(self) -> None:
        if can is None:
            messagebox.showerror("CAN 转 CAN FD 工具（BLF）", "缺少 python-can 库，请先运行：pip install python-can")
            return
        if not self.sources:
            messagebox.showwarning("CAN 转 CAN FD 工具（BLF）", "请先添加至少一个 BLF 文件或文件夹。")
            return
        files = collect_blf_files(self.sources)
        if not files:
            messagebox.showwarning("CAN 转 CAN FD 工具（BLF）", "所选来源中未检测到 BLF 文件。")
            return
        try:
            target_id = parse_target_can_id(self.target_can_id.get())
            fd_channel = parse_fd_channel(self.fd_channel.get())
        except ValueError as error:
            messagebox.showwarning("CAN 转 CAN FD 工具（BLF）", str(error))
            return
        source_channel = detect_source_channel(files, target_id)
        try:
            output_dir = create_output_directory()
        except OSError as error:
            messagebox.showerror("CAN 转 CAN FD 工具（BLF）", f"无法创建输出文件夹：{error}")
            return
        self.last_output_dir = output_dir
        self.convert_button.configure(state=DISABLED)
        self.progress.configure(value=0)
        self.set_status(f"正在转换 {len(files)} 个 BLF 文件（输入 ID：{target_id:X}，输出通道：{fd_channel}），请稍候……")
        threading.Thread(target=self.convert_worker, args=(self.sources.copy(), output_dir, fd_channel, target_id, source_channel), daemon=True).start()

    def convert_worker(self, sources: list[Path], output_dir: Path, fd_channel: int, target_id: int, source_channel: int | None) -> None:
        def report(fraction: float) -> None:
            self.root.after(0, self.update_progress, fraction)

        try:
            files, transfers, discarded, errors = convert_sources(sources, output_dir, fd_channel, target_id, source_channel, progress=report)
            message = f"转换完成：已处理 {files} 个文件、{transfers} 组输入 ID {target_id:X} 的传输数据；已移除 {discarded} 个不完整或孤立报文。"
            if source_channel is not None:
                message += f"\n源通道：{source_channel}"
            message += f"\n输出目录：{output_dir}"
            if errors:
                message += "\n转换失败的文件：\n" + "\n".join(errors)
            self.root.after(0, self.conversion_finished, message, not errors)
        except Exception as error:
            self.root.after(0, self.conversion_finished, f"转换失败：{error}", False)

    def conversion_finished(self, message: str, succeeded: bool) -> None:
        self.progress.configure(value=100 if succeeded else 0)
        self.set_status(message)
        self.convert_button.configure(state=NORMAL)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert selected 8-byte classic CAN frames in BLF files into CAN FD records.")
    parser.add_argument("selections", nargs="*", type=Path, help="BLF files and/or folders to convert; omit to open the GUI")
    parser.add_argument("--fd-channel", default=DEFAULT_FD_CHANNEL, help="CAN FD channel field to write (default: 10)")
    parser.add_argument("--can-id", default=f"{DEFAULT_TARGET_ID:X}", help="input CAN ID in hexadecimal (default: 6F4)")
    parser.add_argument("--out-root", type=Path, help="parent folder for timestamped output folders (default: app root)")
    args = parser.parse_args()

    if can is None:
        print("Missing dependency python-can. Install it with: pip install python-can", file=sys.stderr)
        raise SystemExit(1)

    if args.selections:
        target_id = parse_target_can_id(args.can_id)
        fd_channel = parse_fd_channel(args.fd_channel)
        source_channel = detect_source_channel(collect_blf_files(args.selections), target_id)
        output_dir = create_output_directory(args.out_root)
        files, transfers, discarded, errors = convert_sources(args.selections, output_dir, fd_channel, target_id, source_channel)
        print(f"Converted {files} file(s), {transfers} CAN ID {target_id:X} transfer(s); removed {discarded} incomplete/orphan item(s).")
        if source_channel is not None:
            print(f"Source channel: {source_channel}")
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
