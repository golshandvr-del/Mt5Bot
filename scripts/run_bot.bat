@echo off
REM ===========================================================================
REM  MT5 Smart Trading Bot - RUN launcher (Windows 7)
REM ---------------------------------------------------------------------------
REM  Double-click to start the bot in the mode set in config\config.yaml
REM  (general.mode), or pass a mode as the first argument:
REM
REM     run_bot.bat              -> uses config general.mode (default: paper)
REM     run_bot.bat paper        -> one paper decision pass (no orders)
REM     run_bot.bat live         -> one live pass (SENDS orders - be careful)
REM     run_bot.bat search       -> Phase 3 strategy search (build memory)
REM     run_bot.bat backtest     -> internal walk-forward backtest report
REM     run_bot.bat train        -> Phase 1 offline ML training
REM     run_bot.bat loop         -> continuous paper/live loop (VPS friendly)
REM
REM  It locates the same Python that install.bat used (3.8.x), then runs main.py.
REM  Standard ASCII English only.
REM ===========================================================================

setlocal enableextensions enabledelayedexpansion
title MT5 Smart Trading Bot - Run

REM Project root is the parent of this scripts\ folder.
cd /d "%~dp0.."
set "MODE=%~1"

REM --- Locate Python (same search order as install.bat) ----------------------
set "PYEXE="
where py >nul 2>&1
if not errorlevel 1 (
    py -3.8 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%i in ('py -3.8 -c "import sys;print(sys.executable)"') do set "PYEXE=%%i"
    )
)
if not defined PYEXE (
    where python >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
    )
)
if not defined PYEXE (
    if exist "%LOCALAPPDATA%\MT5SmartBot\Python38\python.exe" (
        set "PYEXE=%LOCALAPPDATA%\MT5SmartBot\Python38\python.exe"
    )
)
if not defined PYEXE (
    echo [ERROR] No Python found. Please run install.bat first.
    echo Press any key to close.
    pause >nul
    endlocal
    goto :EOF
)

echo [ OK ]  Using Python: "!PYEXE!"
if defined MODE (
    echo [INFO]  Starting bot in mode: !MODE!
    "!PYEXE!" main.py --mode !MODE!
) else (
    echo [INFO]  Starting bot using mode from config\config.yaml
    "!PYEXE!" main.py
)

echo.
echo [INFO]  Bot process ended. Press any key to close.
pause >nul
endlocal
goto :EOF
