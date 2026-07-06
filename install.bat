@echo off
REM ===========================================================================
REM  MT5 Smart Trading Bot - AUTOMATIC INSTALLER for Windows 7 (64-bit)
REM ---------------------------------------------------------------------------
REM  Just double-click this file (or run it from a command prompt).
REM
REM  What it does, in order:
REM    1. Looks for a usable Python 3.8.x interpreter (py launcher, PATH, or a
REM       previously bot-installed copy).
REM    2. If none is found, it uses PowerShell to DOWNLOAD and silently INSTALL
REM       Python 3.8.10 (64-bit), the last line with official Windows 7 support.
REM    3. Runs installer\install_helper.py, which installs all Python
REM       dependencies (with retries + per-package fallback), verifies the
REM       install, and generates sample data for a first offline run.
REM    4. Reminds you about the Visual C++ runtime that MetaTrader5 / numpy need.
REM
REM  Everything here is standard ASCII English only.
REM ===========================================================================

setlocal enableextensions enabledelayedexpansion
title MT5 Smart Trading Bot - Installer

REM --- Always work from the folder this script lives in ----------------------
cd /d "%~dp0"

echo ===========================================================================
echo  MT5 Smart Trading Bot - Windows 7 Automatic Installer
echo  Working folder: %CD%
echo ===========================================================================
echo.

REM ---------------------------------------------------------------------------
REM  0) Note about the Visual C++ runtime.
REM     numpy/pandas/lightgbm and the MT5 terminal need the VC++ 2015-2019
REM     x64 redistributable. We attempt an automatic download+install below
REM     only if a quick check suggests it is missing; it is a no-op if present.
REM ---------------------------------------------------------------------------

REM ===========================================================================
REM  1) Find a usable Python (prefer 3.8.x).
REM ===========================================================================
set "PYEXE="

REM 1a) Try the Python launcher pinned to 3.8 (best case on Windows).
where py >nul 2>&1
if not errorlevel 1 (
    py -3.8 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%i in ('py -3.8 -c "import sys;print(sys.executable)"') do set "PYEXE=%%i"
    )
)

REM 1b) Try a plain 'python' on PATH.
if not defined PYEXE (
    where python >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
    )
)

REM 1c) Try a copy this installer placed earlier.
if not defined PYEXE (
    if exist "%LOCALAPPDATA%\MT5SmartBot\Python38\python.exe" (
        set "PYEXE=%LOCALAPPDATA%\MT5SmartBot\Python38\python.exe"
    )
)

if defined PYEXE (
    echo [ OK ]  Found Python: "!PYEXE!"
    goto :HAVE_PYTHON
)

REM ===========================================================================
REM  2) No Python found -> download and install Python 3.8.10 (64-bit).
REM ===========================================================================
echo [WARN]  No usable Python found. Downloading Python 3.8.10 (64-bit)...
echo         (This is the last Python line with official Windows 7 support.)

set "PYURL=https://www.python.org/ftp/python/3.8.10/python-3.8.10-amd64.exe"
set "PYINST=%TEMP%\python-3.8.10-amd64.exe"
set "PYTARGET=%LOCALAPPDATA%\MT5SmartBot\Python38"

echo [INFO]  Downloading from %PYURL%
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "try { [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12 } catch {};" ^
  "Invoke-WebRequest -Uri '%PYURL%' -OutFile '%PYINST%'"
if errorlevel 1 (
    echo [ERROR] Failed to download Python. Check your internet connection.
    echo         You can also install Python 3.8.10 x64 manually from python.org
    echo         and then re-run this installer.
    goto :FAIL
)

echo [INFO]  Installing Python 3.8.10 silently to "%PYTARGET%" (per-user)...
REM InstallAllUsers=0 avoids needing admin. Include pip. Do not modify PATH
REM globally to stay non-invasive; we call the exe by full path afterwards.
"%PYINST%" /quiet InstallAllUsers=0 PrependPath=0 Include_pip=1 Include_test=0 TargetDir="%PYTARGET%"
if errorlevel 1 (
    echo [ERROR] Python installer returned an error.
    echo         Try running install.bat again, or install Python 3.8.10 x64
    echo         manually from python.org, then re-run this installer.
    goto :FAIL
)

if exist "%PYTARGET%\python.exe" (
    set "PYEXE=%PYTARGET%\python.exe"
    echo [ OK ]  Python installed at "!PYEXE!"
) else (
    echo [ERROR] Python was not found after install at "%PYTARGET%".
    goto :FAIL
)

:HAVE_PYTHON
echo.
echo ===========================================================================
echo  2b) Checking the Visual C++ x64 runtime (needed by numpy / MT5)...
echo ===========================================================================
where powershell >nul 2>&1
if not errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\installer\install_vcredist.ps1"
) else (
    echo [WARN]  PowerShell not found; skipping VC++ runtime auto-check.
    echo         If 'import numpy' later fails with 'DLL load failed', install
    echo         vc_redist.x64.exe from https://aka.ms/vs/16/release/vc_redist.x64.exe
)

echo.
echo ===========================================================================
echo  3) Installing project dependencies and verifying the environment...
echo ===========================================================================
"!PYEXE!" "%CD%\installer\install_helper.py"
set "HELPER_RC=!errorlevel!"

echo.
echo ===========================================================================
if "!HELPER_RC!"=="0" (
    echo [ OK ]  INSTALLATION COMPLETE AND VERIFIED.
    echo.
    echo  Next steps:
    echo    1. Open config\config.yaml and adjust symbols / risk / features.
    echo    2. Build the strategy memory:   "!PYEXE!" main.py --mode search
    echo    3. See a backtest report:       "!PYEXE!" main.py --mode backtest
    echo    4. One paper decision pass:     "!PYEXE!" main.py --mode paper
    echo    or simply double-click scripts\run_bot.bat
) else (
    echo [WARN]  Installation finished with warnings/errors ^(code !HELPER_RC!^).
    echo         Scroll up to read the messages. Re-running install.bat is safe
    echo         and will retry any failed packages.
)
echo ===========================================================================
echo.
echo Press any key to close this window.
pause >nul
endlocal
goto :EOF

:FAIL
echo.
echo ===========================================================================
echo [ERROR] Installation could not complete. See the messages above.
echo ===========================================================================
echo Press any key to close this window.
pause >nul
endlocal
goto :EOF
