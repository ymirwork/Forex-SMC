# Raport Grafu Wiedzy Codebase (Knowledge Graph Report) 🕸️

Wygenerowany przez narzędzie **/graphify** dla repozytorium **Forex-SMC**.

- **Wizualizacja interaktywna:** Otwórz w przeglądarce plik [graph.html](file:///d:/Github/Forex-SMC/graphify-out/graph.html).
- **Struktura JSON:** Plik [graph.json](file:///d:/Github/Forex-SMC/graphify-out/graph.json).

---

## 1. Podsumowanie Grafu

- **533 wierzchołki (nodes)** · **1344 krawędzie (edges)**
- **19 wykrytych społeczności (communities)**
- **Współczynnik ekstrakcji:** 85% EXTRACTED (strukturalny AST) · 15% INFERRED (semantyczny/zależności) · 0% AMBIGUOUS

---

## 2. Kluczowe Węzły Centralne (God Nodes)

Węzły o najwyższej liczbie połączeń w projekcie – stanowią rdzeń systemu:

1. **`TradeJournal`** – **59 krawędzi:**
   - Centralne repozytorium historii transakcji, łączące bazę danych, optymalizator Best-RR, silnik statystyczny, bota Telegrama i procedury recovery.
2. **`MT5Client`** – **56 krawędzi:**
   - Główny most łączący logikę Pythona z rynkiem finansowym i terminalem MetaTrader 5. Odpowiada za notowania, loty, zlecenia, Break-Even i Rollover Shield.
3. **`TelegramBotManager`** – **42 krawędzie:**
   - Interfejs użytkownika, dispatch alertów, odbiór callbacków, parsowanie komend administracyjnych.
4. **`SMCScanner`** – **33 krawędzie:**
   - Główna pętla orkiestracji – łączy pobieranie danych, detekcję formacji, filtry i dispatch powiadomień.
5. **`SMCDetector`** – **29 krawędzi:**
   - Silnik analityczny metodologii Smart Money Concepts, wyliczający konfluencje, strefy OTE, FVG i Order Blocks.
6. **`NewsFilter`** – **27 krawędzi:**
   - Zabezpieczenie przed wydarzeniami makroekonomicznymi High-Impact (ForexFactory).
7. **`sync_all()`** – **25 krawędzi:**
   - Moduł synchronizacji danych pomiędzy MT5, lokalną bazą SQLite oraz Notion.
8. **`_auto_recover_on_corruption()`** – **24 krawędzie:**
   - Dekorator samoleczenia bazy SQLite w razie awarii dysku lub nagłego wyłączenia zasilania.
9. **`NotionJournal`** – **23 krawędzie:**
   - Integracja z chmurowym dziennikiem Notion.
10. **`SessionManager`** – **23 krawędzie:**
    - Wykrywanie sesji (Azja, Londyn, Nowy Jork), godzin Killzones oraz spreadów rolloverowych.

---

## 3. Zidentyfikowane Klastry Kodu (Communities)

| ID | Nazwa Społeczności | Liczba Węzłów | Zakres Odpowiedzialności |
| :---: | :--- | :---: | :--- |
| **0** | **Core Strategy Orchestration & Scanner** | 81 | Orkiestracja strategii SMC, Zumiko, Golden Combo oraz pętla główna skanera. |
| **1** | **Database Recovery & SQLite Integrity** | 73 | Procedury samoleczenia, reindex, naprawa uszkodzonych plików `trades.db`. |
| **2** | **Institutional Liquidity Sweep Engine** | 63 | Identyfikacja poziomów PDH/PDL, PWH/PWL, EQH/EQL oraz sweepów sesji azjatyckiej. |
| **3** | **Telegram Bot & Interactive Controls** | 56 | Obsługa aplikacji Telegram, autoryzacja użytkowników, callbacki zleceń. |
| **4** | **Trade Insights & Statistical Edge Metrics** | 34 | Badanie przewagi statystycznej (EV, Win Rate) oraz dynamiczny filtr słabych par. |
| **5** | **Dark Mode Setup Chart Visualizer** | 29 | Generowanie wykresów świecowych mplfinance z naniesionymi strefami i poziomami. |
| **6** | **MT5 Execution & Lot Size Engine** | 26 | Matematyka wielkości lota, tick value, filling modes i egzekucja zleceń w MT5. |
| **7** | **Market Session & Timezone Manager** | 25 | Obliczanie stref czasowych brokera (UTC offset) i śledzenie sesji giełdowych. |
| **8** | **Notion Trade Journal Integration** | 23 | Mapowanie pól SQLite do bazy danych Notion w chmurze. |
| **9** | **ForexFactory High-Impact News Filter** | 19 | Pobieranie i buforowanie kalendarza ekonomicznego. |
| **10** | **Application Lifecycle & Run Runner** | 19 | Start aplikacji (`run.py`), wątki w tle, obsługa sygnałów zamknięcia. |
| **11** | **MT5 Position Tracking & SL Modification** | 18 | Monitorowanie otwartych pozycji, przestawianie na Break-Even. |
| **12** | **Best RR & MFE Optimizer** | 16 | Symulator Maximum Favorable Excursion i wyznaczanie optymalnego Take Profit. |
| **13** | **Pip Sizing & Spread Guard** | 14 | Przeliczanie pipsów i kontrola spreadu w czasie rzeczywistym. |
| **14** | **MT5 Data Ingestion & OHLCV Feed** | 11 | Połączenie z terminalem MT5 i pobieranie danych świecowych. |
| **15** | **Multi-Level Pyramiding & Cluster Scale-In** | 9 | Dokładanie pozycji do silnego trendu (L1, L2, L3) z blokowaniem zysku. |
| **16** | **Pyramid Cluster Test Verification** | 9 | Testy jednostkowe mechanizmów piramidowania. |
| **17** | **Symbol Normalization & Currency Parser** | 7 | Normalizacja nazw par walutowych (`clean_symbol`) i rozpoznawanie walut bazowych. |
| **18** | **LuxAlgo MQL5 SMC Indicator** | 1 | Plik wskaźnika `.ex5` / `.mq5` dla terminala MetaTrader 5. |

---

## 4. Jak Korzystać z Grafu w Codziennej Pracy?

Dzięki zainstalowanemu silnikowi `/graphify` możesz odpytywać graf bezpośrednio w konsoli lub podczas pracy z asystentem AI:

```powershell
# Szybkie pytanie o architekturę lub powiązania
uv tool run --from graphifyy graphify query "Gdzie wyliczany jest lot z uwzględnieniem tick value?"

# Odnalezienie najkrótszej ścieżki zależności między dwoma modułami
uv tool run --from graphifyy graphify path "SMCScanner" "TradeJournal"

# Wyjaśnienie roli danego komponentu
uv tool run --from graphifyy graphify explain "MT5Client"

# Otwarcie interaktywnej mapy grafu w przeglądarce
Start-Process graphify-out\graph.html
```
