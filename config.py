import os
from dotenv import load_dotenv

load_dotenv()

# Telegram Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Support single or multiple allowed Telegram IDs (e.g. channel and user ID)
raw_chat_id = os.getenv("TELEGRAM_CHAT_ID", "0")
ALLOWED_CHAT_IDS = []
for x in raw_chat_id.split(","):
    clean_val = x.strip().strip("'\"")
    if clean_val and clean_val != "0":
        try:
            ALLOWED_CHAT_IDS.append(int(clean_val))
        except ValueError:
            pass
# Primary destination channel for alerts (first negative ID, or first ID)
TELEGRAM_ALERT_CHANNEL = next(
    (cid for cid in ALLOWED_CHAT_IDS if cid < 0),
    ALLOWED_CHAT_IDS[0] if ALLOWED_CHAT_IDS else 0
)
# Backward-compatibility alias
TELEGRAM_CHAT_ID = TELEGRAM_ALERT_CHANNEL

# MetaTrader 5 Configuration
MT5_PATH = os.getenv("MT5_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")
MT5_LOGIN = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")

# Symbols to scan (Forex Majors + Gold)
DEFAULT_SYMBOLS = [
    "XAUUSD",   # Gold
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCAD",
    "NZDUSD",
    "USDCHF",
]

SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", ",".join(DEFAULT_SYMBOLS)).split(",") if s.strip()]


def clean_symbol(symbol: str) -> str:
    """
    Normalizes a broker symbol to standard clean uppercase ticker:
    e.g. 'EURUSD.pro' -> 'EURUSD', 'XAUUSD.raw' -> 'XAUUSD', 'EURUSDm' -> 'EURUSD', 'pro.EURUSD' -> 'EURUSD'.
    """
    if not symbol:
        return ""
    clean = symbol.upper().strip()
    for prefix in ["PRO.", "RAW.", "ECN.", "M.", "PRO_", "RAW_", "ECN_"]:
        if clean.startswith(prefix):
            clean = clean[len(prefix):]
            break
    for suffix in [".PRO", ".RAW", ".ECN", ".F", "_PRO", "_RAW", ".STP", ".C"]:
        if clean.endswith(suffix):
            clean = clean[:-len(suffix)]
            break
    # Single-char broker suffixes like EURUSDm or EURUSDp
    if len(clean) == 7 and clean.endswith(("M", "P", "C", "I", "S", "F")):
        clean = clean[:-1]
    clean = clean.replace("/", "").replace("_", "").replace("-", "").replace(".", "")
    return clean


def parse_currencies(symbol: str) -> tuple[str, str]:
    """
    Parses base and quote currency from standard Forex/Metals pairs.
    E.g. EURUSD -> ('EUR', 'USD'), XAUUSD -> ('XAU', 'USD'), USDJPY -> ('USD', 'JPY')
    """
    sym = clean_symbol(symbol)
    if not sym:
        return "", ""
    if "XAU" in sym or "GOLD" in sym:
        return "XAU", "USD"
    if "XAG" in sym or "SILVER" in sym:
        return "XAG", "USD"
    if len(sym) >= 6:
        return sym[:3], sym[3:6]
    return sym, ""


# Timeframe Settings
# HTF (Higher Timeframe) for Trend & Major Market Structure
HTF_TIMEFRAME = os.getenv("HTF_TIMEFRAME", "H4")  # H4 as primary HTF
# LTF (Lower Timeframe) for Entry Confirmations
LTF_TIMEFRAME = os.getenv("LTF_TIMEFRAME", "M15") # M15 as execution timeframe
# Broker Timezone Settings (MT5 server time offset from UTC, e.g. EET = UTC+2, EEST = UTC+3)
BROKER_UTC_OFFSET_HOURS = int(os.getenv("BROKER_UTC_OFFSET_HOURS", "2"))

# Risk Management Settings
DEFAULT_RISK_PERCENT = float(os.getenv("DEFAULT_RISK_PERCENT", "1.0"))  # 1% per trade
MAX_RISK_PERCENT = float(os.getenv("MAX_RISK_PERCENT", "2.0"))          # Max allowed risk
DEFAULT_RR_RATIO = float(os.getenv("DEFAULT_RR_RATIO", "2.0"))          # Default Risk:Reward ratio (1:2)

# Dynamic / Adaptive Best RR Settings
USE_OPTIMAL_RR = os.getenv("USE_OPTIMAL_RR", "True").lower() in ("true", "1")
MIN_TRADES_FOR_OPTIMAL_RR = int(os.getenv("MIN_TRADES_FOR_OPTIMAL_RR", "30"))
OPTIMAL_RR_MIN_FLOOR = float(os.getenv("OPTIMAL_RR_MIN_FLOOR", "2.0"))

# Rollover & Session Protection
# 1. New Trade Session Lock (UTC based: 20:00 - 00:00 UTC): blocks scanning & market orders during low-liquidity evening/rollover hours
BLOCK_ROLLOVER_SESSION = os.getenv("BLOCK_ROLLOVER_SESSION", "True").lower() in ("true", "1")

# 2. Open Trade Rollover SL Shield (Broker Server Time: 23:40 - 00:30 MT5): protects already open positions from midnight spread spikes
ENABLE_ROLLOVER_SL_SHIELD = os.getenv("ENABLE_ROLLOVER_SL_SHIELD", "True").lower() in ("true", "1")
ROLLOVER_SHIELD_START_MINUTES = int(os.getenv("ROLLOVER_SHIELD_START_MINUTES", "20"))  # Min before D1 close (23:40 MT5)
ROLLOVER_SHIELD_END_MINUTES = int(os.getenv("ROLLOVER_SHIELD_END_MINUTES", "30"))      # Min after D1 open (00:30 MT5)
ROLLOVER_DISASTER_SL_R = float(os.getenv("ROLLOVER_DISASTER_SL_R", "2.5"))              # Disaster SL distance in R
ROLLOVER_MAX_SPREAD_MULT = float(os.getenv("ROLLOVER_MAX_SPREAD_MULT", "1.5"))          # Max spread multiplier before restoring SL
ROLLOVER_MAX_SPREAD_PIPS = float(os.getenv("ROLLOVER_MAX_SPREAD_PIPS", "3.0"))          # Absolute max spread floor in pips for Forex majors
ROLLOVER_MAX_SPREAD_GOLD_PIPS = float(os.getenv("ROLLOVER_MAX_SPREAD_GOLD_PIPS", "25.0")) # Max spread floor in pips for Gold ($2.50)

# Max SL & Structural Room Filtering
MAX_SL_ATR_MULTIPLIER = float(os.getenv("MAX_SL_ATR_MULTIPLIER", "2.5"))
REQUIRE_STRUCTURAL_SPACE_FOR_RR = os.getenv("REQUIRE_STRUCTURAL_SPACE_FOR_RR", "True").lower() in ("true", "1")

# ==========================================
# ADVANCED SMC & SMART RISK SETTINGS (YouTube Smart Risk Mentorship)
# ==========================================
# 1. Strong vs Weak Structural Points (SL strictly behind Strong, TP aimed at Weak)
ENFORCE_STRONG_STRUCTURAL_SL = os.getenv("ENFORCE_STRONG_STRUCTURAL_SL", "False").lower() in ("true", "1")

# 2. Sweep-Engineered OB (Manipulation Grab / Smart Risk Hack #2)
REQUIRE_SWEEP_ENGINEERED_OB = os.getenv("REQUIRE_SWEEP_ENGINEERED_OB", "False").lower() in ("true", "1")
BONUS_POINTS_SWEEP_OB = int(os.getenv("BONUS_POINTS_SWEEP_OB", "15"))

# 3. Room-to-Run / Opposite S/D Barrier (Smart Risk Hack #1 & #5)
ENABLE_ROOM_TO_RUN_HTF_FILTER = os.getenv("ENABLE_ROOM_TO_RUN_HTF_FILTER", "True").lower() in ("true", "1")
ROOM_TO_RUN_HTF_MIN_RR = float(os.getenv("ROOM_TO_RUN_HTF_MIN_RR", "1.5"))

# Scanner Settings
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))   # Check every 60s
TRADE_MONITOR_INTERVAL_SECONDS = int(os.getenv("TRADE_MONITOR_INTERVAL_SECONDS", "15")) # Check open TP/SL every 15s
SWING_LENGTH = int(os.getenv("SWING_LENGTH", "5"))                      # Swing pivot length
LOOKBACK_BARS = int(os.getenv("LOOKBACK_BARS", "300"))                  # Bars to analyze

# Confluence & News Settings
MIN_CONFLUENCE_SCORE = int(os.getenv("MIN_CONFLUENCE_SCORE", "70"))    # Minimum score (>= 70%) to alert
BLOCK_ON_HIGH_IMPACT_NEWS = os.getenv("BLOCK_ON_HIGH_IMPACT_NEWS", "False").lower() in ("true", "1")
NEWS_WINDOW_MINUTES = int(os.getenv("NEWS_WINDOW_MINUTES", "45"))      # Check news +/- 45 mins

# Strategy Toggles (Hybrid Mode: SMC + Zumiko KillMLM)
ENABLE_SMC_STRATEGY = os.getenv("ENABLE_SMC_STRATEGY", "True").lower() in ("true", "1")
ENABLE_ZUMIKO_STRATEGY = os.getenv("ENABLE_ZUMIKO_STRATEGY", "True").lower() in ("true", "1")

# Zumiko KillMLM Strategy Settings (Official PDF & Webinar)
ZUMIKO_FAST_EMA = int(os.getenv("ZUMIKO_FAST_EMA", "9"))
ZUMIKO_SLOW_EMA = int(os.getenv("ZUMIKO_SLOW_EMA", "21"))
ZUMIKO_TREND_EMA = int(os.getenv("ZUMIKO_TREND_EMA", "238"))
ZUMIKO_SWING_LEN = int(os.getenv("ZUMIKO_SWING_LEN", "10"))
ZUMIKO_REQUIRE_H1 = os.getenv("ZUMIKO_REQUIRE_H1", "True").lower() in ("true", "1")
ZUMIKO_REQUIRE_VWAP_ALIGNMENT = os.getenv("ZUMIKO_REQUIRE_VWAP_ALIGNMENT", "True").lower() in ("true", "1")
ZUMIKO_RSI_MAX_BUY = float(os.getenv("ZUMIKO_RSI_MAX_BUY", "75.0"))
ZUMIKO_RSI_MIN_SELL = float(os.getenv("ZUMIKO_RSI_MIN_SELL", "25.0"))
ZUMIKO_USE_HEIKIN_ASHI_FILTER = os.getenv("ZUMIKO_USE_HEIKIN_ASHI_FILTER", "True").lower() in ("true", "1")
ZUMIKO_ENABLE_POC_TARGETING = os.getenv("ZUMIKO_ENABLE_POC_TARGETING", "True").lower() in ("true", "1")

# ==========================================
# PROP FIRM SETTINGS
# ==========================================
PROP_FIRM_MODE = os.getenv("PROP_FIRM_MODE", "True").lower() in ("true", "1")
PROP_INITIAL_BALANCE = float(os.getenv("PROP_INITIAL_BALANCE", "600.0"))
PROP_MAX_DRAWDOWN_PERCENT = float(os.getenv("PROP_MAX_DRAWDOWN_PERCENT", "10.0")) # 10% Static Max DD
PROP_SAFETY_BUFFER_PERCENT = float(os.getenv("PROP_SAFETY_BUFFER_PERCENT", "7.5")) # Halts new setups if DD reaches 7.5%
PROP_RISK_PER_TRADE_PERCENT = float(os.getenv("PROP_RISK_PER_TRADE_PERCENT", "0.5")) # 0.5% per trade
PROP_MAX_OPEN_TRADES = int(os.getenv("PROP_MAX_OPEN_TRADES", "6")) # Max 6 concurrent open trades
PROP_MAX_SAME_CURRENCY_TRADES = int(os.getenv("PROP_MAX_SAME_CURRENCY_TRADES", "2")) # Max 2 on same currency
PROP_ENFORCE_NEWS_FILTER = os.getenv("PROP_ENFORCE_NEWS_FILTER", "True").lower() in ("true", "1")

# ==========================================
# SCALABLE RISK & SL MANAGEMENT
# ==========================================
# Ciasny SL oparty na świecy reakcyjnej i strefie OTE / EMA 10/21
TIGHT_SL_MODE = os.getenv("TIGHT_SL_MODE", "True").lower() in ("true", "1")
# Dopuszczalne odchylenie ryzyka % przy zaokrągleniu do min. lota 0.01 (1.35 = max +35% ponad cel %)
MAX_RISK_DEVIATION_TOLERANCE = float(os.getenv("MAX_RISK_DEVIATION_TOLERANCE", "1.35"))
# Automatyczne przestawianie na Break-Even po osiągnięciu zysku (domyślnie +0.85R zamiast sztywnego 1.0R)
AUTO_BE_AT_1R = os.getenv("AUTO_BE_AT_1R", "True").lower() in ("true", "1")
AUTO_BE_R_TRIGGER = float(os.getenv("AUTO_BE_R_TRIGGER", "0.85"))
# Dodatkowy bufor w pipsach dodawany do Break-Even w stronę zysku (np. +0.2 pips na pokrycie prowizji, bez wybijania retestu)
AUTO_BE_OFFSET_PIPS = float(os.getenv("AUTO_BE_OFFSET_PIPS", "0.2"))
# Uwzględnianie spreadu brokera przy kalkulacji Stop Lossa (oddalenie SL o spread)
ADD_SPREAD_TO_SL = os.getenv("ADD_SPREAD_TO_SL", "True").lower() in ("true", "1")
# Domyślny spread w pipsach, jeśli MT5 nie zwróci danych
DEFAULT_SPREAD_PIPS = float(os.getenv("DEFAULT_SPREAD_PIPS", "1.0"))

# Tryb kalkulacji Stop Lossa: "STRUCTURE", "ATR" lub "HYBRID" (domyślnie HYBRID)
SL_CALCULATION_MODE = os.getenv("SL_CALCULATION_MODE", "HYBRID").upper()
# Mnożnik ATR dla czystego trybu ATR (np. 1.5 * ATR(14))
SL_ATR_MULTIPLIER = float(os.getenv("SL_ATR_MULTIPLIER", "1.5"))
# Minimalny bufor ATR dla trybu HYBRID (np. 0.8 * ATR(14))
SL_HYBRID_MIN_ATR_MULT = float(os.getenv("SL_HYBRID_MIN_ATR_MULT", "0.8"))

# Twarde minimalne progi odległości Stop Loss (ochrona przed mikro-stopami poniżej szumu rynkowego)
MIN_SL_PIPS_FOREX = float(os.getenv("MIN_SL_PIPS_FOREX", "14.0"))       # Standardowe pary walutowe (min 14 pips)
MIN_SL_PIPS_CROSS_JPY = float(os.getenv("MIN_SL_PIPS_CROSS_JPY", "18.0")) # Pary z JPY (min 18 pips)
MIN_SL_PIPS_GBP = float(os.getenv("MIN_SL_PIPS_GBP", "20.0"))           # Zmienne pary z GBP (min 20 pips)
MIN_SL_PIPS_GOLD = float(os.getenv("MIN_SL_PIPS_GOLD", "300.0"))        # Złoto XAUUSD (300 pips = $30.00)

# Potwierdzenie świecowe wejścia (Rejection Candle Filter)
REQUIRE_REJECTION_CANDLE = os.getenv("REQUIRE_REJECTION_CANDLE", "True").lower() in ("true", "1")
REJECTION_WICK_MIN_RATIO = float(os.getenv("REJECTION_WICK_MIN_RATIO", "0.25")) # Min. 25% knot odrzucenia lub świeca w kierunku setupu

# Blokada wejść wieczornych (sesja New York PM i spadek płynności 17:00-23:00 UTC)
BLOCK_EVENING_ENTRIES = os.getenv("BLOCK_EVENING_ENTRIES", "True").lower() in ("true", "1")
EVENING_BLOCK_START_HOUR_UTC = int(os.getenv("EVENING_BLOCK_START_HOUR_UTC", "17")) # 17:00 UTC = 18:00 CET
EVENING_BLOCK_END_HOUR_UTC = int(os.getenv("EVENING_BLOCK_END_HOUR_UTC", "23"))   # 23:00 UTC = 00:00 CET / 01:00 MT5

# ==========================================
# ANTI-SPAM & ALERT COOLDOWN SETTINGS
# ==========================================
# Minimalny czas (w minutach) pomiędzy kolejnymi alertami na tym samym symbolu i kierunku
ALERT_COOLDOWN_MINUTES = int(os.getenv("ALERT_COOLDOWN_MINUTES", "30"))
# Minimalny czas (w minutach) kwarantanny po uderzeniu w Stop Loss na danym symbolu (ochrona przed revenge tradingiem)
POST_LOSS_COOLDOWN_MINUTES = int(os.getenv("POST_LOSS_COOLDOWN_MINUTES", "60"))
# Czas (w godzinach) wyciszenia alertów po kliknięciu "Odrzuć" na Telegramie
DISMISS_MUTE_HOURS = float(os.getenv("DISMISS_MUTE_HOURS", "2.0"))

# ==========================================
# NOTION TRADE JOURNAL SETTINGS
# ==========================================
ENABLE_NOTION = os.getenv("ENABLE_NOTION", "False").lower() in ("true", "1")
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")

# ==========================================
# ADVANCED MULTI-LEVEL PYRAMIDING SETTINGS (SCALE-IN)
# ==========================================
ENABLE_PYRAMIDING = os.getenv("ENABLE_PYRAMIDING", "True").lower() in ("true", "1")
MAX_PYRAMID_LEVEL = int(os.getenv("MAX_PYRAMID_LEVEL", "3"))               # Max poziomy (np. 3: Baza + L2 + L3)
PYRAMID_EXECUTION_TIMEFRAME = os.getenv("PYRAMID_EXECUTION_TIMEFRAME", "M5") # Interwał szukania wejść dla dokładek (M5)
PYRAMID_MIN_PROFIT_R = float(os.getenv("PYRAMID_MIN_PROFIT_R", "1.0"))     # Każda poprzednia pozycja musi osiągnąć min. +1.0R
PYRAMID_MIN_RR_TO_TP = float(os.getenv("PYRAMID_MIN_RR_TO_TP", "1.0"))     # Minimalna przestrzeń do Master TP (min 1.0R dla M5)
PYRAMID_AUTO_EXECUTE = os.getenv("PYRAMID_AUTO_EXECUTE", "False").lower() in ("true", "1") # Auto wejście w MT5 vs przyciski TG
PYRAMID_MAX_RISK_FINANCE_RATIO = float(os.getenv("PYRAMID_MAX_RISK_FINANCE_RATIO", "0.75")) # Max % zablokowanego zysku na ryzyko dokładki (75%)
PYRAMID_ALLOW_MIN_LOT_FLOAT = os.getenv("PYRAMID_ALLOW_MIN_LOT_FLOAT", "True").lower() in ("true", "1") # Zezwolenie na min. lot 0.01 jeśli mieści się w zablokowanym zysku
PYRAMID_LOCK_PREVIOUS_PROFIT = os.getenv("PYRAMID_LOCK_PREVIOUS_PROFIT", "True").lower() in ("true", "1") # Lock profit on parent trade
PYRAMID_LEVEL_DECAY = {2: 0.50, 3: 0.25, 4: 0.125}                        # Mnożnik bazowego ryzyka dla dokładek
PYRAMID_GRADE_WEIGHTS = {"A+": 1.0, "A": 0.75, "B": 0.50}                  # Wagi Grade dla dokładek


# ==========================================
# ADVANCED INSTITUTIONAL LIQUIDITY SWEEPS
# ==========================================
ENABLE_LIQUIDITY_DETECTOR = os.getenv("ENABLE_LIQUIDITY_DETECTOR", "True").lower() in ("true", "1")
ENABLE_PDH_PDL_SWEEP = os.getenv("ENABLE_PDH_PDL_SWEEP", "True").lower() in ("true", "1")
ENABLE_PWH_PWL_SWEEP = os.getenv("ENABLE_PWH_PWL_SWEEP", "True").lower() in ("true", "1")
ENABLE_EQH_EQL_SWEEP = os.getenv("ENABLE_EQH_EQL_SWEEP", "True").lower() in ("true", "1")
ENABLE_ASIAN_SWEEP = os.getenv("ENABLE_ASIAN_SWEEP", "True").lower() in ("true", "1")
LIQUIDITY_SWEEP_LOOKBACK_BARS = int(os.getenv("LIQUIDITY_SWEEP_LOOKBACK_BARS", "15"))
EQH_EQL_ATR_MULT = float(os.getenv("EQH_EQL_ATR_MULT", "0.15"))
EQH_EQL_MIN_BARS_DISTANCE = int(os.getenv("EQH_EQL_MIN_BARS_DISTANCE", "5"))

# ==========================================
# DYNAMIC TRADE INSIGHTS & EDGE FILTER
# ==========================================
ENABLE_DYNAMIC_EDGE_FILTER = os.getenv("ENABLE_DYNAMIC_EDGE_FILTER", "True").lower() in ("true", "1")
# "ADVISORY" = warning badge + 50% risk reduction; "STRICT" = block signal entirely
DYNAMIC_FILTER_MODE = os.getenv("DYNAMIC_FILTER_MODE", "ADVISORY").upper()
# Minimum number of trades in a category before the engine issues a warning or restriction
INSIGHTS_MIN_SAMPLE_SIZE = int(os.getenv("INSIGHTS_MIN_SAMPLE_SIZE", "10"))
# Automatically send weekend insights report (Sunday before market open)
SEND_WEEKEND_INSIGHTS_REPORT = os.getenv("SEND_WEEKEND_INSIGHTS_REPORT", "True").lower() in ("true", "1")

# ==========================================
# ADAPTIVE POSITION MANAGEMENT & REGIME SETTINGS
# ==========================================
ENABLE_ADAPTIVE_MANAGER = os.getenv("ENABLE_ADAPTIVE_MANAGER", "True").lower() in ("true", "1")
ADAPTIVE_MODE = os.getenv("ADAPTIVE_MODE", "SHADOW").upper()  # 'SHADOW' (telemetria) lub 'LIVE' (realne modyfikacje w MT5)
ADAPTIVE_MIN_REGIME_SAMPLES = int(os.getenv("ADAPTIVE_MIN_REGIME_SAMPLES", "10"))  # Min próba na reżim (10 zagrań)
ADAPTIVE_M15_TRAILING = os.getenv("ADAPTIVE_M15_TRAILING", "True").lower() in ("true", "1")  # Trailing na bazie M15 Swing
ADAPTIVE_BE_TREND_R = float(os.getenv("ADAPTIVE_BE_TREND_R", "1.15"))      # Szerszy BE w silnym trendzie (pozwala na retest)
ADAPTIVE_BE_CHOP_R = float(os.getenv("ADAPTIVE_BE_CHOP_R", "0.80"))        # Szybki BE w konsolidacji / szumie
ADAPTIVE_BE_VOLATILE_R = float(os.getenv("ADAPTIVE_BE_VOLATILE_R", "0.85")) # BE przy podwyższonej zmienności
ADAPTIVE_TRAILING_MIN_STEP_R = float(os.getenv("ADAPTIVE_TRAILING_MIN_STEP_R", "0.40")) # Min. krok zysku do przesunięcia SL
ADAPTIVE_TRAILING_ATR_MULT = float(os.getenv("ADAPTIVE_TRAILING_ATR_MULT", "1.0"))       # Min. dystans SL od ceny (1.0 * ATR)

# ==========================================
# AI STRATEGY & TRADE AUDITOR SETTINGS
# ==========================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-pro")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
AUDIT_DEFAULT_RANGE = os.getenv("AUDIT_DEFAULT_RANGE", "30d")
ENABLE_SUNDAY_AUDIT_REPORT = os.getenv("ENABLE_SUNDAY_AUDIT_REPORT", "True").lower() in ("true", "1")
SUNDAY_AUDIT_HOUR_UTC = int(os.getenv("SUNDAY_AUDIT_HOUR_UTC", "19")) # 19:00 UTC = 20:00 CET







