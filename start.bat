@echo off
title Forex and Gold SMC Scanner
cd /d "%~dp0"

echo ============================================================
echo        STARTING FOREX AND GOLD SMC SCANNER (MT5 + TELEGRAM)
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment .venv not found!
    echo Please install dependencies first.
    pause
    exit /b 1
)

.venv\Scripts\python.exe run.py

pause
