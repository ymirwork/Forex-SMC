# Project Context: Forex & Gold SMC Scanner + Semi-Auto Bot 🚀

Niniejszy dokument stanowi **oficjalne kompendium wiedzy architektonicznej i kontekstu technicznego** projektu. Jest to nadrzędny dokument referencyjny dla programistów oraz autonomicznych agentów AI.

---

## 1. Wizja i Cel Systemu

System to zaawansowany **półautomatyczny skaner i silnik egzekucyjny** dla rynków Forex (Major Pairs) oraz Złota (XAUUSD). Łączy on zaawansowaną analizę techniczną z rygorystycznym instytucjonalnym zarządzaniem ryzykiem i natychmiastową komunikacją mobilną przez Telegram.

### Główne cele:
1. **Wykrywanie formacji wysokiego prawdopodobieństwa (High-Probability Setups):**
   - Połączenie metodologii **Smart Money Concepts (SMC)**, **Zumiko KillMLM (Multi-Timeframe Trend)** oraz **Instytucjonalnych Sweepów Płynności**.
2. **Półautomatyczna kontrola (Human-in-the-Loop Semi-Auto):**
   - Trader otrzymuje na telefon (Telegram) zrzut wykresu Dark Mode ze strefami i przyciskami interaktywnymi (`Market`, `Limit`, `Pomiń`).
   - Jedno kliknięcie kalkuluje bezpieczną wielkość lota i składa zlecenie bezpośrednio w **MetaTrader 5**.
3. **Aktywna ochrona kapitału i reguły Prop Firm:**
   - Automatyczny Break-Even (+0.85R + bufor prowizji), ochrona przed spreadem podczas rolloveru (Rollover SL Shield), twarde minimalne odległości SL, limit ekspozycji na tę samą walutę i statyczny drawdown.
4. **Ciągła pętla uczenia i optymalizacji:**
   - Każda transakcja zapisywana jest w SQLite (`trades.db`) i synchronizowana z Notion.
   - Silnik **Best RR (MFE Optimizer)** symuluje optymalny poziom Take Profit na podstawie historii.

---

## 2. Architektura Systemu i Warstwy

System zaprojektowany jest w architekturze modułowej o wyraźnie rozdzielonych odpowiedzialnościach:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     WARSTWA PREZENTACJI I KONTROLI                      │
│             telegram_bot.py  ──  chart_generator.py                     │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │
┌────────────────────────────────────▼────────────────────────────────────┐
│                    WARSTWA ORKIESTRACJI I SKANERA                       │
│                     run.py  ──  scanner.py                              │
└───────────────────┬─────────────────────────────────┬───────────────────┘
                    │                                 │
┌───────────────────▼──────────────┐   ┌──────────────▼───────────────────┐
│ WARSTWA STRATEGII I DETEKCJI     │   │ WARSTWA FILTRÓW I RYZYKA         │
│ - smc_detector.py                │   │ - session_manager.py             │
│ - zumiko_detector.py             │   │ - news_filter.py                 │
│ - liquidity_detector.py          │   │ - trade_insights.py              │
└───────────────────┬──────────────┘   └──────────────┬───────────────────┘
                    │                                 │
┌───────────────────▼─────────────────────────────────▼───────────────────┐
│             WARSTWA BROKERA I EGZEKUCJI (MetaTrader 5)                  │
│             mt5_client.py  ──  config.py (SSOT)                         │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │
┌────────────────────────────────────▼────────────────────────────────────┐
│             WARSTWA PERSYSTENCJI I DANYCH ANALITYCZNYCH                 │
│   trade_journal.py (SQLite) ── notion_journal.py ── rr_optimizer.py     │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Komponenty i Mapa Odpowiedzialności Plików

| Plik | Rola i Odpowiedzialność | Kluczowe Klasy / Funkcje | Niedozwolone w tym pliku |
| :--- | :--- | :--- | :--- |
| **`config.py`** | **Pojedyncze Źródło Prawdy (SSOT)** dla konfiguracji i parametrów. Odczyt `.env`, normalizacja nazw instrumentów (`clean_symbol`). | `clean_symbol()`, stałe konfiguracyjne | Logika biznesowa, zapytania SQL, wywołania MT5. |
| **`mt5_client.py`** | Klient API MetaTrader 5. Pobieranie notowań OHLCV, zarządzanie połączeniem, obliczanie wielkości lota, składanie/modyfikacja zleceń, tracking otwartych pozycji, Rollover Shield, piramidowanie. | `MT5Client` | Samodzielne podejmowanie decyzji o strategiach SMC. |
| **`smc_detector.py`** | Silnik detekcji Smart Money Concepts. Identyfikacja BOS, CHoCH, Order Blocks, FVG, zniesień Fibonacciego OTE, scoring konfluencji (0-100%), wyliczanie poziomów SL/TP. | `SMCDetector`, `TradeSetup`, `ConfluenceItem`, `calculate_stop_loss()` | Bezpośrednie składanie zleceń w MT5, wysyłanie wiadomości Telegram. |
| **`zumiko_detector.py`** | Silnik strategii Zumiko KillMLM. EMA 10/21/238, struktura swingów (Higher Lows / Lower Highs), MTF Alignment (M15, H1, H4). | `ZumikoDetector`, `ZumikoSetup` | Obsługa bazy danych SQLite, generowanie wykresów. |
| **`liquidity_detector.py`** | Wykrywanie instytucjonalnych poziomów płynności: PDH/PDL, PWH/PWL, EQH/EQL oraz Asian Range sweeps. | `LiquidityDetector`, `LiquidityEvaluation`, `SweptLevel` | Parsowanie wiadomości Telegram, wywołania brokera. |
| **`scanner.py`** | Pętla skanera rynkowego. Koordynuje pobieranie danych z MT5, uruchamia detektory, sprawdza filtry (news, rollover, sesje, prop firm), generuje setupy, wywołuje generowanie wykresu i przekazuje alerty do bota. | `SMCScanner`, `is_in_rollover_window()` | Logika rysowania wykresów (delegowana do `chart_generator`). |
| **`telegram_bot.py`** | Interfejs użytkownika. Wysyłanie alertów ze zdjęciem wykresu, obsługa przycisków inline (`Market`, `Limit`, `Pomiń`), autoryzacja (`ALLOWED_CHAT_IDS`), komendy `/status`, `/journal`, `/scan`, `/bestrr`. | `TelegramBotManager` | Obliczanie poziomów Fibo/SMC (od tego są detektory). |
| **`chart_generator.py`** | Generator wykresów świecowych w stylu Dark Mode (mplfinance). Rysuje świece, strefy OB/FVG, linie Fibo OTE, EMA Zumiko, poziomy płynności, Entry, SL, TP1, TP2. | `ChartGenerator` | Logika decyzyjna tradingu. |
| **`trade_journal.py`** | Dziennik transakcji w SQLite (`trades.db`). Rejestracja zagrań, statusów, PnL, R-wielokrotności, ochrona przed uszkodzeniem bazy (WAL, autorecovery, reindex). | `TradeJournal`, dekorator `_auto_recover_on_corruption` | Bezpośrednie wywołania graficzne. |
| **`trade_insights.py`** | Silnik statystyczny badający przewagę (Edge). Analizuje Win Rate i EV w podziale na sesje, symbole, strategie; dynamicznie rekomenduje redukcję ryzyka dla słabych par. | `TradeInsightEngine`, `EdgeMetrics` | Edycja zleceń w MT5. |
| **`adaptive_manager.py`** | Silnik adaptacyjnego zarządzania pozycjami: klasyfikacja reżimów rynkowych (STRONG_TREND, COMPRESSION, HIGH_VOLATILITY), dynamiczny próg Break-Even, strukturalny trailing SL (M15 Swing) oraz filtr piramidowania. | `AdaptivePositionManager`, `MarketRegime` | Bezpośrednie rysowanie wykresów. |
| **`rr_optimizer.py`** | Optymalizator Best RR oparty na symulacji Maximum Favorable Excursion (MFE). Oblicza matematycznie optymalny Take Profit. | `RROptimizer`, `RROptimizerResult` | Komunikacja sieciowa. |
| **`session_manager.py`** | Śledzenie sesji giełdowych (Azja, Londyn, Nowy Jork AM/PM, Rollover), Killzones oraz wyznaczanie ekstremów sesji azjatyckiej (Asian Range High/Low). | `SessionManager`, `get_broker_utc_offset()` | Pobieranie notowań z zewnętrznych API. |
| **`news_filter.py`** | Filtr kalendarza makroekonomicznego (ForexFactory). Wykrywanie wiadomości High-Impact (czerwone teczki) w oknie czasowym +/- 45 min. | `NewsFilter`, `EconomicEvent` | Modyfikacja bazy SQLite. |
| **`notion_journal.py`** | Klient API Notion. Mapowanie transakcji z bazy SQLite do bazy danych Notion w chmurze. | `NotionJournal` | Obsługa protokołu MT5. |
| **`sync_journal.py`** | Skrypt CLI do dwukierunkowej synchronizacji historii transakcji: MT5 <-> SQLite <-> Notion. | `sync_all()` | Logika czasu rzeczywistego skanera. |
| **`repair_db.py`** | Narzędzie ratunkowe do naprawy uszkodzonego pliku `trades.db`. | `repair_database()` | - |
| **`run.py`** | Główny plik startowy aplikacji. Inicjalizuje MT5, Telegram, wątki skanera i monitora pozycji. | `main()` | Szczegółowa logika tradingowa. |

---

## 4. Modele Domenowe i Logika Strategii

### 4.1. Strategia Smart Money Concepts (SMC)
- **Kontekst Trendu HTF (H4):** Określenie nadrzędnego kierunku rynku (BULLISH / BEARISH / NEUTRAL).
- **Struktura LTF (M15):** Wykrycie wybicia struktury **CHoCH (Change of Character)** lub **BOS (Break of Structure)**.
- **Optymalna Strefa Wejścia (OTE - Optimal Trade Entry):** Po impulsie wyznaczana jest siatka Fibonacciego:
  - 50.0% – Poziom równowagi (Equilibrium).
  - 61.8% – 78.6% – **Golden Pocket (Strefa OTE)**.
- **Konfluencje Instytucjonalne:**
  - Order Block (OB) w strefie OTE.
  - Fair Value Gap (FVG) jako magnes płynnościowy.
  - Asian Range Liquidity Sweep (wybicie płynności sesji azjatyckiej).
- **Potwierdzenie Świecowe (Rejection Wick):** Świeca reakcyjna musi zamknąć się w kierunku setupu lub posiadać knot odrzucenia $\ge 25\%$ całkowitej rozpiętości świecy.
- **Scoring Konfluencji (Wagi 0-100%):**
  1. Trend H4 (+20%)
  2. M15 CHoCH (+20%)
  3. Fibo OTE $\ge 61.8\%$ (+20%)
  4. Order Block / FVG (+15%)
  5. Asian Sweep (+15%)
  6. Rejection Wick (+10%)
- **Klasyfikacja Setupu:**
  - **Grade A+:** Score $\ge 85\%$
  - **Grade A:** Score $70 - 84\%$
  - **Grade B / Reject:** Score $< 70\%$

### 4.2. Strategia Zumiko KillMLM (Oficjalny Poradnik PDF i Webinarium ZuMiKo)
Zaimplementowana ściśle według oficjalnej specyfikacji systemu scalpingowo-trendowego ZuMiKo KillMLM:
1. **Trend Bazowy:** `EMA 238` (Market Filter: Cena > EMA 238 = dozwolony wyłącznie BUY; Cena < EMA 238 = dozwolony wyłącznie SELL).
2. **Dynamiczna Wstęga Momentum:** `EMA 9 (Fast)` oraz `EMA 21 (Slow)` (parametry oficjalne ZuMiKo zaktualizowane z EMA 10 na EMA 9).
3. **Instytucjonalna Kotwica VWAP:** Dzienny Volume Weighted Average Price (`ZUMIKO_REQUIRE_VWAP_ALIGNMENT=True`). Dla BUY cena musi znajdować się powyżej VWAP (potwierdzenie akumulacji), dla SELL poniżej VWAP (potwierdzenie dystrybucji).
4. **Magnes Wolumenowy POC (Point of Control):** Wyznaczany na bazie profilu wolumenu (lookback 40 barów). W przypadku gdy POC znajduje się pomiędzy wejściem a TP1 z minimalnym zyskiem $\ge 1.5R$, TP1 jest automatycznie docinany do poziomu POC (`ZUMIKO_ENABLE_POC_TARGETING=True`), zabezpieczając zysk przed strefą silnej reakcji wolumenowej.
5. **Ekspansja Wstęgi (Ribbon Expansion):** Badanie odległości $|EMA 9 - EMA 21|$. Wzrost szerokości wstęgi potwierdza rosnący impet instytucjonalny; zwężenie wstęgi wskazuje na wyczerpanie trendu lub przejście w konsolidację.
6. **Filtr Wykupienia/Wyprzedania RSI (14):** Zabezpiecza przed wchodzeniem w pozycję po nadmiernym rozciągnięciu rynku (`ZUMIKO_RSI_MAX_BUY=75.0` dla BUY, `ZUMIKO_RSI_MIN_SELL=25.0` dla SELL).
7. **Filtr Świecowy Heikin-Ashi:** Weryfikacja czystości impulsu (`ZUMIKO_USE_HEIKIN_ASHI_FILTER=True`). Wymaga świecy ze znikomym przeciwnym knotem (flat bottom dla BUY, flat top dla SELL), eliminując fałszywe wybicia z długimi knotami odrzucenia.
8. **Struktura Rynku:** Weryfikacja formowania Higher Low (HL) dla BUY lub Lower High (LH) dla SELL.
9. **Klasyfikacja Sygnału:**
   - **Główny Sygnał ZuMiKo KillMLM (Grade A+):** Zgodność trendu MTF (H1/H4) + Rozszerzająca się wstęga EMA 9/21 + Potwierdzenie pozycji względem VWAP.
   - **Sygnał Korekcyjny ZuMiKo (Grade A/B):** Odbicie w strefie EMA 9/21 (Pullback) w kierunku trendu bazowego EMA 238.

### 4.3. Złoty Setup (Golden Combo)
Występuje, gdy na instrumencie w tym samym czasie wystąpi zbieżność setupu SMC oraz Zumiko:
- Zgoda kierunku: M15 CHoCH + Fibo OTE w kierunku zgodnym z EMA 238 i trendem H1.
- Generuje najwyższy priorytet alertu: **Grade A+ (Score 95-100%)**.

### 4.4. Instytucjonalne Poziomy Płynności (`liquidity_detector.py`)
Wykrywa strefy płynności zewnętrznej (Buy-Side / Sell-Side Liquidity) i ich fałszywe wybicia (Sweeps):
- **PDH / PDL:** Previous Day High / Low.
- **PWH / PWL:** Previous Week High / Low.
- **EQH / EQL:** Equal Highs / Equal Lows (podwójne/potrójne szczyty i dołki w tolerancji ATR).
- **Asian Session High / Low:** Ekstremum sesji azjatyckiej (00:00 - 08:00 UTC).

### 4.5. Zaawansowane Reguły Smart Risk (Smart Risk Mentorship & A+ Setupy)
Zaimplementowane na bazie instytucjonalnych zasad Smart Money Concepts:
1. **Strong vs Weak Structural Points (`smc_detector.py`):**
   - **BUY:** Dołek wybijający strukturę w górę to **Strong Low** (ochrona pozycji), a szczyt niezdolny do jego przebicia to **Weak High** (cel płynnościowy TP).
   - **SELL:** Szczyt wybijający strukturę w dół to **Strong High** (ochrona pozycji), a dołek to **Weak Low** (cel płynnościowy TP).
   - `ENFORCE_STRONG_STRUCTURAL_SL`: opcja zakotwiczenia Stop Lossa ściśle za poziomem Strong, eliminująca wycięcia na szumie.
2. **Filtr Sweep-Engineered OB (`smc_detector.py`):**
   - Wykrywa Order Blocki, które powstały w wyniku natychmiastowego zgarnięcia płynności (Manipulation Grab / Inducement Sweep) z poprzednich świec przed impulsem BOS/CHoCH.
   - Setupy oparte o Sweep-Engineered OB otrzymują bonus konfluencji `+15%` (`BONUS_POINTS_SWEEP_OB`) lub mogą stanowić warunek konieczny (`REQUIRE_SWEEP_ENGINEERED_OB=True`).
3. **Filtr Room-to-Run / Opposite S/D Barrier & TP1 Capping (`smc_detector.py`):**
   - Weryfikuje przestrzeń do najbliższej nieprzetestowanej strefy przeciwległej na wyższych interwałach (H1/H4).
   - Zabezpiecza przed kupowaniem tuż pod podażą H1/H4 lub sprzedawaniem tuż nad popytem: jeśli wolna przestrzeń $< 1.5R$ (`ROOM_TO_RUN_HTF_MIN_RR`), trade zostaje odrzucony.
   - **TP1 Capping:** Jeśli przeciwległa strefa znajduje się w bezpiecznej odległości ($\ge 1.5R$), ale bliżej niż domyślny Take Profit, TP1 jest automatycznie docinany do poziomu bariery, uniemożliwiając celowanie w ceny za strefą odwrócenia.
4. **Filtr Spójności Kierunkowej (Directional Conflict Shield w `scanner.py`):**
   - Automatycznie wykrywa i odrzuca przeciwstawne sygnały (np. SMC BUY vs Zumiko SELL na tej samej parze w tym samym skanie lub w oknie cooldownu).
   - Zapobiega sprzecznym alertom na Telegramie oraz otwieraniu niedozwolonych hedge'y na kontach Prop Firm.

---

## 5. Cykl Życia Pozycji i Aktywne Zarządzanie

```
[Nowy Setup z Skanera]
       │
       ▼
[Alert na Telegramie z Wykresem i Przyciskami]
       │
   ┌───┴──────────────────────────┐
   ▼                              ▼
[Klik: Wejdź Market]           [Klik: Złóż Limit]
   │                              │
   ▼                              ▼
[Obliczenie Bezpiecznego Lota]    [Kalkulacja Lota na Strefie OTE]
   │                              │
   ▼                              ▼
[Złożenie Zlecenia w MT5]      [Złożenie Zlecenia Oczekującego MT5]
   │                              │
   └──────────────┬───────────────┘
                  │
                  ▼
         [Monitor Pozycji (co 15s)]
                  │
    ┌─────────────┼───────────────────────────┐
    ▼             ▼                           ▼
[Zysk >= +0.85R] [Rollover 23:40-00:30 MT5] [Spełnione Warunki Piramidowania]
    │             │                           │
    ▼             ▼                           ▼
[Przestawienie   [Rollover SL Shield:        [Piramidowanie L2/L3:
 na Break-Even    Tymczasowy Disaster SL,     Zabezpieczenie zysku L1,
 +0.2 pips]       Przywrócenie po spreadzie]  Decaying Risk, Master TP]
                  │
                  ▼
         [Zamknięcie Pozycji (TP / SL / Manual)]
                  │
                  ▼
         [Zapis do trades.db + Sync do Notion + Notyfikacja TG]
```

### Szczegóły Zabezpieczeń w Locie:
1. **Automatyczny Break-Even (Auto-BE):**
   - Po osiągnięciu zysku `AUTO_BE_R_TRIGGER = 0.85R` bot przestawia SL na cenę wejścia + `AUTO_BE_OFFSET_PIPS = 0.2 pips` (pokrywa prowizję i swapy brokera).
2. **Rollover SL Shield (`ENABLE_ROLLOVER_SL_SHIELD=True`):**
   - W oknie rolloveru (23:40 - 00:30 czasu serwera MT5) spready brokerów drastycznie rosną (nawet 10-20x), co powoduje sztuczne wybijanie SL.
   - Bot tymczasowo odsuwa SL na poziom **Disaster SL (2.5R)**, a po unormowaniu spreadu (< 1.5x średniej) automatycznie przywraca pierwotny SL.
   - Posiada zabezpieczenie MID-price: jeśli realna cena rynkowa (średnia bid/ask) dotknie oryginalnego SL, zlecenie jest natychmiast zamykane rynkowo.
3. **Piramidowanie Wielopoziomowe (Scale-In):**
   - Pozwala dołożyć pozycję (Poziom L2, L3) do silnego trendu tylko wtedy, gdy poprzednia pozycja zarobiła min. `+1.0R`, a jej zysk został trwale zablokowany (SL powyżej wejścia).
   - Wielkość dokładek maleje (`PYRAMID_LEVEL_DECAY = {2: 0.50, 3: 0.25}`).
   - Wszystkie pozycje w klastrze otrzymują wspólny zsynchronizowany SL i Master TP.

---

## 6. Zasady Zarządzania Ryzykiem i Reguły Prop Firm

Bot posiada wbudowane twarde reguły bezpieczeństwa:
1. **Ryzyko na transakcję:**
   - Domyślnie `0.5%` w trybie Prop Firm (`PROP_RISK_PER_TRADE_PERCENT`), max `1.0%` w trybie standardowym.
   - Wzór wyliczania lota uwzględnia walutę kwotowaną, tick size, tick value oraz kroki brokera (`min_lot`, `max_lot`, `lot_step`).
2. **Twarde podłogi minimalnego Stop Lossa (Min SL Floors):**
   - Zabezpieczenie przed mikro-stopami poniżej naturalnego szumu rynkowego:
     - Standardowy Forex: **min. 14 pips**
     - Pary z JPY: **min. 18 pips**
     - Pary z GBP: **min. 20 pips**
     - Złoto (XAUUSD): **min. 300 pips ($30.00)**
3. **Maksymalny Drawdown Konta (Prop Firm Mode):**
   - Statyczny max drawdown `10.0%`.
   - **Safety Buffer:** Jeśli strata konta osiągnie `7.5%`, bot natychmiast blokuje generowanie jakichkolwiek nowych pozycji.
4. **Limity Dywersyfikacji:**
   - Max 6 otwartych transakcji jednocześnie (`PROP_MAX_OPEN_TRADES = 6`).
   - Max 2 transakcje eksponowane na tę samą walutę bazową lub kwotowaną (`PROP_MAX_SAME_CURRENCY_TRADES = 2`).
5. **Filtr Przestrzeni Strukturalnej (`REQUIRE_STRUCTURAL_SPACE_FOR_RR`):**
   - Jeśli pomiędzy ceną wejścia a TP1 znajduje się potężna przeszkoda strukturalna (np. dołek fali Sell-Side Liquidity), która daje mniej niż 2.0R przestrzeni, setup jest odrzucany.

---

## 7. Baza Danych i Mechanizmy Self-Healing

- **Baza SQLite:** `trades.db`.
- **Tryb pracy:** `PRAGMA journal_mode=WAL;` oraz `PRAGMA synchronous=NORMAL;` z `PRAGMA busy_timeout=30000;`.
- **Wielowątkowość:** Dostęp do bazy chroniony jest wątkową blokadą `threading.RLock()`.
- **Automatyczne Samoleczenie (Self-Healing Decorator):**
  - Funkcje bazy udekorowane są `@_auto_recover_on_corruption`.
  - W razie błędu uszkodzenia pliku bazy (`database disk image is malformed`), silnik:
    1. Próbuje wykonać `REINDEX`.
    2. W razie niepowodzenia bezpiecznie archiwizuje uszkodzone pliki (`.corrupt_YYYYMMDD_HHMMSS`).
    3. Tworzy nową, czystą bazę danych.
    4. Uruchamia procedurę synchronizacji i odzyskuje historię oraz aktywne pozycje bezpośrednio z terminala MetaTrader 5!

---

## 8. Twarde Reguły Anty-Duplikacji Kodu (DRY & Single Source of Truth)

> [!CAUTION]
> **ZAKAZ POWIELANIA LOGIKI!** W tym projekcie każdy mechanizm ma swoje JEDNO dedykowane miejsce. Tworzenie kopii funkcji kalkulacji, parserów czy zapytań SQL jest traktowane jako błąd architektoniczny.

Przed napisaniem jakiejkolwiek funkcji sprawdź poniższą tabelę:

| Potrzebna Funkcjonalność | UŻYJ TEGO (Single Source of Truth) | ZAKAZANE (Anti-Patterns) |
| :--- | :--- | :--- |
| **Normalizacja symbolu brokera** (usuwanie `.pro`, `_i`, `m`) | `clean_symbol(symbol)` w `config.py` | Pisanie własnych `.replace(".pro", "")` w scannerze czy bocie. |
| **Pobranie wielkości pipsa** dla pary / złota | `get_symbol_pip_size(symbol, digits)` w `smc_detector.py` lub `mt5_client.py` | Dzielenie przez 10000 na sztywno bez uwzględnienia JPY i XAUUSD. |
| **Wyliczenie wielkości lota** dla danego SL i ryzyka | `mt5_client.calculate_lot_size(symbol, risk_amount, entry, sl)` | Samodzielne dzielenie ryzyka przez dystans w `telegram_bot.py`. |
| **Sprawdzenie czy można otworzyć trade** (limity walut, ryzyka, DD) | `mt5_client.can_open_new_trade(symbol, ...)` | Sprawdzanie `len(open_trades)` w pętli skanera. |
| **Kalkulacja Stop Lossa** (Structure, ATR, Hybrid) | `calculate_stop_loss()` w `smc_detector.py` | Samodzielne odejmowanie ATR od wejścia w innych modułach. |
| **Weryfikacja świecy reakcyjnej** (Rejection Wick) | `check_rejection_candle()` w `smc_detector.py` | Dodatkowe sprawdzanie knotów w `scanner.py`. |
| **Zapis lub aktualizacja transakcji** w bazie | Metody `TradeJournal` (`add_trade`, `update_trade_outcome`, itp.) | Bezpośrednie zapytania `sqlite3.connect()` poza `trade_journal.py`. |
| **Określenie aktywnej sesji / Killzone** | `SessionManager` (`get_current_session`, `is_in_killzone`) | Sprawdzanie `datetime.now().hour` w innych plikach. |
| **Sprawdzenie kalendarza wiadomości** | `news_filter.check_news(currencies)` | Odpytywanie zewnętrznych stron www poza `news_filter.py`. |
| **Rysowanie wykresów setupu** | `ChartGenerator.generate_setup_chart()` | Importowanie `mplfinance` i rysowanie wewnątrz bota Telegrama. |
| **Wyciszenie powtarzających się alertów** | `telegram_bot.mute_symbol()` / `is_symbol_muted()` | Tworzenie lokalnych słowników cooldownów w scannerze. |

---

## 9. Procedura Weryfikacji i Testowania

Przed wdrożeniem jakiejkolwiek zmiany w kodzie:
1. Uruchom pełen pakiet testów jednostkowych:
   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -s tests
   ```
2. Upewnij się, że:
   - Wszystkie testy kończą się wynikiem `OK`.
   - Testy nie pozostawiają otwartych procesów MT5 ani wiszących plików tymczasowych baz danych.
3. W przypadku modyfikacji bazy danych lub struktury `TradeSetup`:
   - Zaktualizuj schemat tabeli w `trade_journal.py` oraz mapowanie pól w `notion_journal.py`.
   - Zaktualizuj testy w `tests/test_core.py` oraz `tests/test_journal_recovery.py`.

---

## 10. Standard Konfiguracji i Samodokumentacji Pliku `.env.example`

Każda zmiana, rozszerzenie lub dodanie nowej zmiennej konfiguracyjnej w projekcie (w `config.py` oraz `.env`) bezwzględnie wymaga zaktualizowania pliku referencyjnego `.env.example` zgodnie z poniższym standardem.

### 10.1. Obowiązkowy Komentarz dla Każdej Zmiennej
> [!IMPORTANT]
> **ZAKAZ DODAWANIA ZMIENNYCH BEZ WYJAŚNIENIA!**
> Przy dopisywaniu lub modyfikacji jakiejkolwiek flagi/parametru w `.env.example` **każda zmienna musi posiadać jednoznaczny komentarz wyjaśniający** (inline `# ...` lub tuż nad zmienną), opisujący:
> 1. **Co dokładnie robi dany parametr** i jaki ma wpływ na trading / logikę bota.
> 2. **Dopuszczalne wartości i jednostki** (np. `True/False`, pipsy, sekundy, mnożnik ATR, wielokrotność R).
> 3. **Domyślne lub rekomendowane zachowanie**.

### 10.2. Wzorzec Formatowania
Format zapisu w pliku `.env.example`:
```env
# Przykład 1: Flagi logiczne (True/False)
USE_OPTIMAL_RR=True # Uzywanie optymalnego RR (Take Profit wyliczany automatycznie z MFE w trades.db)

# Przykład 2: Limity i progi liczbowe
MIN_TRADES_FOR_OPTIMAL_RR=30 # Minimalna liczba transakcji w bazie przed aktywacją Best RR
PROP_MAX_DRAWDOWN_PERCENT=10.0 # Maksymalny statyczny drawdown konta w % (wymóg Prop Firm)

# Przykład 3: Tryby działania
SL_CALCULATION_MODE=HYBRID # Tryb wyliczania Stop Lossa: HYBRID (struktura + bufor ATR), STRUCTURE lub ATR
```

### 10.3. Cel Biznesowy: "Year-Later Readiness" (Gotowość na powrót po roku)
Zasada ta ma kluczowe znaczenie długoterminowe:
- Gdy trader lub programista wróci do repozytorium np. **za rok**, natychmiast wie, do czego służy każda linijka w pliku konfiguracyjnym, bez potrzeby przekopywania kodu źródłowego w `config.py` czy czytania commitów.
- Zapewnia pojedyncze, przejrzyste źródło prawdy konfiguracyjnej dla osób wdrażających bota na nowym serwerze VPS.

### 10.4. Obowiązkowa Równoległa Synchronizacja z `.env`
> [!IMPORTANT]
> **ZASADA NATYCHMIASTOWEJ SYNCHRONIZACJI Z PLIKIEM `.env`!**
> Jeśli jakakolwiek nowa zmienna zostaje dopisana do `.env.example`, **musi zostać natychmiast dopisana również do lokalnego pliku `.env`**, aby konfiguracja była zawsze w 100% na bieżąco.
> - **Ochrona sekretów tradera:** Przy dopisywaniu zmiennych do `.env` pod żadnym pozorem nie wolno nadpisywać ani kasować istniejących kluczy (np. `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `NOTION_API_KEY`, niestandardowych list `SYMBOLS`).
> - Nowo dodawane parametry w `.env` otrzymują bezpieczne wartości domyślne (fallback) oraz ten sam czytelny komentarz wyjaśniający ich rolę.
> - Dzięki temu aktywne środowisko bota od razu widzi nowe funkcje bez konieczności ręcznego porównywania i przepisywania parametrów przez tradera.
