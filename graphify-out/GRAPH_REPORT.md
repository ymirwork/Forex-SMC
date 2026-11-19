# Graph Report - Forex-SMC  (2026-11-19)

## Corpus Check
- Corpus is ~40,293 words - fits in a single context window. You may not need a graph.

## Summary
- 533 nodes · 1344 edges · 19 communities (9 shown, 10 thin omitted)
- Extraction: 85% EXTRACTED · 15% INFERRED · 0% AMBIGUOUS · INFERRED: 204 edges (avg confidence: 0.88)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Core Strategy Orchestration & Scanner
- Database Recovery & SQLite Integrity
- Institutional Liquidity Sweep Engine
- Telegram Bot & Interactive Controls
- Trade Insights & Statistical Edge Metrics
- Dark Mode Setup Chart Visualizer
- MT5 Execution & Lot Size Engine
- Market Session & Timezone Manager
- Notion Trade Journal Integration
- ForexFactory High-Impact News Filter
- Application Lifecycle & Run Runner
- Best RR & MFE Optimizer
- Pip Sizing & Spread Guard
- MT5 Data Ingestion & OHLCV Feed
- Multi-Level Pyramiding & Cluster Scale-In
- Pyramid Cluster Test Verification
- Symbol Normalization & Currency Parser
- LuxAlgo MQL5 SMC Indicator

## God Nodes (most connected - your core abstractions)
1. `TradeJournal` - 59 edges
2. `MT5Client` - 56 edges
3. `TelegramBotManager` - 42 edges
4. `SMCScanner` - 33 edges
5. `SMCDetector` - 29 edges
6. `NewsFilter` - 27 edges
7. `sync_all()` - 25 edges
8. `_auto_recover_on_corruption()` - 24 edges
9. `NotionJournal` - 23 edges
10. `SessionManager` - 23 edges

## Surprising Connections (you probably didn't know these)
- `TestBreakEvenLogic` --uses--> `MT5Client`  [INFERRED]
  tests/test_core.py → mt5_client.py
- `TestCurrencyParsing` --uses--> `MT5Client`  [INFERRED]
  tests/test_core.py → mt5_client.py
- `TestPyramidCluster` --uses--> `MT5Client`  [INFERRED]
  tests/test_pyramid_cluster.py → mt5_client.py
- `TestSignalGuards` --uses--> `RROptimizer`  [INFERRED]
  tests/test_signal_guards.py → rr_optimizer.py
- `TestPyramidCluster` --uses--> `SMCScanner`  [INFERRED]
  tests/test_pyramid_cluster.py → scanner.py

## Import Cycles
- None detected.

## Communities (19 total, 10 thin omitted)

### Community 0 - "Core Strategy Orchestration & Scanner"
Cohesion: 0.07
Nodes (17): Best RR Optimizer (MFE Analysis), Golden Combo (SMC + Zumiko Confluence), Dynamic Risk & Position Sizing Management, Smart Money Concepts (SMC) Strategy, Telegram Semi-Auto Bot Interface, Zumiko KillMLM Strategy, clean_symbol(), RROptimizerResult (+9 more)

### Community 1 - "Database Recovery & SQLite Integrity"
Cohesion: 0.06
Nodes (9): main(), repair_database(), sync_all(), TestSyncJournalDirectionFallback, TestTradeJournalRecovery, TestRolloverJournalPersistence, _auto_recover_on_corruption(), wrapper() (+1 more)

### Community 2 - "Institutional Liquidity Sweep Engine"
Cohesion: 0.06
Nodes (6): LiquidityDetector, LiquidityEvaluation, SweptLevel, SMCDetector, TestLiquidityDetector, TestSignalGuards

### Community 3 - "Telegram Bot & Interactive Controls"
Cohesion: 0.07
Nodes (4): TelegramBotManager, TestAntiSpamAndSLModes, TestAuthorizationSecurity, TestTelegramCallbacksAndOrderExecution

### Community 4 - "Trade Insights & Statistical Edge Metrics"
Cohesion: 0.09
Nodes (3): EdgeMetrics, SetupEvaluation, TradeInsightEngine

### Community 5 - "Dark Mode Setup Chart Visualizer"
Cohesion: 0.10
Nodes (5): ChartGenerator, ConfluenceItem, TradeSetup, TestSMCDetectorLiquidityIntegration, ZumikoDetector

### Community 6 - "MT5 Execution & Lot Size Engine"
Cohesion: 0.12
Nodes (3): MT5Client, TestPropFirmRiskMin, TestPyramidMinLotRiskProtection

### Community 7 - "Market Session & Timezone Manager"
Cohesion: 0.09
Nodes (3): get_broker_utc_offset(), check_rejection_candle(), TestRiskAndFilters

### Community 10 - "Application Lifecycle & Run Runner"
Cohesion: 0.15
Nodes (4): main(), init_telegram_with_retry(), SMCScanner, TestScannerCandleCaching

## Knowledge Gaps
- **7 isolated node(s):** `Smart Money Concepts (SMC) Strategy`, `Zumiko KillMLM Strategy`, `Golden Combo (SMC + Zumiko Confluence)`, `Best RR Optimizer (MFE Analysis)`, `Dynamic Risk & Position Sizing Management` (+2 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 196 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **10 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `TradeJournal` connect `Database Recovery & SQLite Integrity` to `Core Strategy Orchestration & Scanner`, `Institutional Liquidity Sweep Engine`, `Telegram Bot & Interactive Controls`, `Trade Insights & Statistical Edge Metrics`, `Dark Mode Setup Chart Visualizer`, `ForexFactory High-Impact News Filter`, `Application Lifecycle & Run Runner`, `Pyramid Cluster Test Verification`?**
  _High betweenness centrality (0.212) - this node is a cross-community bridge._
- **Are the 8 inferred relationships involving `TradeJournal` (e.g. with `SMCScanner` and `SMCDetector`) actually correct?**
  _`TradeJournal` has 8 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Smart Money Concepts (SMC) Strategy`, `Zumiko KillMLM Strategy`, `Golden Combo (SMC + Zumiko Confluence)` to the rest of the system?**
  _7 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Core Strategy Orchestration & Scanner` be split into smaller, more focused modules?**
  _Cohesion score 0.07407407407407407 - nodes in this community are weakly interconnected._
- **Why does `MT5Client` connect `MT5 Execution & Lot Size Engine` to `Core Strategy Orchestration & Scanner`, `Database Recovery & SQLite Integrity`, `Telegram Bot & Interactive Controls`, `Trade Insights & Statistical Edge Metrics`, `Market Session & Timezone Manager`, `Application Lifecycle & Run Runner`, `MT5 Position Tracking & SL Modification`, `Pip Sizing & Spread Guard`, `MT5 Data Ingestion & OHLCV Feed`, `Multi-Level Pyramiding & Cluster Scale-In`, `Pyramid Cluster Test Verification`, `Symbol Normalization & Currency Parser`?**
  _High betweenness centrality (0.181) - this node is a cross-community bridge._
- **Are the 7 inferred relationships involving `MT5Client` (e.g. with `SMCScanner` and `TelegramBotManager`) actually correct?**
  _`MT5Client` has 7 INFERRED edges - model-reasoned connections that need verification._
- **Should `Database Recovery & SQLite Integrity` be split into smaller, more focused modules?**
  _Cohesion score 0.06278538812785388 - nodes in this community are weakly interconnected._