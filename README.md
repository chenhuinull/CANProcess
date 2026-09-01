# CANProcess

Vector CAN 日志处理工具集，包含三个 Windows GUI 工具：

- **BLF / ASC 互转工具**（`CANProcess_BlfAscConvert.py`）：将 Vector BLF 与 ASC 日志互相转换，支持一次添加多个文件或文件夹批量处理，兼容经典 CAN 与 CAN FD。
- **CAN 转 CAN FD 工具**（`CANProcess_AscCAN2FD.py`）：将 ASC 文件中指定 CAN ID 的经典 CAN 8 字节报文（ISO-TP 风格多帧传输）合并转换为 64 字节 CAN FD 报文。
- **ASC 日志合并工具**（`CANProcess_AscMerger.py`）：将多个 ASC 日志文件按记录起始时间合并为一个文件，并检查时间重叠。

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `CANProcess_BlfAscConvert.py` | BLF / ASC 互转工具（GUI + 命令行模式） |
| `CANProcess_AscCAN2FD.py` | CAN 转 CAN FD 工具（GUI + 命令行模式） |
| `CANProcess_AscMerger.py` | ASC 日志合并工具（GUI） |
| `CANProcess_BlfAscConvert.bat` | 使用本地 Python 启动 BLF / ASC 互转工具 |
| `CANProcess_AscCAN2FD.bat` | 使用本地 Python 启动 CAN 转 CAN FD 工具 |
| `CANProcess_AscMerger.bat` | 使用本地 Python 启动 ASC 合并工具 |
| `Toolchain\python\` | 本地 Python、Tkinter 和工具依赖 |
| Py2Exe.bat | 基于 PyInstaller 的通用打包脚本（固定使用本地 Python） |
| Clean.bat | 清理项目内全部 Python __pycache__ 缓存目录 | 
| `Out/` | 已打包好的 EXE 文件 |

## 依赖

- 内置 `Toolchain\python\python.exe`（完整 Python + Tcl/Tk）
- 内置 `python-can`（BLF / ASC 互转）、`tkinterdnd2`（文件拖放）和 PyInstaller

运行和打包均使用内置 Python，不依赖系统 Python。若本地 Python 缺少 PyInstaller、`python-can` 或 `tkinterdnd2`，`Py2Exe.bat` 会自动下载安装到 `Toolchain\python`。

## 使用方式

### 方式一：直接运行 EXE

进入 `Out` 文件夹，双击对应的 EXE 文件。

### 方式二：同名 BAT 启动（推荐）

双击对应 BAT 即可打开 GUI；也可以把输入文件拖到对应 BAT 图标上：

```text
CANProcess_BlfAscConvert.bat
CANProcess_AscCAN2FD.bat
CANProcess_AscMerger.bat
```

三个 BAT 均固定使用 `Toolchain\python\python.exe`，不依赖系统 Python。

### 方式三：Python 运行

```powershell
# 打开 GUI
.\Toolchain\python\python.exe .\CANProcess_BlfAscConvert.py
.\Toolchain\python\python.exe .\CANProcess_AscCAN2FD.py
.\Toolchain\python\python.exe .\CANProcess_AscMerger.py

# BLF / ASC 互转：命令行模式（无 GUI）
.\Toolchain\python\python.exe .\CANProcess_BlfAscConvert.py <BLF/ASC 文件或文件夹> [--direction auto|blf2asc|asc2blf] [--out-root 输出目录]

# CAN 转 CAN FD：命令行模式（无 GUI）
.\Toolchain\python\python.exe .\CANProcess_AscCAN2FD.py <ASC 文件或文件夹> [--fd-channel 10] [--can-id 6F4] [--out-root 输出目录]
```

## BLF / ASC 互转工具

### 转换规则

- 自动模式下：`.blf` 转为 `.asc`，`.asc` 转为 `.blf`；也可强制只做 BLF→ASC 或 ASC→BLF。
- 支持一次选择多个文件、多个文件夹（文件夹递归扫描），或直接把文件/文件夹拖进窗口列表。
- 基于 python-can 读写，完整保留：CAN ID（标准/扩展帧）、经典 CAN / CAN FD、BRS/ESI 标志、远程帧、错误帧、数据长度与数据内容、通道号、收发方向（Rx/Tx）。
- 时间戳保持绝对时间基准：ASC 的 Triggerblock 日期与 BLF 的记录时间一致，不会从 0 重新开始。

### 输出

- 结果保存到 `BlfAscConvert_YYYYMMDD_HHMMSS` 文件夹（工具所在目录下），每个输入文件生成一个同名的 `.asc` 或 `.blf`。
- 个别文件损坏或格式不受支持时会跳过并在结果中列出，不影响其余文件转换。

## CAN 转 CAN FD 工具

### 转换规则

- 目标 CAN ID 默认为 `0x6F4`，可在 GUI 中修改。
- 支持的首帧格式为 `10 LL`（ISO-TP 首帧），连续帧为 `2N`（序号 0-F）。
- 每个完整传输合并为一条 64 字节 CAN FD 报文，时间戳取首帧时间：
  - 超出声明长度（LL）的字节被丢弃；
  - 不足 64 字节的部分用 `AA` 填充；
  - ASC 中 CAN FD 使用 DLC 15 表示 64 字节。
- 短单帧（首字节为 0-7 的长度值）直接转换为 CAN FD。
- 原始经典 CAN 的 `0x6F4` 记录全部从结果中移除：
  - 完整传输和短单帧被 CAN FD 替换；
  - 不完整或孤立的报文被丢弃（其他 CAN ID 的交错报文保留）。

### 输出

- 结果保存到 `AscCAN2FD_YYYYMMDD_HHMMSS` 文件夹（工具所在目录下），每个输入文件生成一个 `<文件名>_CANFD.asc`。

## ASC 日志合并工具

### 合并规则

- 读取每个 ASC 文件头部 `date` 行确定起始时间，按时间先后排序合并。
- 各文件时间戳根据与最早文件的起始时间差进行偏移，输出 `base hex timestamps absolute` 格式。
- 头部日期无效或为 1970 年的文件自动跳过。
- 合并完成后提示无效文件及时间重叠检查结果。

### 输出

- 结果保存到工具所在目录下的 `AscMerger_YYYYMMDD_HHMMSS` 文件夹，文件名为 `AscMerger_时间戳.asc`。
- 转换/合并结果仅在窗口下方的运行状态区域显示，不会弹出提示框。

## 打包 EXE

将 `.py` 文件拖到 `Py2Exe.bat` 上，或：

```powershell
Py2Exe.bat CANProcess_BlfAscConvert.py
Py2Exe.bat CANProcess_AscCAN2FD.py
Py2Exe.bat CANProcess_AscMerger.py
```

默认参数：`--onefile --windowed`，输出到脚本旁的 `Out` 文件夹。可用 `Py2Exe.bat --help` 查看全部选项。

`Py2Exe.bat` 固定使用 `Toolchain\python\python.exe`。若缺少所需打包或运行依赖，脚本会自动安装到该本地目录。


## 提交前清理缓存

双击 Clean.bat，即可删除项目目录及子目录内所有 __pycache__ 缓存目录，避免将 Python 字节码缓存提交到 Git。
