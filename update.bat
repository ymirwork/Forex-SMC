@echo off
title Forex and Gold SMC Scanner - Aktualizacja
cd /d "%~dp0"

echo ============================================================
echo       AKTUALIZACJA FOREX AND GOLD SMC SCANNER (GITHUB)
echo ============================================================
echo.

:: 1. Sprawdzenie dostepnosci Gita
where git >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo ============================================================
    echo [BLAD] Git nie zostal znaleziony w systemie!
    echo ============================================================
    echo Aby pobierac aktualizacje, musisz miec zainstalowany program Git:
    echo https://git-scm.com/download/win
    echo.
    echo Upewnij sie, ze podczas instalacji zaznaczono opcje:
    echo "Git from the command line and also from 3rd-party software".
    echo ============================================================
    echo.
    pause
    exit /b 1
)

:: 2. Sprawdzenie czy folder to repozytorium Git
git rev-parse --is-inside-work-tree >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo ============================================================
    echo [BLAD] Ten folder nie jest repozytorium Git!
    echo ============================================================
    echo Nie mozna pobrac aktualizacji, poniewaz projekt nie zostal
    echo sklonowany przez git clone lub brakuje folderu .git.
    echo ============================================================
    echo.
    pause
    exit /b 1
)

:: Pobranie nazwy biezacej galezi
set "BRANCH="
for /f "tokens=*" %%a in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set "BRANCH=%%a"
if "%BRANCH%"=="" set "BRANCH=main"

echo [1/3] Wykryto galaz: %BRANCH%
echo       Sprawdzanie stanu repozytorium...

:: 3. Sprawdzenie czy sa niezatwierdzone lokalne zmiany w sledzonych plikach
set "LOCAL_CHANGES="
for /f "delims=" %%i in ('git status -uno --porcelain 2^>nul') do set "LOCAL_CHANGES=1"

if defined LOCAL_CHANGES (
    echo.
    echo ============================================================
    echo [UWAGA] Wykryto lokalnie zmodyfikowane pliki!
    echo ============================================================
    echo Posiadasz wlasne modyfikacje w plikach projektu, ktore moga
    echo kolidowac z nowa wersja pobierana z GitHuba.
    echo.
    echo Wybierz, jak chcesz postapic:
    echo.
    echo   [1] Bezpieczna aktualizacja (Stash - zachowaj zmiany i zaktualizuj)
    echo   [2] Standardowy git pull (moze zwrocic blad w razie kolizji)
    echo   [3] Twardy reset (ODRZUC lokalne zmiany i pobierz czysta wersje)
    echo   [4] Anuluj aktualizacje
    echo ============================================================
    choice /C 1234 /N /M "Wybierz opcje (1-4): "
    if errorlevel 4 goto CANCEL_UPDATE
    if errorlevel 3 goto CONFIRM_HARD_RESET
    if errorlevel 2 goto NORMAL_PULL
    if errorlevel 1 goto STASH_PULL
)

:NORMAL_PULL
echo.
echo [2/3] Pobieranie najnowszych zmian z GitHuba (git pull)...
git pull origin %BRANCH%
if %ERRORLEVEL% neq 0 (
    echo.
    echo ============================================================
    echo [BLAD] Pobieranie aktualizacji nie powiodlo sie!
    echo ============================================================
    echo Mozliwe przyczyny:
    echo  1. Brak polaczenia z internetem lub problem z serwerem GitHub.
    echo  2. Konflikt lokalnych plikow ze zmianami na GitHubie.
    echo ============================================================
    echo.
    pause
    exit /b 1
)
goto PULL_SUCCESS

:STASH_PULL
echo.
echo [2/3] Zabezpieczanie lokalnych zmian (git stash)...
git stash push -m "Automatyczna kopia zapasowa przed aktualizacja"
echo Pobieranie najnowszych zmian z GitHuba (git pull)...
git pull origin %BRANCH%
if %ERRORLEVEL% neq 0 (
    echo.
    echo [OSTRZEZENIE] Blad podczas git pull. Przywracanie zabezpieczonych zmian...
    git stash pop
    echo.
    pause
    exit /b 1
)
echo Przywracanie Twoich lokalnych zmian (git stash pop)...
git stash pop
goto PULL_SUCCESS

:CONFIRM_HARD_RESET
echo.
echo ============================================================
echo [OSTRZEZENIE] Twardy reset bezpowrotnie usunie Twoje lokalne
echo modyfikacje w sledzonych plikach i przywroci stan z GitHuba!
echo (Twoj plik .env nie zostanie naruszony).
echo ============================================================
choice /C TN /N /M "Czy na pewno chcesz nadpisac lokalne pliki? (T=Tak, N=Nie): "
if errorlevel 2 goto CANCEL_UPDATE

echo.
echo [2/3] Wymuszanie aktualnej wersji z GitHuba (reset --hard)...
git fetch origin %BRANCH%
git reset --hard origin/%BRANCH%
if %ERRORLEVEL% neq 0 (
    echo [BLAD] Nie udalo sie zresetowac repozytorium do origin/%BRANCH%.
    echo.
    pause
    exit /b 1
)
goto PULL_SUCCESS

:CANCEL_UPDATE
echo.
echo Aktualizacja zostala anulowana przez uzytkownika.
echo.
pause
exit /b 0

:PULL_SUCCESS
echo.
echo [3/3] Sprawdzanie srodowiska i bibliotek Pythona...

:: Sprawdzenie czy istnieje .env
if not exist ".env" (
    if exist ".env.example" (
        copy .env.example .env >nul
        echo Utworzono brakujacy plik .env na bazie .env.example.
    )
)

:: Aktualizacja bibliotek w .venv jesli srodowisko istnieje
if exist ".venv\Scripts\python.exe" (
    echo Weryfikacja zaleznosci wirtualnego srodowiska (.venv)...
    where uv >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        uv pip install --python .venv\Scripts\python.exe -r requirements.txt
    ) else (
        .venv\Scripts\python.exe -m pip install -r requirements.txt --upgrade --quiet
    )
    if %ERRORLEVEL% equ 0 (
        echo Zaleznosci sa aktualne.
    ) else (
        echo [OSTRZEZENIE] Wystapil problem podczas aktualizacji bibliotek. Mozesz uruchomic setup.bat.
    )
) else (
    echo [INFO] Nie wykryto folderu .venv. Jezeli to pierwsze uruchomienie, uzyj setup.bat.
)

echo.
echo ============================================================
echo   PROJEKT ZOSTAL POMYSLNIE ZAKTUALIZOWANY!
echo ============================================================
echo Aktualna wersja (ostatni commit):
git log -1 --oneline
echo ============================================================
echo Mozesz teraz uruchomic bota za pomoca start.bat.
echo ============================================================
echo.
pause
