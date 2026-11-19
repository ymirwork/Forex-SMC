# Architektura Systemu i Diagramy Przepływu 📐

Niniejszy dokument przedstawia wizualną architekturę systemu **Forex-SMC**, relacje między modułami, cykl życia zlecenia oraz schemat przepływu danych.

---

## 1. Diagram Sekwencji: Od Detekcji Setupu do Egzekucji

Poniższy diagram ilustruje pełen obieg danych w systemie: od pobrania świec z terminala MT5, przez wielopoziomowe filtry, po wygenerowanie wykresu i interaktywne zatwierdzenie przez tradera na Telegramie.

```mermaid
sequenceDiagram
    autonumber
    participant MT5 as Terminal MetaTrader 5
    participant Scanner as SMCScanner (scanner.py)
    participant Det as Silnik Detekcji (SMC / Zumiko)
    participant Filter as Filtry Ryzyka (News / Rollover / Session)
    participant Chart as ChartGenerator (Dark Mode)
    participant TG as TelegramBotManager (telegram_bot.py)
    participant Trader as Użytkownik (Telegram Push)
    participant DB as TradeJournal (SQLite trades.db)

    loop Pętla skanowania (co 60s)
        Scanner->>MT5: Pobierz świece M15, H1, H4 (fetch_ohlcv)
        MT5-->>Scanner: Dane świecowe DataFrame
        Scanner->>Filter: Sprawdź filtry (Rollover, News, Godziny wieczorne)
        alt Filtr zablokowany
            Filter-->>Scanner: Odrzuć setup (skip)
        else Filtry otwarte
            Scanner->>Det: Analizuj strukturę (BOS, CHoCH, OTE, EMA, Sweepy)
            Det-->>Scanner: Zwróć TradeSetup (Score, Grade, SL, TP)
            alt Confluence Score >= 70%
                Scanner->>Chart: Wygeneruj wykres świecowy (PNG Dark Mode)
                Chart-->>Scanner: Ścieżka do pliku wykresu
                Scanner->>TG: Wyślij alert ze zdjęciem i przyciskami inline
                TG->>Trader: Powiadomienie Push z wykresem i przyciskami
                
                alt Trader klika [ Wejdź Market ]
                    Trader->>TG: Callback: market_<id>
                    TG->>MT5: Oblicz lot i wyślij order_send (BUY / SELL)
                    MT5-->>TG: Zlecenie zrealizowane (Ticket #)
                    TG->>DB: Zapisz otwarty trade do SQLite
                    TG->>Trader: Potwierdzenie egzekucji w czacie
                else Trader klika [ Złóż Limit ]
                    Trader->>TG: Callback: limit_<id>
                    TG->>MT5: Złóż zlecenie oczekujące (BUY_LIMIT / SELL_LIMIT)
                    MT5-->>TG: Zlecenie oczekujące przyjęte
                    TG->>Trader: Potwierdzenie zlecenia oczekującego
                else Trader klika [ Pomiń ]
                    Trader->>TG: Callback: dismiss_<id>
                    TG->>TG: Wycisz alerty dla symbolu (Mute na 2h)
                end
            end
        end
    end
```

---

## 2. Maszyna Stanów: Cykl Życia Pozycji (Trade State Machine)

Każde zagranie w systemie przechodzi przez ustrukturyzowany cykl stanów:

```mermaid
stateDiagram-v2
    [*] --> DETECTED: Skaner wykrywa setup (Score >= 70%)
    DETECTED --> ALERT_SENT: Wysłano alert na Telegram z wykresem
    
    ALERT_SENT --> REJECTED: Kliknięto [ Pomiń ] lub Timeout (6h)
    ALERT_SENT --> MARKET_EXECUTED: Kliknięto [ Wejdź Market ]
    ALERT_SENT --> LIMIT_PLACED: Kliknięto [ Złóż Limit ]
    
    LIMIT_PLACED --> MARKET_EXECUTED: Limit wypełniony przez rynek
    LIMIT_PLACED --> CANCELLED: Zlecenie anulowane ręcznie / wygasłe
    
    MARKET_EXECUTED --> IN_PROGRESS: Pozycja aktywna w MT5
    
    IN_PROGRESS --> BREAK_EVEN: Zysk osiąga >= +0.85R (SL na Entry + 0.2 pips)
    
    IN_PROGRESS --> ROLLOVER_SHIELDED: Godzina 23:40 MT5 (Poszerzony spread)
    BREAK_EVEN --> ROLLOVER_SHIELDED: Godzina 23:40 MT5 (Poszerzony spread)
    ROLLOVER_SHIELDED --> IN_PROGRESS: Spread unormowany (Przywrócono oryginalny SL)
    
    BREAK_EVEN --> PYRAMID_L2: Spełnione warunki dokładek (Scale-In)
    PYRAMID_L2 --> PYRAMID_L3: Dalszy silny trend (Kolejna dokładka)
    
    IN_PROGRESS --> CLOSED_TP: Osiągnięto Take Profit (TP1 / TP2 / Master TP)
    IN_PROGRESS --> CLOSED_SL: Osiągnięto Stop Loss
    BREAK_EVEN --> CLOSED_BE: Zamknięcie na poziomie Break-Even (+0.2 pips)
    ROLLOVER_SHIELDED --> CLOSED_MID_SL: Awaryjne zamknięcie MID-price podczas rolloveru
    
    CLOSED_TP --> JOURNALED: Zapisano do trades.db + Sync do Notion
    CLOSED_SL --> JOURNALED: Zapisano do trades.db + Sync do Notion
    CLOSED_BE --> JOURNALED: Zapisano do trades.db + Sync do Notion
    CLOSED_MID_SL --> JOURNALED: Zapisano do trades.db + Sync do Notion
    
    JOURNALED --> [*]
    REJECTED --> [*]
    CANCELLED --> [*]
```

---

## 3. Matryca Multi-Timeframe Alignment (MTF Confluence)

System łączy trzy poziomy perspektywy rynkowej:

| Timeframe | Rola w Systemie | Wskaźniki i Obserwowane Elementy | Warunek Konieczny |
| :--- | :--- | :--- | :--- |
| **HTF (H4)** | **Nadrzędny Trend & Kontekst** | Trend strukturalny (Higher Highs / Lower Lows), Major Order Blocks | Zgodność kierunku trendu (+20% Confluence Score) |
| **MTF (H1)** | **Filtr Potwierdzenia Trendu** | Trend Zumiko KillMLM (`EMA 238`, `EMA 10/21`) | Bezwzględny wymóg: H1 musi potwierdzać kierunek dla Zumiko |
| **LTF (M15)** | **Egzekucja & Timing Wejścia** | Wybicie struktury CHoCH, Pullback Fibo OTE (61.8% - 78.6%), świeca reakcyjna, Asian Sweep | Generowanie precyzyjnych poziomów Entry, SL, TP1, TP2 |

---

## 4. Schemat Bazy Danych SQLite (`trades.db`)

Dziennik transakcji zapisuje pełną historię każdego zagrania wraz z metadanymi statystycznymi:

```mermaid
erDiagram
    TRADES {
        INTEGER id PK "Auto-inkrementowany klucz główny"
        TEXT timestamp "Data i czas otwarcia (ISO 8601)"
        TEXT symbol "Znormalizowany symbol (np. EURUSD, XAUUSD)"
        TEXT direction "Kierunek zlecenia: BUY lub SELL"
        REAL entry_price "Cena wejścia w pozycję"
        REAL sl_price "Cena Stop Lossa"
        REAL tp_price "Cena Take Profitu"
        REAL realized_r "Zrealizowany zysk/strata w jednostkach R"
        REAL max_r "Maksymalny osiągnięty zysk (MFE) w jednostkach R"
        REAL mae_r "Maksymalne obsunięcie w trakcie trwania (MAE) w R"
        INTEGER confluence_score "Wynik konfluencji w skali 0-100%"
        TEXT grade "Ocena setupu: A+, A, B"
        TEXT status "Status: OPEN, CLOSED, CANCELLED"
        TEXT chart_path "Ścieżka do zapisanego pliku wykresu PNG"
        INTEGER mt5_ticket "Numer zlecenia w terminalu MT5"
        TEXT comment "Komentarz / Notatki z trade'u"
        TEXT notion_page_id "Identyfikator zsynchronizowanej strony Notion"
        INTEGER parent_ticket "Ticket pozycji bazowej (w razie piramidowania)"
        INTEGER is_pyramid "Flaga czy trade jest dokładką (0 lub 1)"
        INTEGER pyramid_level "Poziom piramidy: 1 (Baza), 2 (L2), 3 (L3)"
        TEXT strategy "Strategia: SMC, ZUMIKO, GOLDEN_COMBO"
        TEXT session_name "Sesja: London, New York AM/PM, Asian"
        INTEGER is_killzone "Czy trade zawarty w Killzone (0 lub 1)"
        TEXT setup_type "Nazwa formacji (np. M15 CHoCH + Fibo OTE)"
        TEXT confluences_json "Szczegółowy zrzut wag konfluencji w JSON"
        TEXT closed_at "Data i czas zamknięcia pozycji"
        TEXT exit_reason "Powód wyjścia: TP hit, SL hit, BE hit, Rollover"
        INTEGER is_rollover_shielded "Czy trade korzystał z osłony rolloveru"
        REAL pnl_usd "Zysk lub strata netto w walucie konta (USD)"
    }
```

---

## 5. Przepływ Zarządzania Ryzykiem (Risk Flowchart)

```mermaid
flowchart TD
    A["Nowy Setup gotowy do egzekucji"] --> B{"Czy tryb Prop Firm aktywny?"}
    
    B -->|Tak| C["Pobierz reguły Prop Firm: Ryzyko = 0.5%"]
    B -->|Nie| D["Pobierz reguły Standard: Ryzyko = 1.0%"]
    
    C --> E{"Czy bieżący Drawdown >= 7.5%?"}
    E -->|Tak| F["BLOKADA: Przekroczono Safety Buffer! Odrzuć setup."]
    E -->|Nie| G{"Czy liczba otwartych pozycji >= 6?"}
    
    G -->|Tak| H["BLOKADA: Osiągnięto limit 6 otwartych pozycji!"]
    G -->|Nie| I{"Czy liczba pozycji z tą samą walutą >= 2?"}
    
    I -->|Tak| J["BLOKADA: Limit ekspozycji waluty osiągnięty (max 2)!"]
    I -->|Nie| K["Sprawdź odległość Stop Lossa (Min SL Floor)"]
    
    D --> K
    
    K --> L{"Czy SL >= Min SL Floor dla symbolu?<br/>(Forex 14p, JPY 18p, GBP 20p, Gold 300p)"}
    L -->|Nie| M["Koryguj SL do poziomu podłogi minimalnej"]
    L -->|Tak| N["Zachowaj oryginalny SL strukturalny / hybrydowy"]
    
    M --> O["Oblicz wielkość lota wg tick value i dystansu"]
    N --> O
    
    O --> P{"Czy zaokrąglenie do min. lota 0.01<br/>przekracza tolerancję ryzyka (+35%)?"}
    P -->|Tak| Q["BLOKADA: Zbyt małe konto na minimalny lot przy tym SL!"]
    P -->|Nie| R["Zezwól na wysłanie zlecenia do MT5"]
```
