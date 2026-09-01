@echo off
setlocal
cd /d "%~dp0"
"%~dp0Toolchain\python\python.exe" "%~dp0CANProcess_AscMerger.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%