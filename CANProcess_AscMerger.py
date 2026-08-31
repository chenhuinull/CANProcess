"""GUI tool for merging Vector ASC log files."""

from __future__ import annotations

import os
import re
import sys
import threading
import ctypes
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from tkinter import Tk, Listbox, Text, Toplevel, END, DISABLED, NORMAL
from tkinter import filedialog, messagebox, ttk
from ctypes import wintypes
from uuid import UUID

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DRAG_DROP_AVAILABLE = True
except ImportError:
    DRAG_DROP_AVAILABLE = False


DATE_RE = re.compile(
    rb"^date\s+(Sun|Mon|Tue|Wed|Thu|Fri|Sat)\s+"
    rb"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    rb"(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+(\d{4})\s*$"
)
TIMESTAMP_RE = re.compile(rb"^(\s*)(\d+(?:\.\d+)?)(\s+)(.*)$")
MONTHS = {name: number for number, name in enumerate(
    (b"Jan", b"Feb", b"Mar", b"Apr", b"May", b"Jun", b"Jul", b"Aug", b"Sep", b"Oct", b"Nov", b"Dec"), 1
)}
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

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


def application_directory() -> Path:
    """Return the directory containing this script or its frozen executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def create_output_directory(root: Path | None = None) -> Path:
    """Create a unique out/AscMerger_YYYYmmdd_HHMMSS directory for one merge run."""
    output_root = root if root is not None else application_directory() / "out"
    output_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = output_root / f"AscMerger_{timestamp}"
    suffix = 1
    while destination.exists():
        destination = output_root / f"AscMerger_{timestamp}_{suffix:02d}"
        suffix += 1
    destination.mkdir()
    return destination


class GUID(ctypes.Structure):
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
    """Open the native Windows folder picker with Ctrl/Shift multi-select support."""
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
            return []  # User cancelled the dialog.
        if com_method(dialog, 27, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(dialog, ctypes.byref(results)) < 0:
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


@dataclass(frozen=True)
class AscFile:
    path: Path
    start_time: datetime


def parse_asc_start(path: Path) -> datetime | None:
    """Return the date from the first ASC header line, or None if malformed."""
    try:
        with path.open("rb") as source:
            line = source.readline().rstrip(b"\r\n")
    except OSError:
        return None

    match = DATE_RE.match(line)
    if not match:
        return None
    _weekday, month, day, hour, minute, second, year = match.groups()
    try:
        return datetime(
            int(year), MONTHS[month], int(day), int(hour), int(minute), int(second)
        )
    except ValueError:
        return None


def collect_asc_files(selections: list[Path]) -> list[Path]:
    files: dict[Path, Path] = {}
    for selection in selections:
        if selection.is_file() and selection.suffix.lower() == ".asc":
            files[selection.resolve()] = selection
        elif selection.is_dir():
            for item in selection.rglob("*"):
                if item.is_file() and item.suffix.lower() == ".asc":
                    files[item.resolve()] = item
    return sorted(files.values(), key=lambda item: str(item).lower())


def format_asc_date(value: datetime) -> bytes:
    return (
        f"date {WEEKDAYS[value.weekday()]} {MONTH_NAMES[value.month - 1]} "
        f"{value.day:2d} {value:%H:%M:%S} {value.year:04d}\n"
    ).encode("ascii")


def find_time_overlaps(ranges: list[tuple[datetime, datetime, Path]]) -> list[str]:
    """Return a readable report of overlapping recording intervals."""
    overlaps: list[str] = []
    latest_end: datetime | None = None
    latest_path: Path | None = None
    for begin, end, path in sorted(ranges, key=lambda item: (item[0], str(item[2]).lower())):
        if latest_end is not None and begin < latest_end:
            overlaps.append(
                f"{path.name} overlaps {latest_path.name}: "
                f"{begin:%Y-%m-%d %H:%M:%S} to {min(end, latest_end):%Y-%m-%d %H:%M:%S}"
            )
        if latest_end is None or end > latest_end:
            latest_end, latest_path = end, path
    return overlaps


def merge_asc_files(
    selections: list[Path],
    output: Path,
    progress: Callable[[float], None] | None = None,
) -> tuple[int, int, list[str], list[str]]:
    candidates = collect_asc_files(selections)
    if not candidates:
        raise ValueError("No .asc files were found.")

    valid: list[AscFile] = []
    skipped: list[str] = []
    for path in candidates:
        start_time = parse_asc_start(path)
        if start_time is None:
            skipped.append(f"{path.name} (invalid date header)")
        elif start_time.year == 1970:
            skipped.append(f"{path.name} (timestamp is in 1970)")
        else:
            valid.append(AscFile(path, start_time))

    if not valid:
        raise ValueError("No valid ASC files are available to merge.")

    valid.sort(key=lambda item: (item.start_time, str(item.path).lower()))
    first_time = valid[0].start_time

    total_size = sum(item.path.stat().st_size for item in valid) or 1
    done_size = 0

    ranges: list[tuple[datetime, datetime, Path]] = []
    with output.open("wb", buffering=4 * 1024 * 1024) as destination:
        destination.write(format_asc_date(first_time))
        destination.write(b"base hex timestamps absolute\n")

        for item in valid:
            offset = (item.start_time - first_time).total_seconds()
            first_timestamp: float | None = None
            last_timestamp: float | None = None
            file_size = item.path.stat().st_size or 0
            start_fraction = done_size / total_size
            end_fraction = (done_size + file_size) / total_size
            bytes_read = 0
            next_report = 0
            with item.path.open("rb", buffering=4 * 1024 * 1024) as source:
                source.readline()  # date header
                source.readline()  # base/timestamp header
                for line in source:
                    bytes_read += len(line)
                    if progress is not None and bytes_read >= next_report:
                        next_report = bytes_read + 1024 * 1024
                        fraction = min(bytes_read / file_size, 1.0)
                        progress(start_fraction + fraction * (end_fraction - start_fraction))
                    body = line.rstrip(b"\r\n")
                    match = TIMESTAMP_RE.match(body)
                    if match:
                        indent, timestamp, separator, rest = match.groups()
                        raw_timestamp = float(timestamp)
                        first_timestamp = raw_timestamp if first_timestamp is None else min(first_timestamp, raw_timestamp)
                        last_timestamp = raw_timestamp if last_timestamp is None else max(last_timestamp, raw_timestamp)
                        adjusted = raw_timestamp + offset
                        destination.write(indent + f"{adjusted:.6f}".encode("ascii") + separator + rest + b"\n")
                    elif body:
                        destination.write(body + b"\n")

            if first_timestamp is not None and last_timestamp is not None:
                ranges.append((
                    item.start_time + timedelta(seconds=first_timestamp),
                    item.start_time + timedelta(seconds=last_timestamp),
                    item.path,
                ))
            done_size += file_size

    if progress is not None:
        progress(1.0)
    return len(valid), len(candidates), skipped, find_time_overlaps(ranges)


class App:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("ASC 日志合并工具")
        self.root.geometry("900x720")
        self.root.minsize(720, 560)
        self.sources: list[Path] = []
        self.last_output_dir: Path | None = None

        style = ttk.Style()
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Subtitle.TLabel", foreground="#5b6472")
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"))

        frame = ttk.Frame(root, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=3)
        frame.rowconfigure(4, weight=1)

        source_box = ttk.LabelFrame(frame, text=" 1. 数据来源 ", padding=12)
        source_box.grid(row=1, column=0, sticky="nsew")
        source_box.columnconfigure(0, weight=1)
        source_box.rowconfigure(1, weight=1)
        action_bar = ttk.Frame(source_box)
        action_bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Button(action_bar, text="添加 ASC 文件", command=self.choose_files).pack(side="left")
        ttk.Button(action_bar, text="添加文件夹（可多选）", command=self.choose_folder).pack(side="left", padx=(8, 0))
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
            ttk.Label(source_box, text="提示：也可以将 ASC 文件或文件夹直接拖到上方列表中。", style="Subtitle.TLabel").grid(row=2, column=0, sticky="w", pady=(8, 0))

        operation_box = ttk.LabelFrame(frame, text=" 2. 检查与合并 ", padding=12)
        operation_box.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        operation_box.columnconfigure(0, weight=1)
        ttk.Label(
            operation_box,
            text="输出位置：out\\AscMerger_YYYYMMDD_HHMMSS\\AscMerger_时间戳.asc\n合并前可查看实际扫描到的 ASC 文件，合并完成后会显示无效文件与时间重叠检查结果。",
            justify="left",
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        button_bar = ttk.Frame(operation_box)
        button_bar.grid(row=1, column=0, columnspan=4, sticky="e")
        ttk.Button(button_bar, text="查看 ASC 文件", width=13, command=self.show_detected_files).pack(side="left", padx=(0, 8))
        ttk.Button(button_bar, text="打开输出文件夹", width=14, command=self.open_output_folder).pack(side="left", padx=(0, 8))
        self.merge_button = ttk.Button(button_bar, text="合并并保存", width=12, command=self.start_merge, style="Primary.TButton")
        self.merge_button.pack(side="left")

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

    def choose_folder(self) -> None:
        try:
            paths = select_multiple_folders(self.root.winfo_id())
        except OSError as error:
            messagebox.showerror("ASC 日志合并工具", f"无法打开多选文件夹窗口：{error}")
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
        indexes = list(self.source_list.curselection())
        for index in reversed(indexes):
            del self.sources[index]
            self.source_list.delete(index)

    def clear_sources(self) -> None:
        self.sources.clear()
        self.source_list.delete(0, END)

    def show_detected_files(self) -> None:
        if not self.sources:
            messagebox.showwarning("ASC 日志合并工具", "请先添加至少一个 ASC 文件或文件夹。")
            return
        files = collect_asc_files(self.sources)
        dialog = Toplevel(self.root)
        dialog.title("检测到的 ASC 文件")
        dialog.geometry("900x520")
        dialog.minsize(600, 320)
        container = ttk.Frame(dialog, padding=12)
        container.pack(fill="both", expand=True)
        ttk.Label(container, text=f"共检测到 {len(files)} 个不重复的 ASC 文件：").pack(anchor="w", pady=(0, 8))
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
        """Open the latest run's output folder, or the shared out folder."""
        output_dir = self.last_output_dir or (application_directory() / "out")
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(output_dir)  # type: ignore[attr-defined]  # Windows-only application.
        except OSError as error:
            messagebox.showerror("ASC 日志合并工具", f"无法打开输出文件夹：{error}")

    def start_merge(self) -> None:
        if not self.sources:
            messagebox.showwarning("ASC 日志合并工具", "请先添加至少一个 ASC 文件或文件夹。")
            return
        try:
            output_dir = create_output_directory()
        except OSError as error:
            messagebox.showerror("ASC 日志合并工具", f"无法创建输出文件夹：{error}")
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = output_dir / f"AscMerger_{timestamp}.asc"
        self.last_output_dir = output_dir
        self.merge_button.configure(state=DISABLED)
        self.progress.configure(value=0)
        self.set_status("正在合并文件，请稍候……")
        threading.Thread(target=self.merge_worker, args=(self.sources.copy(), output), daemon=True).start()

    def merge_worker(self, sources: list[Path], output: Path) -> None:
        def report(fraction: float) -> None:
            self.root.after(0, self.update_progress, fraction)

        try:
            valid, total, skipped, overlaps = merge_asc_files(sources, output, progress=report)
            message = f"合并完成：已合并 {valid}/{total} 个 ASC 文件。\n输出文件：{output}"
            if skipped:
                message += "\n已跳过：\n" + "\n".join(skipped)
            if overlaps:
                message += "\n发现时间重叠：\n" + "\n".join(overlaps)
            else:
                message += "\n时间重叠检查：未发现重叠。"
            self.root.after(0, self.merge_finished, message, True)
        except Exception as error:  # Display user-readable failure in the GUI.
            self.root.after(0, self.merge_finished, f"合并失败：{error}", False)

    def merge_finished(self, message: str, succeeded: bool) -> None:
        self.progress.configure(value=100 if succeeded else 0)
        self.set_status(message)
        self.merge_button.configure(state=NORMAL)
        if not succeeded:
            messagebox.showerror("ASC 日志合并工具", message)


if __name__ == "__main__":
    window = TkinterDnD.Tk() if DRAG_DROP_AVAILABLE else Tk()
    App(window)
    window.mainloop()
