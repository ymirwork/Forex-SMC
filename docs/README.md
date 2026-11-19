# Forex-SMC Documentation Hub 📚

Witaj w centralnym repozytorium wiedzy i dokumentacji technicznej projektu **Forex & Gold SMC Scanner + Telegram Semi-Auto Bot**.

Katalog `docs/` został stworzony po to, aby deweloperzy oraz agenci AI pracujący nad projektem:
1. **Nigdy się nie gubili** w modularnej strukturze projektu (ponad 500 wierzchołków w grafie zależności).
2. **Nie powielali kodu (zasada DRY – Don't Repeat Yourself)** i korzystali z pojedynczego źródła prawdy (Single Source of Truth).
3. **Maksymalizowali produktywność**, opierając się na jasnych rolach agentów, testach automatycznych oraz ustrukturyzowanym grafie wiedzy.

---

## 🗺️ Mapa Dokumentacji

| Dokument | Przeznaczenie | Grupa Odbiorców |
| :--- | :--- | :--- |
| **[PROJECT_CONTEXT.md](file:///d:/Github/Forex-SMC/docs/PROJECT_CONTEXT.md)** | **Kompletny kontekst projektu:** architektura, opis każdego modułu, modele domenowe (SMC, Zumiko, Płynność), zasady zarządzania ryzykiem, konfiguracja oraz twarde reguły anty-duplikacji kodu. | Deweloperzy, Wszyscy Agenci AI |
| **[AGENTS.md](file:///d:/Github/Forex-SMC/docs/AGENTS.md)** | **Instrukcja operacyjna dla Agentów AI:** 5 wyspecjalizowanych ról (Architekt, Quant/Strategia, Inżynier MT5, QA/Tester, Analityk/Telegram), protokoły pracy i checklisty weryfikacyjne. | Agenci AI (Antigravity / Subagenci) |
| **[ARCHITECTURE.md](file:///d:/Github/Forex-SMC/docs/ARCHITECTURE.md)** | **Wizualna architektura systemu:** diagramy sekwencji, cykl życia pozycji (State Machine), diagramy przepływu danych i relacje bazy SQLite. | Architekci, Inżynierowie |
| **[GRAPH_REPORT.md](file:///d:/Github/Forex-SMC/docs/GRAPH_REPORT.md)** | **Raport grafu wiedzy wygenerowany przez `/graphify`:** klastry kodu (19 społeczności), God Nodes, mosty między modułami i analiza spójności. | Agenci AI, Audytorzy Kodu |

---

## ⚡ Szybki Przewodnik Dla Nowego Agenta / Programisty

Gdy rozpoczynasz jakiekolwiek zadanie programistyczne w tym repozytorium:

1. **Zawsze najpierw przeczytaj sekcję Anty-Duplikacji w [PROJECT_CONTEXT.md](file:///d:/Github/Forex-SMC/docs/PROJECT_CONTEXT.md#8-twarde-reguły-anty-duplikacji-kodu-dry-single-source-of-truth).**
   - Nigdy nie twórz własnego kalkulatora wielkości lota – użyj `mt5_client.py`.
   - Nigdy nie normalizuj symboli ręcznie (`.pro`, `_i`, `m`) – użyj `clean_symbol()` z `config.py`.
   - Nigdy nie odpytuj bezpośrednio SQLite – użyj `TradeJournal`.
2. **Przyjmij odpowiednią rolę z [AGENTS.md](file:///d:/Github/Forex-SMC/docs/AGENTS.md).**
3. **Sprawdź istniejące zależności za pomocą grafu:**
   - Otwórz w przeglądarce [graph.html](file:///d:/Github/Forex-SMC/graphify-out/graph.html) lub skorzystaj z CLI `/graphify query "<pytanie>"`.
4. **Przed zatwierdzeniem zmian ZAWSZE uruchom pakiet testów:**
   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -s tests
   ```
   Wszystkie 70+ testów jednostkowych musi zakończyć się statusem `OK`.
