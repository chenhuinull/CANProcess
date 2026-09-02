#!/usr/bin/env python3
"""Convert Vector BLF and ASC CAN log files into each other.

Every selected ``.blf`` / ``.asc`` file (or any file found recursively inside
selected folders) is converted into the opposite format.  Classic CAN, CAN FD,
extended IDs, remote/error frames and channel assignments are preserved through
python-can.  Timestamps stay on an absolute time base, so the recording date is
kept.  Results are written to a timestamped ``out`` folder.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import sys
import threading
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


BLF_SUFFIX = ".blf"
ASC_SUFFIX = ".asc"
SUPPORTED_SUFFIXES = (BLF_SUFFIX, ASC_SUFFIX)

DIRECTION_AUTO = "自动（BLF→ASC，ASC→BLF）"
DIRECTION_BLF_TO_ASC = "BLF 转 ASC"
DIRECTION_ASC_TO_BLF = "ASC 转 BLF"
DIRECTIONS = (DIRECTION_AUTO, DIRECTION_BLF_TO_ASC, DIRECTION_ASC_TO_BLF)

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
    """Create a unique BlfAscConvert_YYYYmmdd_HHMMSS run directory beside the tool."""
    output_root = root if root is not None else application_directory()
    output_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = output_root / f"BlfAscConvert_{timestamp}"
    suffix = 1
    while destination.exists():
        destination = output_root / f"BlfAscConvert_{timestamp}_{suffix:02d}"
        suffix += 1
    destination.mkdir()
    return destination


def collect_log_files(selections: list[Path], direction: str = DIRECTION_AUTO) -> list[Path]:
    """Recursively collect unique BLF/ASC files from selected files and folders."""
    if direction == DIRECTION_BLF_TO_ASC:
        wanted = (BLF_SUFFIX,)
    elif direction == DIRECTION_ASC_TO_BLF:
        wanted = (ASC_SUFFIX,)
    else:
        wanted = SUPPORTED_SUFFIXES

    files: dict[Path, Path] = {}
    for selection in selections:
        if selection.is_file() and selection.suffix.lower() in wanted:
            files[selection.resolve()] = selection
        elif selection.is_dir():
            for item in selection.rglob("*"):
                if item.is_file() and item.suffix.lower() in wanted:
                    files[item.resolve()] = item
    return sorted(files.values(), key=lambda item: str(item).lower())


def destination_for(source: Path, output_dir: Path, used_names: set[str]) -> Path:
    """Return a non-conflicting converted path inside output_dir."""
    if source.suffix.lower() == BLF_SUFFIX:
        candidate = output_dir / f"{source.stem}.asc"
    else:
        candidate = output_dir / f"{source.stem}.blf"
    index = 2
    while candidate.name.casefold() in used_names:
        candidate = output_dir / f"{source.stem}_{index}{candidate.suffix}"
        index += 1
    used_names.add(candidate.name.casefold())
    return candidate


def fix_asc_date_header(path: Path, recording_timestamp: float) -> None:
    """Replace python-can ASCWriter's datetime.now() date header with the recording date."""
    recording_date = datetime.fromtimestamp(recording_timestamp)
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    msec = recording_date.microsecond // 1000
    line = (
        f"date {weekdays[recording_date.weekday()]} {months[recording_date.month - 1]} "
        f"{recording_date.day:2d} {recording_date:%H:%M:%S}.{msec} {recording_date.year:04d}\n"
    ).encode("ascii")
    temp = path.with_suffix(".tmp")
    with path.open("rb") as src, temp.open("wb") as dst:
        src.readline()
        dst.write(line)
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
    temp.replace(path)


# Vector CANoe writes ASC lines with an explicit frame-type keyword, while the
# legacy (and python-can) format omits it.  Both are supported below:
#   <ts> CANFD <ch> <id> <Rx|Tx> <BRS> <ESI> d <DLC:hex> <len:dec> <data...>
#   <ts> CAN   <ch> <id> <Rx|Tx>  0    0    d <DLC:hex> <len:dec> <data...>
#   <ts> <ch> <id> <Rx|Tx> d <DLC:hex> <data...>          (legacy classic)
#   <ts> CANFD <ch> <id> <Rx|Tx> d <DLC:hex> <len:dec> <data...>  (legacy FD)
ASC_MONTHS = {
    name: index for index, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1
    )
}
# Smallest epoch the BLF writer can represent (it rejects pre-1970 timestamps).
MIN_BLF_EPOCH = 1.0

_HEX_BYTES = set("0123456789abcdefABCDEF")

# CAN DLC code (0..15) -> payload length in bytes.
_DLC_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)


def local_utc_offset_seconds() -> float:
    """Seconds east of UTC for the local time zone (e.g. UTC+8 -> 28800).

    CANoe stores the BLF SYSTEMTIME as local wall-clock time, but python-can
    4.6 treats it as UTC (issue #1992).  This offset is used to compensate for
    that difference so the recorded date survives a conversion round trip.
    """
    import time

    if time.daylight and time.localtime().tm_isdst > 0:
        return -float(time.altzone)
    return -float(time.timezone)


class AscLogReader:
    """Tolerant Vector ASC reader supporting both CANoe keyword and legacy lines.

    Timestamps are exposed on an absolute epoch when a valid ``date`` header is
    present, otherwise as relative seconds.  Lines that cannot be parsed
    (statistics, J1939, comments, unknown variants) are skipped instead of
    aborting the whole file.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.start_time = 0.0
        self.parsed = 0
        self.skipped = 0
        self.bytes_read = 0

    @staticmethod
    def _parse_date(text: str) -> float | None:
        """Parse a Vector ASC date header ('Mon Jan  2 11:04:05.123 2026')."""
        match = re.search(
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d+)\s+"
            r"(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d+))?\s+(\d{4})",
            text,
        )
        if not match:
            return None
        month_name, day, hour, minute, second, fraction, year = match.groups()
        microsecond = int((fraction or "0")[:6].ljust(6, "0"))
        try:
            return datetime(
                int(year), ASC_MONTHS[month_name], int(day),
                int(hour), int(minute), int(second), microsecond,
            ).timestamp()
        except (ValueError, OSError, OverflowError):
            # Windows rejects pre-1970 local times (negative epoch) in
            # timestamp(); such files (e.g. unclocked loggers starting at
            # Jan 1 1970 08:00) fall back to relative timestamps below.
            return None

    def __iter__(self):
        with self.path.open("r", encoding="utf-8", errors="replace") as handle:
            in_body = False
            for raw_line in handle:
                self.bytes_read += len(raw_line)
                line = raw_line.strip()
                if not line:
                    continue
                if not in_body:
                    lower = line.lower()
                    if lower.startswith("date"):
                        parsed = self._parse_date(line)
                        if parsed is not None and parsed >= MIN_BLF_EPOCH:
                            self.start_time = parsed
                        continue
                    if "triggerblock" in lower:
                        # CANoe puts the recording date in the ``date`` header,
                        # but python-can writes it in ``Begin Triggerblock`` and
                        # keeps ``date`` as the conversion time; prefer the
                        # trigger block.
                        parsed = self._parse_date(line)
                        if parsed is not None and parsed >= MIN_BLF_EPOCH:
                            self.start_time = parsed
                        continue
                    if lower.startswith(("base", "internal", "no internal", "begin", "end", "//")) or lower.startswith("start of measurement"):
                        continue
                    in_body = True
                message = self._parse_message(line)
                if message is None:
                    self.skipped += 1
                    continue
                self.parsed += 1
                yield message

    def _parse_message(self, line: str):
        fields = line.split()
        if len(fields) < 2:
            return None
        try:
            timestamp = float(fields[0])
        except ValueError:
            return None

        index = 1
        # The frame-type keyword is informational only; real classic CAN lines
        # from CANoe share the CANFD field layout (flags + 'd' + DLC + length),
        # so the classic-vs-FD decision is taken from the payload length later.
        keyword = fields[index]
        if keyword == "CANFD" or keyword == "CAN_FD" or keyword == "CAN" or keyword == "canfd" or keyword == "can_fd" or keyword == "can":
            index += 1

        # Two field layouts are seen in the wild:
        #   CANoe / legacy:  <ch> <id>  Rx  <brs> <esi> d ...  (ID before Rx)
        #   python-can:      <ch> Rx <id>       <brs> <esi> d ...  (ID after Rx)
        # The direction marker sits at index+1 (python-can) or index+2 (CANoe);
        # the channel token always precedes the marker at ``index``.
        count = len(fields)
        token1 = fields[index + 1] if index + 1 < count else ""
        token2 = fields[index + 2] if index + 2 < count else ""

        if token1 == "Rx" or token1 == "Tx" or token1 == "rx" or token1 == "tx":
            is_rx = token1[0] in "Rr"
            id_text = token2
            channel = int(fields[index]) - 1 if fields[index].isdigit() else 0
            index = index + 3
        elif token2 == "Rx" or token2 == "Tx" or token2 == "rx" or token2 == "tx":
            is_rx = token2[0] in "Rr"
            id_text = token1
            channel = int(fields[index]) - 1 if fields[index].isdigit() else 0
            index = index + 3
        else:
            return None

        is_extended = id_text[-1:] == "x" or id_text[-1:] == "X"
        if is_extended:
            id_text = id_text[:-1]
        try:
            arbitration_id = int(id_text, 16)
        except ValueError:
            return None

        # Remote frames: an 'r'/'R' marker (with an optional DLC after it).
        if index < len(fields) and (fields[index] == "r" or fields[index] == "R"):
            remote_dlc = 8
            if index + 1 < len(fields) and fields[index + 1].isdigit():
                remote_dlc = int(fields[index + 1])
            return can.Message(
                timestamp=self.start_time + timestamp,
                arbitration_id=arbitration_id,
                is_extended_id=is_extended,
                is_remote_frame=True,
                is_rx=is_rx,
                channel=channel,
                dlc=remote_dlc,
                data=b"",
            )

        # Error frames (legacy form: '<id> ErrorFrame'; keyword form flags
        # are consumed below, so also accept the keyword here).
        if index < len(fields) and (fields[index] == "ErrorFrame" or fields[index] == "errorframe"):
            return can.Message(
                timestamp=self.start_time + timestamp,
                arbitration_id=arbitration_id,
                is_extended_id=is_extended,
                is_error_frame=True,
                is_rx=is_rx,
                channel=channel,
                dlc=0,
                data=b"",
            )

        # CANoe keyword-form lines carry two flags (BRS/ESI) after the marker.
        # Legacy classic lines have no flags here.
        brs = False
        esi = False
        if index + 1 < len(fields) and fields[index] in "01" and fields[index + 1] in "01":
            brs = fields[index] == "1"
            esi = fields[index + 1] == "1"
            index += 2

        # Data frames carry a DLC next, optionally preceded by a 'd' marker
        # (CANoe keyword lines and python-can classic rows have it; python-can
        # CANFD rows omit it).
        if index < len(fields) and (fields[index] == "d" or fields[index] == "D"):
            index += 1
        if index >= len(fields):
            return None

        # The DLC token is at ``index``.  Keyword-form lines then repeat the
        # payload length as a decimal value ('d 15 64 <bytes>', 'd 8 8 <bytes>'
        # or, without a 'd' marker on FD rows: 'f 64 <bytes>'); legacy classic
        # lines follow the DLC directly with the bytes ('d 8 <bytes>').  The
        # payload is the run of two-digit hex tokens; a decimal length token in
        # front of it is skipped only when doing so leaves exactly that many
        # bytes (classic rows have dlc_length bytes from the first token).
        try:
            dlc = int(fields[index], 16)
            dlc_length = _DLC_LENGTHS[dlc] if dlc < 16 else min(dlc, 64)
        except (ValueError, IndexError):
            return None
        rest = fields[index + 1:]

        # Collect every two-digit hex token in one comprehension (C-level loop).
        hex_all = [tok for tok in rest if len(tok) == 2 and tok[0] in _HEX_BYTES and tok[1] in _HEX_BYTES]

        # Default: legacy classic rows carry dlc_length bytes from the first
        # token on.
        payload = bytes.fromhex("".join(hex_all[:dlc_length]))

        # Keyword-form lines repeat the payload length as a decimal token before
        # the bytes ('d 8 8 <8 bytes>' for classic, 'f 64 <64 bytes>' /
        # 'd 15 64 <64 bytes>' for FD).  Recognise it when skipping the first
        # token leaves exactly the declared byte count.
        if rest and rest[0].isdigit() and 1 <= int(rest[0]) <= 64:
            declared = int(rest[0])
            after_first = hex_all[1:] if rest[0] in hex_all else hex_all
            if len(after_first) == declared and (dlc_length > 8 or len(hex_all) > dlc_length):
                payload = bytes.fromhex("".join(after_first[:declared]))

        if not payload:
            return None

        data = payload
        payload_length = len(data)

        is_fd_frame = payload_length > 8
        if is_fd_frame:
            return can.Message(
                timestamp=self.start_time + timestamp,
                arbitration_id=arbitration_id,
                is_extended_id=is_extended,
                is_fd=True,
                bitrate_switch=brs,
                error_state_indicator=esi,
                is_rx=is_rx,
                channel=channel,
                dlc=payload_length,
                data=data[:payload_length],
            )

        return can.Message(
            timestamp=self.start_time + timestamp,
            arbitration_id=arbitration_id,
            is_extended_id=is_extended,
            is_rx=is_rx,
            channel=channel,
            dlc=len(data),
            data=data[:8],
        )

    def stop(self) -> None:
        pass


def convert_file(
    source: Path,
    destination: Path,
    progress: Callable[[float], None] | None = None,
) -> tuple[int, int]:
    """Convert one BLF/ASC file into the opposite format.

    Returns ``(message_count, skipped_lines)``.  Timestamps stay on an absolute
    time base so the recording date is preserved.

    Time-zone handling: CANoe writes the BLF SYSTEMTIME in local wall-clock
    time while python-can 4.6 interprets it as UTC, so BLF readers return an
    epoch that is ``offset`` seconds too large and the BLF writer stores the
    system time ``offset`` seconds too small.  We shift by the local offset in
    each direction so both output formats show the correct recording date.

    ``progress``, when given, is called with this file's 0.0-1.0 completion
    fraction as it is read.
    """
    offset = local_utc_offset_seconds()
    suffix = source.suffix.lower()
    skipped_lines = 0
    if suffix == BLF_SUFFIX:
        reader = can.BLFReader(str(source))
        writer = can.ASCWriter(str(destination))
        time_shift = -offset
    elif suffix == ASC_SUFFIX:
        reader = AscLogReader(source)
        writer = can.BLFWriter(str(destination))
        time_shift = offset
    else:
        raise ValueError(f"Unsupported file type: {source.suffix}")

    file_size = source.stat().st_size or 1
    count = 0
    next_report = 0
    first_timestamp = None
    try:
        for message in reader:
            # BLF cannot represent pre-1970 timestamps; clamp so a bad header
            # (e.g. missing/1970 date) does not abort the whole file.
            if message.timestamp is not None:
                if suffix == ASC_SUFFIX and message.timestamp < MIN_BLF_EPOCH:
                    message.timestamp = MIN_BLF_EPOCH
                message.timestamp += time_shift
                if first_timestamp is None:
                    first_timestamp = message.timestamp
            writer.on_message_received(message)
            count += 1
            if progress is not None and count >= next_report:
                next_report = count + 512
                if suffix == ASC_SUFFIX:
                    fraction = min(getattr(reader, "bytes_read", 0) / file_size, 1.0)
                else:
                    fraction = min(getattr(getattr(reader, "file", None), "tell", lambda: file_size)() / file_size, 1.0)
                progress(fraction)
    finally:
        writer.stop()
        reader.stop()
    if suffix == BLF_SUFFIX and first_timestamp is not None:
        fix_asc_date_header(destination, first_timestamp)
    if progress is not None:
        progress(1.0)
    if suffix == ASC_SUFFIX:
        skipped_lines = getattr(reader, "skipped", 0)
    return count, skipped_lines


def convert_sources(
    selections: list[Path],
    output_dir: Path,
    direction: str = DIRECTION_AUTO,
    progress: Callable[[float], None] | None = None,
) -> tuple[int, int, list[str], list[str]]:
    """Convert every discovered log file; return counts and per-file errors.

    Returns ``(converted_files, message_count, skipped, errors)``.  Files whose
    type does not match the requested direction are reported in ``skipped``.

    ``progress``, when given, is called with the overall 0.0-1.0 completion
    fraction, weighted by the input files' sizes.
    """
    if can is None:
        raise RuntimeError("缺少 python-can 库，请先运行：pip install python-can")

    sources = collect_log_files(selections, DIRECTION_AUTO)
    if not sources:
        raise ValueError("所选文件或文件夹中未找到 .blf 或 .asc 文件。")

    converted_files = 0
    message_count = 0
    skipped_lines_total = 0
    skipped: list[str] = []
    errors: list[str] = []
    used_names: set[str] = set()

    # Separate the files to convert from those skipped by the direction filter.
    to_convert: list[tuple[Path, Path]] = []
    for source in sources:
        suffix = source.suffix.lower()
        if direction == DIRECTION_BLF_TO_ASC and suffix != BLF_SUFFIX:
            skipped.append(f"{source.name}（不是 BLF 文件）")
            continue
        if direction == DIRECTION_ASC_TO_BLF and suffix != ASC_SUFFIX:
            skipped.append(f"{source.name}（不是 ASC 文件）")
            continue
        to_convert.append((source, destination_for(source, output_dir, used_names)))

    total_size = sum(source.stat().st_size for source, _ in to_convert) or 1
    done_size = 0

    for source, destination in to_convert:
        size = source.stat().st_size or 0
        start_fraction = done_size / total_size
        end_fraction = (done_size + size) / total_size

        def file_progress(fraction: float, start: float = start_fraction, end: float = end_fraction) -> None:
            if progress is not None:
                progress(start + fraction * (end - start))

        try:
            file_messages, file_skipped_lines = convert_file(source, destination, progress=file_progress)
            message_count += file_messages
            skipped_lines_total += file_skipped_lines
            converted_files += 1
            if file_skipped_lines:
                skipped.append(f"{source.name}（{file_skipped_lines} 行无法识别，已跳过）")
        except Exception as error:  # Keep batch processing going; report later.
            errors.append(f"{source.name}: {error}")
        done_size += size

    return converted_files, message_count, skipped_lines_total, skipped, errors


class ConverterApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("BLF / ASC 互转工具")
        self.root.geometry("900x680")
        self.root.minsize(720, 540)
        self.sources: list[Path] = []
        self.last_output_dir: Path | None = None
        self.direction = StringVar(value=DIRECTION_AUTO)

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
        ttk.Button(action_bar, text="添加 BLF/ASC 文件", command=self.choose_files).pack(side="left")
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
            ttk.Label(source_box, text="提示：也可以将多个 BLF/ASC 文件或文件夹直接拖到上方列表中。", style="Subtitle.TLabel").grid(row=2, column=0, sticky="w", pady=(8, 0))

        operation_box = ttk.LabelFrame(frame, text=" 2. 转换 ", padding=12)
        operation_box.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        operation_box.columnconfigure(0, weight=1)
        settings_bar = ttk.Frame(operation_box)
        settings_bar.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        ttk.Label(settings_bar, text="转换方向：").pack(side="left")
        direction_box = ttk.Combobox(
            settings_bar, width=24, state="readonly", textvariable=self.direction, values=DIRECTIONS
        )
        direction_box.pack(side="left", padx=(4, 0))
        ttk.Label(
            operation_box,
            text="输出位置：工具所在目录\\BlfAscConvert_YYYYMMDD_HHMMSS\n每个输入文件都会生成一个同名的 .blf 或 .asc 文件。",
            justify="left",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 8))
        button_bar = ttk.Frame(operation_box)
        button_bar.grid(row=2, column=0, columnspan=4, sticky="e")
        ttk.Button(button_bar, text="查看检测到的文件", width=15, command=self.show_detected_files).pack(side="left", padx=(0, 8))
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
            self.set_status("准备就绪：请先添加 BLF/ASC 文件或文件夹。")
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
            if not (path.is_dir() or (path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES)):
                continue
            if path.resolve() not in known:
                self.sources.append(path)
                known.add(path.resolve())
                self.source_list.insert(END, str(path))

    def choose_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择 BLF/ASC 文件",
            filetypes=[("BLF/ASC 日志文件", "*.blf *.asc"), ("BLF 文件", "*.blf"), ("ASC 文件", "*.asc"), ("所有文件", "*.*")],
        )
        self.add_sources(list(paths))

    def choose_folders(self) -> None:
        try:
            paths = select_multiple_folders(self.root.winfo_id())
        except OSError as error:
            messagebox.showerror("BLF / ASC 互转工具", f"无法打开多选文件夹窗口：{error}")
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
        files = collect_log_files(self.sources, self.direction.get())
        if not files:
            messagebox.showwarning("BLF / ASC 互转工具", "当前来源中未检测到可转换的 BLF/ASC 文件。")
            return
        dialog = Toplevel(self.root)
        dialog.title("检测到的日志文件")
        dialog.geometry("900x520")
        container = ttk.Frame(dialog, padding=12)
        container.pack(fill="both", expand=True)
        ttk.Label(container, text=f"共检测到 {len(files)} 个不重复的日志文件：").pack(anchor="w", pady=(0, 8))
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
            messagebox.showerror("BLF / ASC 互转工具", f"无法打开输出文件夹：{error}")

    def start_conversion(self) -> None:
        if can is None:
            messagebox.showerror("BLF / ASC 互转工具", "缺少 python-can 库，请先运行：pip install python-can")
            return
        if not self.sources:
            messagebox.showwarning("BLF / ASC 互转工具", "请先添加至少一个 BLF/ASC 文件或文件夹。")
            return
        files = collect_log_files(self.sources, self.direction.get())
        if not files:
            messagebox.showwarning("BLF / ASC 互转工具", "所选来源中未检测到可转换的 BLF/ASC 文件。")
            return
        try:
            output_dir = create_output_directory()
        except OSError as error:
            messagebox.showerror("BLF / ASC 互转工具", f"无法创建输出文件夹：{error}")
            return
        self.last_output_dir = output_dir
        direction = self.direction.get()
        self.convert_button.configure(state=DISABLED)
        self.progress.configure(value=0)
        self.set_status(f"正在转换 {len(files)} 个文件（{direction}），请稍候……")
        threading.Thread(target=self.convert_worker, args=(self.sources.copy(), output_dir, direction), daemon=True).start()

    def convert_worker(self, sources: list[Path], output_dir: Path, direction: str) -> None:
        def report(fraction: float) -> None:
            self.root.after(0, self.update_progress, fraction)

        try:
            files, messages, _skipped_lines, skipped, errors = convert_sources(sources, output_dir, direction, progress=report)
            message = f"转换完成：已转换 {files} 个文件、共 {messages} 条报文。\n输出目录：{output_dir}"
            if skipped:
                message += "\n提示：\n" + "\n".join(skipped)
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
    parser = argparse.ArgumentParser(description="Convert Vector BLF and ASC CAN log files into each other.")
    parser.add_argument("selections", nargs="*", type=Path, help="BLF/ASC files and/or folders to convert; omit to open the GUI")
    parser.add_argument(
        "--direction",
        choices=("auto", "blf2asc", "asc2blf"),
        default="auto",
        help="conversion direction (default: auto converts each file to the opposite format)",
    )
    parser.add_argument("--out-root", type=Path, help="parent folder for timestamped output folders (default: app/out)")
    args = parser.parse_args()

    if can is None:
        print("Missing dependency python-can. Install it with: pip install python-can", file=sys.stderr)
        raise SystemExit(1)

    if args.selections:
        direction = {
            "auto": DIRECTION_AUTO,
            "blf2asc": DIRECTION_BLF_TO_ASC,
            "asc2blf": DIRECTION_ASC_TO_BLF,
        }[args.direction]
        output_dir = create_output_directory(args.out_root)
        files, messages, _skipped_lines, skipped, errors = convert_sources(args.selections, output_dir, direction)
        print(f"Converted {files} file(s), {messages} message(s).")
        print(f"Output: {output_dir}")
        if skipped:
            print("Notes:\n" + "\n".join(skipped))
        if errors:
            print("Failed files:\n" + "\n".join(errors), file=sys.stderr)
            raise SystemExit(1)
        return

    window = TkinterDnD.Tk() if DRAG_DROP_AVAILABLE else Tk()
    ConverterApp(window)
    window.mainloop()


if __name__ == "__main__":
    main()
