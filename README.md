# CANProcess

Vector ASC 日志处理工具集，包含两个 Windows GUI 工具：

- **CAN 转 CAN FD 工具**（`CANProcess_AscCAN2FD.py`）：将 ASC 文件中指定 CAN ID 的经典 CAN 8 字节报文（ISO-TP 风格多帧传输）合并转换为 64 字节 CAN FD 报文。
- **ASC 日志合并工具**（`CANProcess_AscMerger.py`）：将多个 ASC 日志文件按记录起始时间合并为一个文件，并检查时间重叠。

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `CANProcess_AscCAN2FD.py` | CAN 转 CAN FD 工具（GUI + 命令行模式） |
| `CANProcess_AscMerger.py` | ASC 日志合并工具（GUI） |
| `Py2Exe.bat` | 基于 PyInstaller 的通用打包脚本 |
| `release/` | 已打包好的 EXE 文件 |

## 依赖

- Python 3.10+
- 仅标准库（tkinter、ctypes 等），无需额外安装
- 可选：`tkinterdnd2`（启用后支持将文件拖放到窗口列表）

```powershell
pip install tkinterdnd2   # 可选
```

## 使用方式

### 方式一：直接运行 EXE

进入 `release` 文件夹，双击 `CANProcess_AscCAN2FD.exe` 或 `CANProcess_AscMerger.exe`。

### 方式二：Python 运行

```powershell
# 打开 GUI
python CANProcess_AscCAN2FD.py
python CANProcess_AscMerger.py

# CAN 转 CAN FD：命令行模式（无 GUI）
python CANProcess_AscCAN2FD.py <ASC 文件或文件夹> [--fd-channel 10] [--can-id 6F4] [--out-root 输出目录]
```

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

- 结果保存到 `out\YYYYMMDD_HHMMSS` 文件夹（脚本或 EXE 所在目录下），每个输入文件生成一个 `<文件名>_CANFD.asc`。

## ASC 日志合并工具

### 合并规则

- 读取每个 ASC 文件头部 `date` 行确定起始时间，按时间先后排序合并。
- 各文件时间戳根据与最早文件的起始时间差进行偏移，输出 `base hex timestamps absolute` 格式。
- 头部日期无效或为 1970 年的文件自动跳过。
- 合并完成后提示无效文件及时间重叠检查结果。

### 输出

- 通过保存对话框选择输出位置，默认文件名为 `AscMerger_YYYYMMDD_HHMMSS.asc`。

## 打包 EXE

将 `.py` 文件拖到 `Py2Exe.bat` 上，或：

```powershell
Py2Exe.bat CANProcess_AscCAN2FD.py
Py2Exe.bat CANProcess_AscMerger.py
```

默认参数：`--onefile --windowed`，输出到脚本旁的 `release` 文件夹。可用 `Py2Exe.bat --help` 查看全部选项。

需要预先安装 PyInstaller：

```powershell
py -3 -m pip install pyinstaller
```
