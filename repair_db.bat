@echo off
title Forex and Gold SMC Scanner - Naprawa Bazy Danych
cd /d "%~dp0"

echo ============================================================
echo   FOREX AND GOLD SMC SCANNER - NAPRAWA BAZY DANYCH (TRADES.DB)
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [BLAD] Nie wykryto srodowiska wirtualnego .venv!
    echo Uruchom najpierw setup.bat, aby zainstalowac wymagane biblioteki.
    pause
    exit /b 1
)

.venv\Scripts\python.exe repair_db.py --sync

echo.
pause
