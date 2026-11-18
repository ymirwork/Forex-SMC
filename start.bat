@echo off
title Forex and Gold SMC Scanner
cd /d "%~dp0"

echo ============================================================
echo        STARTING FOREX AND GOLD SMC SCANNER (MT5 + TELEGRAM)
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment .venv not found!
    echo Please run setup.bat first to install dependencies.
    pause
    exit /b 1
)

.venv\Scripts\python.exe -c "import sys" >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Virtual environment .venv is broken or was copied from another machine!
    echo Please run setup.bat to automatically repair/rebuild it.
    pause
    exit /b 1
)

.venv\Scripts\python.exe run.py

pause
