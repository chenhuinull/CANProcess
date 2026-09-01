@echo off
setlocal
cd /d "%~dp0"
"%~dp0DevEnv\python\python.exe" "%~dp0CANProcess_BlfAscConvert.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%