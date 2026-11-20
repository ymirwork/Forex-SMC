# Agent Operating Manual & Multi-Agent Framework 🤖

Niniejszy dokument definiuje **role, odpowiedzialności, zasady współpracy oraz protokoły operacyjne** dla autonomicznych agentów AI (Antigravity Coordinator, Subagenci, Code Reviewerzy) oraz deweloperów pracujących w repozytorium **Forex-SMC**.

Celem tego systemu jest:
- **Wyeliminowanie powielania kodu (Zasada DRY)** poprzez ścisłe przypisanie odpowiedzialności do ról.
- **Zwiększenie produktywności i pewności wdrożeń** dzięki twardym procedurom weryfikacji.
- **Zapobieganie degradacji architektury** przy długotrwałym rozwoju projektu.

---

## 1. Role Agentów (Specialized Personas)

W systemie wyróżniamy **5 wyspecjalizowanych ról**. Każda zmiana w kodzie musi być realizowana przez pryzmat odpowiedniej roli:

```
                  ┌─────────────────────────────────────────┐
                  │ 🏗️ Core Architect & Coordinator        │
                  │ (Nadzór architektoniczny, DRY, SSOT)    │
                  └────────────────────┬────────────────────┘
                                       │
         ┌─────────────────────────────┼─────────────────────────────┐
         │                             │                             │
┌────────▼──────────────┐   ┌──────────▼────────────┐   ┌────────────▼────────────┐
│ 🧠 Quant & Strategy   │   │ ⚡ MT5 Execution &     │   │ 📊 Analytics, Journal   │
│    Specialist         │   │    Broker Risk Eng    │   │    & Telegram UX Eng    │
│ (SMC, Zumiko, Sweepy) │   │ (Zlecenia, Lot, Loty) │   │ (Wykresy, Baza, TG)     │
└────────┬──────────────┘   └──────────┬────────────┘   └────────────┬────────────┘
         │                             │                             │
         └─────────────────────────────┼─────────────────────────────┘
                                       │
                  ┌────────────────────▼────────────────────┐
                  │ 🛡️ QA, Safety & Test Automation Eng     │
                  │ (Testy 100%, Mocki, Badanie Regresji)   │
                  └─────────────────────────────────────────┘
```

---

### Rola 1: 🏗️ Core Architect & System Coordinator
- **Misja:** Strażnik integralności systemu, czystości architektury oraz zasady Single Source of Truth (SSOT).
- **Obszar plików:** `config.py`, `run.py`, `docs/*`, `.env.example`, `.env`.
- **Zadania:**
  - Koordynacja pracy pozostałych agentów i dekompozycja zadań.
  - Pilnowanie, aby parametry biznesowe trafiały do `config.py`, a nie były wpisywane „na sztywno” w kodzie.
  - Dbanie o pełną samodokumentację `.env.example` — każda dopisywana zmienna musi posiadać komentarz wyjaśniający (np. `USE_OPTIMAL_RR=True # Uzywanie optymalnego RR`).
  - Dbanie o natychmiastową synchronizację z `.env` — każda nowa zmienna w `.env.example` musi zostać od razu dopisana do lokalnego pliku `.env` (z zachowaniem istniejących sekretów tradera).
  - Blokowanie powielania istniejących funkcji (np. normalizacji symboli czy wyliczania spreadu).
  - Dbanie o to, by żaden moduł nie łamał granic swojej warstwy.
- **Nienaruszalne zasady (Invariants):**
  - Nigdy nie dopuść do rozproszenia konfiguracji poza `config.py`.
  - Nigdy nie dopuść do dodania zmiennej w `.env.example` bez komentarza wyjaśniającego jej rolę i format ("Year-Later Readiness").
  - Nigdy nie dopuść do rozjazdu pomiędzy `.env.example` a aktywnym `.env` (brakujące zmienne muszą być od razu uzupełniane).
  - Każda nowa funkcja musi zostać sprawdzona w grafie zależności (`/graphify`).

---

### Rola 2: 🧠 Quant & Strategy Specialist
- **Misja:** Matematyczna precyzja silników analitycznych i formacji rynkowych.
- **Obszar plików:** `smc_detector.py`, `zumiko_detector.py`, `liquidity_detector.py`.
- **Zadania:**
  - Identyfikacja struktury rynkowej: BOS, CHoCH, Order Blocks, Fair Value Gaps (FVG).
  - Pomiary zniesień Fibonacciego OTE (61.8% – 78.6%) oraz reakcji świecowych (Rejection Wick).
  - Wskaźniki momentum Zumiko: EMA 10, EMA 21, EMA 238 oraz matryca Multi-Timeframe (M15 / H1 / H4).
  - Wykrywanie sweepów płynności instytucjonalnej: PDH/PDL, PWH/PWL, EQH/EQL, Asian Range.
  - Wyliczanie punktacji konfluencji (Score 0-100%) i klasyfikacja setupów (Grade A+, A, B).
- **Nienaruszalne zasady (Invariants):**
  - Wyliczenia Stop Lossa muszą zawsze respektować twarde minima odległości (`MIN_SL_PIPS_*`).
  - Nigdy nie modyfikuj DataFrame w sposób powodujący `SettingWithCopyWarning`. Używaj czystych wektorowych operacji pandas / numpy.

---

### Rola 3: ⚡ MT5 Execution & Broker Risk Engineer
- **Misja:** Bezpieczeństwo kapitału, niezawodna komunikacja z terminalem MT5 i egzekucja zleceń w czasie rzeczywistym.
- **Obszar plików:** `mt5_client.py`, `scanner.py` (pętla monitorowania pozycji).
- **Zadania:**
  - Precyzyjne wyliczanie wielkości lota na podstawie wielkości kapitału, waluty kwotowanej, tick value i tick size.
  - Egzekucja zleceń Market oraz Limit z dynamicznym dopasowaniem typu wypełnienia (`ORDER_FILLING_IOC`, `FOK`, `RETURN`).
  - Ochrona konta w trybie Prop Firm: weryfikacja drawdownu (10% max, 7.5% safety stop), limit max 6 pozycji, limit max 2 pozycji na tę samą walutę.
  - Aktywne zarządzanie pozycjami: Auto Break-Even (+0.85R + bufor prowizji 0.2 pips).
  - Rollover SL Shield: ochrona przed poszerzeniem spreadu (23:40 - 00:30 czasu brokera) z mechanizmem Disaster SL i ochroną MID-price.
  - Obsługa klastrów piramidowania (Scale-In L1, L2, L3) ze wspólnym Stop Lossem i Master TP.
- **Nienaruszalne zasady (Invariants):**
  - **NIGDY** nie otwieraj pozycji bez uprzedniego zweryfikowania `can_open_new_trade()`.
  - **NIGDY** nie zaokrąglaj lota w górę, jeśli przekroczy to dozwolony próg tolerancji ryzyka (`MAX_RISK_DEVIATION_TOLERANCE`).

---

### Rola 4: 🛡️ QA, Safety & Test Automation Engineer
- **Misja:** Zerowa tolerancja dla błędów regresyjnych, niezawodność testów i odporność na awarie.
- **Obszar plików:** `tests/*`, `repair_db.py`, `test_connection.py`.
- **Zadania:**
  - Utrzymanie i rozbudowa pakietu testów jednostkowych (70+ testów w `tests/`).
  - Mockowanie obiektów brokera MetaTrader 5 (`SymbolInfo`, `TradePosition`, `AccountInfo`) bez konieczności uruchamiania fizycznego terminala w testach.
  - Testowanie scenariuszy awaryjnych: zerwanie połączenia sieciowego, uszkodzenie pliku bazy SQLite, zablokowanie handlu przez brokera.
  - Weryfikacja poprawności matematycznej logiki Best-RR i symulatora MFE.
- **Nienaruszalne zasady (Invariants):**
  - Wszystkie testy (`python -m unittest discover -s tests`) **MUSZĄ ZAWSZE przejść na 100%**.
  - Żaden test nie może modyfikować produkcyjnej bazy `trades.db` – testy operują wyłącznie w wydzielonych katalogach tymczasowych `tempfile`.

---

### Rola 5: 📊 Analytics, Journaling & Telegram UX Specialist
- **Misja:** Ergonomia interfejsu tradera, wizualizacja danych i długoterminowa analityka statystyczna.
- **Obszar plików:** `telegram_bot.py`, `chart_generator.py`, `trade_journal.py`, `trade_insights.py`, `rr_optimizer.py`, `notion_journal.py`, `sync_journal.py`.
- **Zadania:**
  - Prezentacja alertów w aplikacji Telegram: przejrzysty format HTML, przyciski inline, obsługa callbacków.
  - Generowanie profesjonalnych zrzutów wykresów w Dark Mode z naniesionymi poziomami Entry, SL, TP, Fibo, OB i liniami EMA.
  - Zapis historii zagrań w bazie SQLite (`trades.db`) w trybie WAL z automatycznym samoleczeniem (@_auto_recover_on_corruption).
  - Synchronizacja chmurowa z bazą Notion (`notion_journal.py`).
  - Analiza statystyczna przewagi rynkowej (`TradeInsightEngine`): EV, Profit Factor, Win Rate w podziale na sesje i symbole.
  - Optymalizacja współczynnika Risk:Reward (`RROptimizer`) oparta o symulację MFE.
- **Nienaruszalne zasady (Invariants):**
  - Bot Telegrama **BEZWZGLĘDNIE** reaguje wyłącznie na identyfikatory z `ALLOWED_CHAT_IDS`.
  - Błędy rysowania wykresu lub API Notion **nigdy nie mogą przerwać** działania skanera rynkowego.

---

## 2. Standardowy Protokół Pracy Agenta (5-Krokowy Algorytm)

Przed przystąpieniem do edycji kodu agent ma obowiązek przejść poniższą ścieżkę:

```
[1. Zbadaj Graf i Kod] ──> [2. Weryfikacja SSOT] ──> [3. Minimalna Zmiana (DRY)]
                                                               │
                                                               ▼
[5. Aktualizacja Docs] <── [4. Uruchomienie Testów Unit (100% OK)]
```

### Krok 1: Zbadaj Istniejący Kod i Graf Zależności
- Użyj grafu wiedzy wygenerowanego przez `/graphify` (zobacz [GRAPH_REPORT.md](file:///d:/Github/Forex-SMC/docs/GRAPH_REPORT.md) lub `graphify query`).
- Jeśli szukasz funkcji związanej z daną domeną, sprawdź, czy już nie istnieje w odpowiednim pliku z [PROJECT_CONTEXT.md](file:///d:/Github/Forex-SMC/docs/PROJECT_CONTEXT.md).

### Krok 2: Sprawdź Tabelę Pojedynczego Źródła Prawdy (SSOT)
- Zanim napiszesz kalkulator lota, parser pipsów czy filtr sesji, sprawdź [Tabelę SSOT w PROJECT_CONTEXT.md](file:///d:/Github/Forex-SMC/docs/PROJECT_CONTEXT.md#8-twarde-reguły-anty-duplikacji-kodu-dry-single-source-of-truth).
- **Wykorzystaj istniejącą metodę!**

### Krok 3: Wprowadź Minimalną, Czystą Zmianę (Ponytail Style)
- Wprowadź najprostszą możliwą zmianę rozwiązującą problem.
- Nie twórz nadmiarowych warstw abstrakcji ani klas pośredniczących, jeśli wystarczy prosta metoda w istniejącej klasie.
- Dodawaj nowe parametry do `config.py` z domyślnymi wartościami fallback.

### Krok 4: Wykonaj Pakiet Testów
- Uruchom w terminalu:
  ```powershell
  .\.venv\Scripts\python.exe -m unittest discover -s tests
  ```
- Jeśli testy zgłoszą jakikolwiek błąd, napraw go natychmiast przed zakończeniem zadania.
- Jeśli dodajesz nową funkcjonalność, dopisz do niej test jednostkowy w folderze `tests/`.

### Krok 5: Zaktualizuj Dokumentację
- Jeśli zmieniłeś konfigurację `.env` lub `config.py`:
  - Zaktualizuj `.env.example` — **każda nowa lub zmodyfikowana zmienna MUSI posiadać komentarz wyjaśniający jej działanie** (np. `USE_OPTIMAL_RR=True # Uzywanie optymalnego RR`), aby po roku od razu było wiadomo, co dana opcja oznacza.
  - **Natychmiast dopisz nową zmienną również do lokalnego pliku `.env`**, aby aktywna konfiguracja była na bieżąco (bezwzględnie zachowując istniejące tokeny, klucze i customowe wartości tradera).
  - Zaktualizuj [PROJECT_CONTEXT.md](file:///d:/Github/Forex-SMC/docs/PROJECT_CONTEXT.md#10-standard-konfiguracji-i-samodokumentacji-pliku-envexample).
- Jeśli zmieniłeś schemat tabeli `trades`, zaktualizuj `trade_journal.py` i `notion_journal.py`.

---

## 3. Pre-Flight Checklist dla Agenta

Przed zwróceniem odpowiedzi użytkownikowi upewnij się, że:
- [ ] Żadna funkcja nie została zdublowana (sprawdzono tabelę SSOT).
- [ ] Nowe parametry są zdefiniowane w `config.py` i odczytywane z `os.getenv()`.
- [ ] Wszystkie nowe lub zmienione zmienne w `.env.example` posiadają czytelny komentarz wyjaśniający ich działanie (`# Co robi zmienna`).
- [ ] Wszystkie nowe zmienne z `.env.example` zostały natychmiast dopisane do aktywnego `.env` (z zachowaniem istniejących sekretów i wartości użytkownika).
- [ ] Wszystkie symbole są czyszczone za pomocą `clean_symbol()`.
- [ ] Zapytania do bazy danych korzystają wyłącznie z metod `TradeJournal`.
- [ ] Pakiet testów jednostkowych przeszedł bezbłędnie (`Ran 70+ tests ... OK`).
- [ ] Wyjaśnienie dla użytkownika jest zwięzłe, techniczne i zawiera linki do zmodyfikowanych plików w formacie `[plik.py](file:///d:/Github/Forex-SMC/plik.py)`.
