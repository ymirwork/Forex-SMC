@echo off
title Forex & Gold SMC Scanner - Setup
cd /d "%~dp0"

echo ============================================================
echo      INSTALACJA I KONFIGURACJA FOREX & GOLD SMC SCANNER
echo ============================================================
echo.

:: Sprawdzenie wersji Pythona
set PYTHON_CMD=
where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
    py -3.12 -c "import sys" >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        set PYTHON_CMD=py -3.12
    ) else (
        py -3.11 -c "import sys" >nul 2>nul
        if %ERRORLEVEL% equ 0 (
            set PYTHON_CMD=py -3.11
        ) else (
            set PYTHON_CMD=py
        )
    )
) else (
    where python >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        set PYTHON_CMD=python
    ) else (
        echo [BŁĄD] Nie znaleziono Pythona w systemie!
        echo Pobierz i zainstaluj Python 3.11 lub 3.12 z: https://www.python.org/downloads/
        echo Upewnij sie, ze zaznaczysz pole: "Add python.exe to PATH".
        pause
        exit /b 1
    )
)

echo [1/3] Wykryty Python: %PYTHON_CMD%
echo [1/3] Tworzenie wirtualnego srodowiska (.venv)...
if not exist ".venv\Scripts\python.exe" (
    %PYTHON_CMD% -m venv .venv
    if %ERRORLEVEL% neq 0 (
        echo [BLAD] Nie udalo sie utworzyc srodowiska wirtualnego.
        pause
        exit /b 1
    )
    echo Srodowisko .venv utworzone pomyslnie.
) else (
    echo Srodowisko .venv juz istnieje.
)

echo.
echo [2/3] Instalacja zaleznosci z requirements.txt...
.venv\Scripts\python.exe -m ensurepip --upgrade >nul 2>nul
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo [OSTRZEZENIE] Wystapil blad podczas instalacji pip. Proba instalacji przez uv...
    where uv >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        uv pip install -r requirements.txt
    )
)

echo.
echo [3/3] Weryfikacja pliku konfiguracyjnego .env...
if not exist ".env" (
    copy .env.example .env
    echo Utworzono plik .env na bazie .env.example.
    echo Pamietaj, aby uzupelnic TELEGRAM_BOT_TOKEN i TELEGRAM_CHAT_ID przed uruchomieniem!
) else (
    echo Plik .env juz istnieje.
)

echo.
echo ============================================================
echo   INSTALACJA ZAKONCZONA POMYSLNIE!
echo ============================================================
echo   1. Otworz plik .env i wklej swoj token z Telegrama
echo   2. Uruchom MetaTrader 5 na komputerze
echo   3. Kliknij dwukrotnie start.bat, aby wlaczyc skaner
echo ============================================================
echo.
pause
