@echo off
REM ===========================================================================
REM  Bifrost - Windows launcher
REM  Creates/uses a local virtual environment, installs dependencies and starts
REM  a single-worker Uvicorn server on the LAN. Keep this window open.
REM ===========================================================================
setlocal EnableExtensions

chcp 65001 >nul
set PYTHONUTF8=1

cd /d "%~dp0"

if not defined BIFROST_PORT set "BIFROST_PORT=8000"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [Bifrost] Creating Python virtual environment ^(.venv^) ...
    python -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1
    if errorlevel 1 (
        echo [Bifrost] ERROR: Python 3.11 or newer is required.
        echo           Install it from https://www.python.org/downloads/ and make
        echo           sure "Add python.exe to PATH" is selected.
        goto :fail
    )
    python -m venv .venv
    if errorlevel 1 goto :fail
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 goto :fail

echo [Bifrost] Installing dependencies ...
python -m pip install --disable-pip-version-check --quiet --upgrade pip
python -m pip install --disable-pip-version-check --quiet -r requirements.txt
if errorlevel 1 (
    echo [Bifrost] ERROR: Dependency installation failed.
    goto :fail
)

echo [Bifrost] Starting server on port %BIFROST_PORT% ...
python -m uvicorn main:app --host 0.0.0.0 --port "%BIFROST_PORT%" --workers 1 --timeout-graceful-shutdown 10 --no-access-log
if errorlevel 1 goto :fail

goto :end

:fail
echo.
echo [Bifrost] The server stopped because of an error.
echo           Check the messages above, then press any key to close this window.
pause >nul
exit /b 1

:end
endlocal
