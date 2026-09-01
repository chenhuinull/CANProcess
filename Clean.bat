@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set /a CACHE_COUNT=0
for /d /r "%~dp0" %%D in (__pycache__) do (
    if exist "%%D" (
        rd /s /q "%%D"
        set /a CACHE_COUNT+=1
    )
)

echo Removed !CACHE_COUNT! Python cache directories.
pause
exit /b 0