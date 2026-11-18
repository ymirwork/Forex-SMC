@echo off
title Forex and Gold SMC Scanner - Setup
cd /d "%~dp0"

echo ============================================================
echo      INSTALACJA I KONFIGURACJA FOREX AND GOLD SMC SCANNER
echo ============================================================
echo.

:: 1. Sprawdzanie interpretera Pythona
set PYTHON_CMD=

:: Sprawdz launcher py dla wersji 3.12 lub 3.11
py -3.12 -c "import sys" >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set PYTHON_CMD=py -3.12
    goto PYTHON_FOUND
)

py -3.11 -c "import sys" >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set PYTHON_CMD=py -3.11
    goto PYTHON_FOUND
)

:: Sprawdz domyslny py
py -c "import sys; assert sys.version_info < (3, 13)" >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set PYTHON_CMD=py
    goto PYTHON_FOUND
)

:: Sprawdz bezposrednio python (np. w PATH)
python -c "import sys; assert sys.version_info < (3, 13)" >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set PYTHON_CMD=python
    goto PYTHON_FOUND
)

:: Sprawdz czy jest uv (ktory moze automatycznie pobrac Python 3.12)
where uv >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set PYTHON_CMD=uv
    goto PYTHON_FOUND
)

:: Jesli zaden warunek nie przeszedl:
echo ============================================================
echo [BLAD] Nie znaleziono Pythona 3.11 lub 3.12!
echo ============================================================
echo MetaTrader 5 wymaga Pythona w wersji 3.11 lub 3.12.
echo (Wersje 3.13+ oraz sam skrot z Microsoft Store nie sa obslugiwane).
echo.
echo 1. Pobierz instalator Pythona 3.12:
echo    https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe
echo.
echo 2. Podczas instalacji KONIECZNIE zaznacz pole:
echo    "Add python.exe to PATH"
echo ============================================================
echo.
pause
exit /b 1

:PYTHON_FOUND
echo [1/3] Wykryty silnik Pythona: %PYTHON_CMD%
echo.

:: 2. Weryfikacja wirtualnego srodowiska .venv
echo [2/3] Sprawdzanie srodowiska wirtualnego .venv...

if not exist ".venv\Scripts\python.exe" goto CREATE_VENV

.venv\Scripts\python.exe -c "import sys" >nul 2>nul
if %ERRORLEVEL% equ 0 goto VENV_OK

echo [OSTRZEZENIE] Istniejace .venv jest uszkodzone lub skopiowane z innego PC!
echo Usuwanie starego srodowiska i tworzenie nowego...
rmdir /s /q .venv

:CREATE_VENV
echo Tworzenie nowego srodowiska wirtualnego .venv...
if "%PYTHON_CMD%"=="uv" (
    uv venv --python 3.12 .venv
) else (
    %PYTHON_CMD% -m venv .venv
)

if %ERRORLEVEL% neq 0 (
    echo [BLAD] Nie udalo sie utworzyc srodowiska wirtualnego.
    pause
    exit /b 1
)
echo Srodowisko .venv utworzone pomyslnie.

:VENV_OK
echo Srodowisko .venv jest poprawne.

echo.
echo [3/3] Instalacja zaleznosci z requirements.txt...

if "%PYTHON_CMD%"=="uv" (
    uv pip install --python .venv\Scripts\python.exe -r requirements.txt
    goto DEPS_DONE
)

.venv\Scripts\python.exe -m ensurepip --upgrade >nul 2>nul
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    where uv >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        echo [OSTRZEZENIE] Proba instalacji przez uv...
        uv pip install --python .venv\Scripts\python.exe -r requirements.txt
    )
)

:DEPS_DONE
echo.
if not exist ".env" (
    if exist ".env.example" (
        copy .env.example .env >nul
        echo Utworzono plik .env na bazie .env.example.
        echo Pamietaj, aby uzupelnic TELEGRAM_BOT_TOKEN i TELEGRAM_CHAT_ID!
    )
) else (
    echo Plik .env juz istnieje.
)

echo.
echo ============================================================
echo   INSTALACJA ZAKONCZONA POMYSLNIE!
echo ============================================================
echo   1. Otworz plik .env i uzupelnij tokeny Telegrama
echo   2. Uruchom MetaTrader 5 na komputerze
echo   3. Kliknij dwukrotnie start.bat, aby wlaczyc bota
echo ============================================================
echo.
pause
