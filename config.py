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
