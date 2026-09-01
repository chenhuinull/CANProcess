@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem ============================================================================
rem Generic Python-to-EXE builder powered by PyInstaller.
rem
rem Use it by dragging one .py file onto this batch file, or run:
rem   build.bat MyTool.py [options]
rem
rem Command-line options override the defaults in the USER CONFIGURATION section.
rem ============================================================================

rem =========================== USER CONFIGURATION ============================
rem Package layout: --onefile creates one EXE; --onedir creates an EXE folder
rem containing its dependencies.
set "DEFAULT_BUNDLE_MODE=--onefile"

rem Window mode: --console shows a terminal for logs and command-line tools.
rem --windowed hides the terminal and is suitable for Tkinter/PyQt GUI programs.
rem This value is used for drag-and-drop builds unless --console/--windowed is set.
set "DEFAULT_UI_MODE=--windowed"

rem Output directory name. It is created beside the input .py file.
set "OUTPUT_FOLDER_NAME=Out"

rem Set to 1 to use a matching icon automatically (MyTool.py -> MyTool.ico).
rem Set to 0 to disable this lookup. An explicit --icon option always wins.
set "AUTO_USE_MATCHING_ICON=1"

rem Optional fixed icon for every build. Leave empty to use the matching-icon rule.
rem When set, this is used only if no --icon command-line option was supplied.
set "DEFAULT_ICON="
rem ============================================================================

if "%~1"=="" goto usage_error
if /I "%~1"=="--help" goto usage_success
if /I "%~1"=="-h" goto usage_success

set "SOURCE=%~f1"
set "SOURCE_EXT=%~x1"
set "SOURCE_DIR=%~dp1"
set "SOURCE_BASE=%~dpn1"
set "APP_NAME=%~n1"
set "BUNDLE_MODE=%DEFAULT_BUNDLE_MODE%"
set "UI_MODE=%DEFAULT_UI_MODE%"
set "ICON_ARG="
set "DATA_ARGS="
set "EXTRA_ARGS="
shift

:parse_args
if "%~1"=="" goto validate
if /I "%~1"=="--name" goto parse_name
if /I "%~1"=="--icon" goto parse_icon
if /I "%~1"=="--add-data" goto parse_data
if /I "%~1"=="--windowed" goto set_windowed
if /I "%~1"=="--console" goto set_console
if /I "%~1"=="--onefile" goto set_onefile
if /I "%~1"=="--onedir" goto set_onedir
if "%~1"=="--" goto collect_extra_start
echo [ERROR] Unknown option: %~1
goto usage_error

:parse_name
if "%~2"=="" goto missing_value
set "APP_NAME=%~2"
shift
shift
goto parse_args

:parse_icon
if "%~2"=="" goto missing_value
set "ICON_ARG=--icon "%~f2""
shift
shift
goto parse_args

:parse_data
if "%~2"=="" goto missing_value
rem The value uses PyInstaller's SOURCE;DEST format. Quote it when it contains spaces.
set "DATA_ARGS=%DATA_ARGS% --add-data "%~2""
shift
shift
goto parse_args

:set_windowed
set "UI_MODE=--windowed"
shift
goto parse_args

:set_console
set "UI_MODE=--console"
shift
goto parse_args

:set_onefile
set "BUNDLE_MODE=--onefile"
shift
goto parse_args

:set_onedir
set "BUNDLE_MODE=--onedir"
shift
goto parse_args

:collect_extra_start
shift

:collect_extra
if "%~1"=="" goto validate
set "EXTRA_ARGS=%EXTRA_ARGS% "%~1""
shift
goto collect_extra

:validate
if /I not "%SOURCE_EXT%"==".py" (
    echo [ERROR] The input must be a Python file: %SOURCE%
    exit /b 1
)
if not exist "%SOURCE%" (
    echo [ERROR] Source file not found: %SOURCE%
    exit /b 1
)
if not defined APP_NAME (
    echo [ERROR] The executable name cannot be empty.
    exit /b 1
)

rem Icon precedence: --icon option, then DEFAULT_ICON, then matching .ico file.
if not defined ICON_ARG if defined DEFAULT_ICON set "ICON_ARG=--icon "%DEFAULT_ICON%""
if not defined ICON_ARG if "%AUTO_USE_MATCHING_ICON%"=="1" if exist "%SOURCE_BASE%.ico" set "ICON_ARG=--icon "%SOURCE_BASE%.ico""

set "OUTPUT_DIR=%SOURCE_DIR%%OUTPUT_FOLDER_NAME%"
set "BUILD_ROOT=%SOURCE_DIR%build"
set "WORK_DIR=%BUILD_ROOT%\pyinstaller\%APP_NAME%"
set "SPEC_DIR=%BUILD_ROOT%\spec"

set "PYTHON=%~dp0Toolchain\python\python.exe"
if not exist "%PYTHON%" (
    echo [ERROR] Local Python was not found: %PYTHON%
    exit /b 1
)

"%PYTHON%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] pip is missing from Toolchain\python.
    echo Reinstall the local Python environment with pip enabled.
    exit /b 1
)

"%PYTHON%" -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Tkinter is missing from Toolchain\python.
    echo Reinstall the local Python environment with Tcl/Tk enabled.
    exit /b 1
)

"%PYTHON%" -c "import PyInstaller, can, tkinterdnd2" >nul 2>&1
if errorlevel 1 (
    echo Installing missing build and runtime dependencies into Toolchain\python...
    "%PYTHON%" -m pip install --upgrade pyinstaller python-can tkinterdnd2
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed.
        exit /b 1
    )
)

:build
rem --clean clears PyInstaller's temporary cache. Build/spec files stay beside the
rem input script rather than being created in whichever directory ran this batch file.
echo.
echo Source:  %SOURCE%
echo Output:  %OUTPUT_DIR%
echo Name:    %APP_NAME%
echo Mode:    %BUNDLE_MODE% %UI_MODE%
echo.

"%PYTHON%" -m PyInstaller ^
    --noconfirm --clean %BUNDLE_MODE% %UI_MODE% ^
    --name "%APP_NAME%" ^
    --distpath "%OUTPUT_DIR%" ^
    --workpath "%WORK_DIR%" ^
    --specpath "%SPEC_DIR%" ^
    --collect-data tkinterdnd2 %ICON_ARG% %DATA_ARGS% %EXTRA_ARGS% ^
    "%SOURCE%"

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed.
    exit /b 1
)

echo.
if /I "%BUNDLE_MODE%"=="--onefile" (
    echo [OK] Built: %OUTPUT_DIR%\%APP_NAME%.exe
) else (
    echo [OK] Built: %OUTPUT_DIR%\%APP_NAME%\%APP_NAME%.exe
)
exit /b 0

:missing_value
echo [ERROR] %~1 requires a value.
goto usage_error

:usage_error
echo.
set "USAGE_EXIT=1"
goto usage

:usage_success
set "USAGE_EXIT=0"

:usage
echo Generic Python EXE Builder
echo.
echo Drag one .py file onto build.bat, or run:
echo   build.bat ^<script.py^> [options]
echo.
echo Options:
echo   --name ^<name^>          Executable name (default: Python filename)
echo   --icon ^<file.ico^>      Icon file (default: matching .ico beside the script)
echo   --windowed               Hide the console window (for GUI programs)
echo   --console                Show a console window (default set in config)
echo   --onefile                Create one EXE file (default set in config)
echo   --onedir                 Create an EXE folder
echo   --add-data ^<src;dest^>   Bundle a file or folder; repeat as needed
echo   -- ^<PyInstaller args^>   Pass additional PyInstaller arguments through
echo.
echo Examples:
echo   build.bat AscMerger.py --windowed --icon AscMerger.ico
echo   build.bat tool.py --add-data "assets;assets" -- --hidden-import plugin_name
exit /b %USAGE_EXIT%
