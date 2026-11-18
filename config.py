import os
from dotenv import load_dotenv

load_dotenv()

# Telegram Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Support single or multiple allowed Telegram IDs (e.g. channel and user ID)
raw_chat_id = os.getenv("TELEGRAM_CHAT_ID", "0")
ALLOWED_CHAT_IDS = [
    int(x.strip()) for x in raw_chat_id.split(",") if x.strip() and x.strip() != "0"
]
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

# Timeframe Settings
# HTF (Higher Timeframe) for Trend & Major Market Structure
HTF_TIMEFRAME = os.getenv("HTF_TIMEFRAME", "H4")  # H4 as primary HTF
# LTF (Lower Timeframe) for Entry Confirmations
LTF_TIMEFRAME = os.getenv("LTF_TIMEFRAME", "M15") # M15 as execution timeframe

# Risk Management Settings
DEFAULT_RISK_PERCENT = float(os.getenv("DEFAULT_RISK_PERCENT", "1.0"))  # 1% per trade
MAX_RISK_PERCENT = float(os.getenv("MAX_RISK_PERCENT", "2.0"))          # Max allowed risk
DEFAULT_RR_RATIO = float(os.getenv("DEFAULT_RR_RATIO", "2.0"))          # Default Risk:Reward ratio (1:2)

# Dynamic / Adaptive Best RR Settings
USE_OPTIMAL_RR = os.getenv("USE_OPTIMAL_RR", "True").lower() in ("true", "1")
MIN_TRADES_FOR_OPTIMAL_RR = int(os.getenv("MIN_TRADES_FOR_OPTIMAL_RR", "5"))

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

# Zumiko KillMLM Strategy Settings
ZUMIKO_FAST_EMA = int(os.getenv("ZUMIKO_FAST_EMA", "10"))
ZUMIKO_SLOW_EMA = int(os.getenv("ZUMIKO_SLOW_EMA", "21"))
ZUMIKO_TREND_EMA = int(os.getenv("ZUMIKO_TREND_EMA", "238"))
ZUMIKO_SWING_LEN = int(os.getenv("ZUMIKO_SWING_LEN", "10"))
ZUMIKO_REQUIRE_H1 = os.getenv("ZUMIKO_REQUIRE_H1", "True").lower() in ("true", "1")

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
# SCALABLE RISK & TIGHT SL MANAGEMENT
# ==========================================
# Ciasny SL oparty na świecy reakcyjnej i strefie OTE / EMA 10/21
TIGHT_SL_MODE = os.getenv("TIGHT_SL_MODE", "True").lower() in ("true", "1")
# Dopuszczalne odchylenie ryzyka % przy zaokrągleniu do min. lota 0.01 (1.35 = max +35% ponad cel %)
MAX_RISK_DEVIATION_TOLERANCE = float(os.getenv("MAX_RISK_DEVIATION_TOLERANCE", "1.35"))
# Automatyczne przestawianie na Break-Even po osiągnięciu zysku +1.0R
AUTO_BE_AT_1R = os.getenv("AUTO_BE_AT_1R", "True").lower() in ("true", "1")

# ==========================================
# NOTION TRADE JOURNAL SETTINGS
# ==========================================
ENABLE_NOTION = os.getenv("ENABLE_NOTION", "False").lower() in ("true", "1")
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")

# ==========================================
# RISK-NEUTRAL PYRAMIDING SETTINGS (SCALE-IN)
# ==========================================
ENABLE_PYRAMIDING = os.getenv("ENABLE_PYRAMIDING", "True").lower() in ("true", "1")
MAX_PYRAMID_LEVEL = int(os.getenv("MAX_PYRAMID_LEVEL", "2"))               # Max 2 concurrent positions per symbol (Base + 1 scale-in)
PYRAMID_MIN_PROFIT_R = float(os.getenv("PYRAMID_MIN_PROFIT_R", "1.0"))     # Base position must have reached at least +1.0R
PYRAMID_LOCK_PREVIOUS_PROFIT = os.getenv("PYRAMID_LOCK_PREVIOUS_PROFIT", "True").lower() in ("true", "1") # Lock profit on parent trade




