# Forex & Gold SMC Scanner + Telegram Semi-Auto Bot 🚀

Profesjonalny skaner i półautomat dla rynku **Forex oraz Złota (XAUUSD)** oparty o strategię **Smart Money Concepts (SMC)**, zintegrowany bezpośrednio z **MetaTrader 5** oraz botem **Telegram**.

---

## 🎯 Twoja Strategia: H4 ➡️ M15 CHoCH ➡️ Fibo Pullback $\ge$ 61.8% (OTE) + Confluence Engine

Bot wykorzystuje instytucjonalny system scoringowy oparty na **Wagach Koniunkcji (Confluence Weights)**:
1. **Kontekst H4 (Wyższa Rama):** Wyznacza dominujący trend i strefy instytucjonalne na H4 (+20%).
2. **Potwierdzenie na M15:** Czeka na **CHoCH (Change of Character)** – przełamanie lokalnej struktury na M15 (+20%).
3. **Mierzenie siatki Fibonacciego:** Po wystąpieniu CHoCH bot automatycznie mierzy falę impulsu i wyznacza kluczowe poziomy:
   * **50.0%** (Równowaga / Equilibrium)
   * **61.8%** (Golden Pocket – minimalny wymagany pullback, +15%)
   * **78.6%** (Deep Discount / OTE - Optimal Trade Entry, +20%)
4. **Zbieżność z Order Blockiem / FVG:** Nakładanie strefy Fibo na unmitigated Order Block lub FVG (+15%).
5. **Wybicie płynności (Liquidity Sweep):** Wybicie szczytu/dołka sesji azjatyckiej (Asian Range Sweep, +15%).
6. **Odrzucenie świecowe (Wick Rejection):** Knot odrzucenia $\ge 30\%$ zakresu świecy w strefie OTE (+10%).
7. **Filtr Jakości (Próg Punktowy):**
   * 🔥 **Grade A+ (Score $\ge 85\%$):** Wybitny, wielowymiarowy setup o wysokim prawdopodobieństwie.
   * ⭐ **Grade A (Score $70\% - 84\%$):** Bardzo mocny standardowy setup.
   * 🚫 **Score $< 70\%$:** Odrzucany automatycznie, aby chronić Cię przed szumem i stratami.
8. **Filtr Wiadomości Makroekonomicznych:** Automatycznie sprawdza kalendarz wydarzeń o dużym wpływie (Red Folder) i ostrzega przed publikacjami NFP, CPI czy decyzjami o stopach.
* **Automatyczne generowanie wykresów:**
  * Każdy alert zawiera ciemny, czytelny wykres świecowy z zaznaczoną strefą Order Blocka, poziomem wejścia (Entry), Stop Lossem (SL) i Take Profitami (TP1 / TP2).
* **Interaktywny Półautomat na Telegramie:**
  * Otrzymujesz powiadomienie push ze zdjęciem wykresu i parametrami zlecenia.
  * Pod wiadomością znajdują się przyciski:
    * `[ 🟢 Wejdź Market (1% ryzyka) ]` – automatycznie oblicza wielkość lota na podstawie odległości do SL i kapitału, a następnie otwiera pozycję w MT5!
    * `[ 🟡 Złóż Limit na strefie ]` – składa zlecenie oczekujące (Buy/Sell Limit) na krawędzi strefy.
    * `[ ❌ Pomiń ]` – odrzuca setup.
* **Bezpieczeństwo (Whitelist):**
  * Bot reaguje wyłącznie na Twój unikalny `TELEGRAM_CHAT_ID`. Nikt obcy nie może wykonać żadnej operacji na Twoim koncie.
* **Gotowy wskaźnik LuxAlgo SMC do MT5:**
  * W folderze `mql5_indicators/` znajduje się wskaźnik `SmartMoneyConcepts.ex5` i `.mq5`, który możesz wrzucić na wykres w MT5.

---

## 📂 Struktura Projektu

```text
Forex-SMC/
├── .env.example               # Szablon konfiguracji (bezpieczny dla Git)
├── .env                       # Twój prywatny plik konfiguracyjny (nigdy nie trafia na Git!)
├── .gitignore                 # Blokada plików wrażliwych (.env, trades.db, wykresy, venv)
├── requirements.txt           # Zależności i biblioteki Python
├── setup.bat                  # Instalator 1-kliknięciem dla nowego komputera
├── start.bat                  # Skrót do uruchamiania bota jednym kliknięciem
├── config.py                  # Główny moduł konfiguracyjny
├── mt5_client.py              # Klient MetaTrader 5 (dane, kalkulacja lota, egzekucja zleceń)
├── smc_detector.py            # Silnik SMC + Confluence Weights + Dynamic Best RR
├── rr_optimizer.py            # Optymalizator Best RR (symulacja MFE)
├── trade_journal.py           # Dziennik z SQLite (baza trades.db generuje się automatycznie)
├── news_filter.py             # Filtr wiadomości High-Impact (Red Folder)
├── session_manager.py         # Zarządzanie sesjami i Asian Range Liquidity Sweep
├── chart_generator.py         # Generator zrzutów wykresów świecowych w Dark Mode
├── telegram_bot.py            # Obsługa bota Telegrama i interaktywnych przycisków
├── scanner.py                 # Pętla skanera rynkowego
├── run.py                     # Główny plik startowy aplikacji
├── test_connection.py         # Skrypt sprawdzający poprawność połączenia z MT5 i Telegramem
├── mql5_indicators/           # Wskaźnik LuxAlgo SMC dla terminala MetaTrader 5
└── charts/                    # Katalog z wygenerowanymi zrzutami wykresów (.gitkeep)
```

---

## 💻 Uruchomienie na Nowym Komputerze (Instrukcja Krok po Kroku)

### Wymagania wstępne:
1. **System operacyjny:** Windows 10 lub 11 (64-bit).
2. **MetaTrader 5:** Zainstalowany terminal MT5 z zalogowanym kontem brokerskim.
3. **Python:** Wersja **3.11 lub 3.12** ([pobierz z python.org](https://www.python.org/downloads/)).
   > ⚠️ **Ważne:** Podczas instalacji Pythona koniecznie zaznacz opcję: **`Add python.exe to PATH`**.  
   > *(Biblioteka MetaTrader5 nie posiada jeszcze oficjalnych paczek dla Pythona 3.13+, dlatego wersja 3.11 lub 3.12 jest optymalna).*

---

### Krok 1: Sklonuj repozytorium z GitHuba
```powershell
git clone https://github.com/TWOJA_NAZWA/Forex-SMC.git
cd Forex-SMC
```

### Krok 2: Automatyczna instalacja (1 kliknięcie)
Kliknij dwukrotnie plik **`setup.bat`** (lub uruchom go w konsoli):
```powershell
setup.bat
```
Skrypt automatycznie:
* Utworzy środowisko wirtualne `.venv` na właściwym Pythonie (3.11 / 3.12).
* Zainstaluje wszystkie biblioteki z `requirements.txt`.
* Stworzy plik `.env` z gotowego szablonu `.env.example`.

### Krok 3: Uzupełnij plik `.env`
Otwórz nowo utworzony plik `.env` w Notatniku lub edytorze kodu i wklej swój token Telegrama oraz ID:
```ini
TELEGRAM_BOT_TOKEN=twoj_token_od_botfather
TELEGRAM_CHAT_ID=-100xxxxxxxxxx,123456789
```

### Krok 4: Uruchom MT5 i włącz bota
1. Upewnij się, że terminal **MetaTrader 5 jest otwarty** na komputerze.
2. (Opcjonalnie) Sprawdź połączenie testem diagnostycznym:
   ```powershell
   .venv\Scripts\python.exe test_connection.py
   ```
3. Uruchom skaner:
   * Kliknij dwukrotnie **`start.bat`**
   * LUB wpisz w konsoli:
     ```powershell
     .venv\Scripts\python.exe run.py
     ```

---

## ⚡ Szybka Konfiguracja Telegrama (BotFather)
1. Otwórz aplikację Telegram i znajdź użytkownika **`@BotFather`**.
2. Wyślij komendę `/newbot`, podaj nazwę i nazwę użytkownika (np. `MojeForexSMC_bot`).
3. Skopiuj otrzymany **Token API** (np. `7123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`).
4. Znajdź bota **`@userinfobot`** na Telegramie i wyślij `/start`, aby poznać swój numeryczny identyfikator (np. `123456789`).

### Krok 2: Uzupełnij plik `.env`
Otwórz plik `.env` w edytorze tekstu i wpisz:
```ini
TELEGRAM_BOT_TOKEN=twoj_token_od_botfather
TELEGRAM_CHAT_ID=twoj_numeryczny_chat_id
```

### Krok 3: Przetestuj i Uruchom
1. Upewnij się, że **MetaTrader 5 jest otwarty** na komputerze.
2. Uruchom test diagnostyczny:
   ```powershell
   .venv\Scripts\python.exe test_connection.py
   ```
3. Uruchom bota:
   * Kliknij dwukrotnie plik **`start.bat`**  
   * LUB wpisz w konsoli:
     ```powershell
     .venv\Scripts\python.exe run.py
     ```

---

## 📱 Komendy na Telegramie

W oknie czatu ze swoim botem możesz używać komend:
* `/start` – sprawdzenie statusu bota i połączenia z MT5
* `/status` – wyświetlenie salda, wyceny equity, wolnego marginu oraz otwartych pozycji z PnL
* `/scan` – natychmiastowe wymuszenie przeskanowania wszystkich par walutowych i złota
* `/journal` – przegląd statystyk dziennika transakcji (wygrane, przegrane, Win Rate, łączny zysk w R)
* `/optimizer` (lub `/bestrr`) – **Optymalizator BEST RR** – symuluje Twoje zagrania i oblicza matematycznie najlepszy poziom Take Profit
* `/addtrade <SYMBOL> <YES/NO> <REALIZED_R> <MAX_R>` – szybkie dodanie transakcji ręcznie z telefonu (np. `/addtrade XAUUSD YES 1.0 2.8`)
* `/help` – lista komend i opis działania

---

## 📈 Optymalizator BEST RR (MFE Analysis)

Optymalizator rozwiązuje odwieczny dylemat tradera: *„Czy zamykać szybciej z wyższym Win Rate, czy trzymać dłużej na wyższe RR?”*
Dla każdej pozycji zapisujesz:
1. **Realized R:** ile rzeczywiście zarobiłeś (np. `1.0R` lub `-1.0R`).
2. **Max R (MFE):** ile maksymalnie rynek dał wyciągnąć, zanim zawrócił (np. `2.8R`, `5.0R`).

Bot symuluje scenariusze „co by było, gdybyś zawsze trzymał stałe RR od 1.0 do 8.0” i wskazuje poziom dający najwyższy sumaryczny zysk netto (**BEST RR**) oraz maksymalny Profit Factor!

---

## 📊 Wskaźnik LuxAlgo SMC dla wykresów MT5

Pliki `SmartMoneyConcepts.ex5` i `SmartMoneyConcepts.mq5` zostały już zainstalowane w Twoim katalogu wskaźników MT5:
`MQL5/Indicators/SmartMoneyConcepts.ex5`

Aby go włączyć:
1. Otwórz **MetaTrader 5**.
2. W oknie **Nawigator** (Ctrl + N) rozwiń zakładkę **Wskaźniki**.
3. Przeciągnij **SmartMoneyConcepts** na dowolny wykres (np. XAUUSD lub EURUSD).
4. Na Twoim wykresie pojawią się kolorowe strefy Order Block, linie BOS/CHoCH oraz Fair Value Gaps.

---

## 🛡️ Zarządzanie Ryzykiem

Domyślnie bot ryzykuje **1.0% kapitału** na transakcję.
Wielkość pozycji jest wyliczana w czasie rzeczywistym ze wzoru:
$$\text{Lot} = \frac{\text{Saldo konta} \times \text{Ryzyko \%}}{\text{Dystans do SL w tickach} \times \text{Wartość ticka}}$$

Możesz zmienić te parametry w pliku `.env`:
```ini
DEFAULT_RISK_PERCENT=1.0        # Ryzyko na transakcję (np. 1%)
DEFAULT_RR_RATIO=2.0            # Domyślny stosunek zysku do ryzyka (1:2)
USE_OPTIMAL_RR=True             # Czy automatycznie adaptować RR do optymalizatora
MIN_TRADES_FOR_OPTIMAL_RR=5     # Minimalna liczba zagrań w bazie (np. 5 lub 10), aby aktywować Best RR
```

### 🧠 Dynamiczny Adaptacyjny Best RR w Praktyce:
* **Gdy w bazie jest mniej niż N zagrań (np. < 5):** Bot ustawia sztywne `DEFAULT_RR_RATIO=2.0` (1:2) z pliku `.env`.
* **Gdy w bazie zbierze się co najmniej N zagrań:** Bot analizuje dziennik i automatycznie dopasowuje TP1 do **matematycznie optymalnego Best RR** (np. `1:1.8` lub `1:2.8`), przy którym łączny zysk konta i Win Rate były najwyższe!
* Informacja o źródle Take Profit (`[Zoptymalizowane Best RR]` lub `[Domyślne z .env]`) pojawia się w każdym alercie na Telegramie.
