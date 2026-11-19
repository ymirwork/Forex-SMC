from datetime import datetime, timezone, timedelta, time as dtime
from typing import Dict, Optional, Tuple
import pandas as pd

from config import (
    BLOCK_EVENING_ENTRIES,
    BROKER_UTC_OFFSET_HOURS,
    EVENING_BLOCK_END_HOUR_UTC,
    EVENING_BLOCK_START_HOUR_UTC,
)


def get_broker_utc_offset() -> int:
    """
    Detects broker server timezone offset from UTC dynamically using MT5 tick time,
    or falls back to BROKER_UTC_OFFSET_HOURS from config (Audit finding 5).
    """
    try:
        import MetaTrader5 as mt5
        tick = mt5.symbol_info_tick("EURUSD") or mt5.symbol_info_tick("XAUUSD")
        if tick and tick.time > 0:
            now_utc = int(datetime.now(timezone.utc).timestamp())
            diff_hours = round((tick.time - now_utc) / 3600.0)
            if -12 <= diff_hours <= 14:
                return int(diff_hours)
    except Exception:
        pass
    return BROKER_UTC_OFFSET_HOURS


class SessionManager:
    """
    Detects active Institutional Trading Sessions (Killzones)
    and monitors Asian Range High/Low Liquidity Sweeps.
    """

    @staticmethod
    def get_current_session(dt: Optional[datetime] = None) -> Dict:
        """
        Determines current session and killzone based on UTC time.
        """
        offset_hours = get_broker_utc_offset()
        if dt is None:
            dt = datetime.now(timezone.utc)
        elif dt.tzinfo is None:
            # Adjust naive candle timestamp (broker server time) to UTC
            dt = (dt - timedelta(hours=offset_hours)).replace(tzinfo=timezone.utc)

        t = dt.time()

        # Session definitions in UTC
        # Asia: 00:00 - 07:00
        # London Open Killzone: 07:00 - 10:00
        # London Session: 07:00 - 15:00
        # New York Open Killzone: 12:00 - 15:00 (Overlap)
        # New York Session: 12:00 - 20:00
        # Post-NY / Rollover: 20:00 - 00:00

        is_london_killzone = dtime(7, 0) <= t <= dtime(10, 0)
        is_ny_killzone = dtime(12, 0) <= t <= dtime(15, 0)
        is_asian_session = dtime(0, 0) <= t < dtime(7, 0)
        is_rollover = dtime(20, 0) <= t or t < dtime(0, 15)

        # Evening entry block (e.g. 17:00 - 23:00 UTC) to avoid low-liquidity chop and midnight spread blowouts
        is_evening_blocked = BLOCK_EVENING_ENTRIES and (dtime(EVENING_BLOCK_START_HOUR_UTC, 0) <= t < dtime(EVENING_BLOCK_END_HOUR_UTC, 0))

        if is_london_killzone:
            session_name = "London Open Killzone"
            is_active_killzone = True
        elif is_ny_killzone:
            session_name = "New York Open Killzone"
            is_active_killzone = True
        elif dtime(10, 0) < t < dtime(12, 0):
            session_name = "London Continuation"
            is_active_killzone = False
        elif dtime(15, 0) < t <= dtime(17, 0):
            session_name = "New York Afternoon"
            is_active_killzone = False
        elif dtime(17, 0) < t <= dtime(20, 0):
            session_name = "New York Late / Evening"
            is_active_killzone = False
        elif is_asian_session:
            session_name = "Asian Session"
            is_active_killzone = False
        else:
            session_name = "Post-NY / Rollover (Low Liquidity)"
            is_active_killzone = False

        return {
            "session_name": session_name,
            "is_killzone": is_active_killzone,
            "is_rollover": is_rollover,
            "is_evening_blocked": is_evening_blocked,
            "time_utc": dt.strftime("%H:%M UTC"),
        }

    @staticmethod
    def calculate_asian_range(df: pd.DataFrame) -> Tuple[Optional[float], Optional[float]]:
        """
        Calculates the Asian Range High and Low for the most recent day (00:00 to 07:00 UTC).
        """
        if df is None or df.empty:
            return None, None

        # Slice recent bars to avoid unnecessary full-history copy
        df_copy = df.tail(80).copy()
        if "time" not in df_copy.columns:
            return None, None

        # Convert broker server time to true UTC
        offset_hours = get_broker_utc_offset()
        df_copy["time_utc"] = pd.to_datetime(df_copy["time"]) - pd.Timedelta(hours=offset_hours)
        latest_time = df_copy["time_utc"].iloc[-1]
        latest_date = latest_time.date()

        # If today's Asian session is still in progress (UTC < 07:00), we must use yesterday's completed Asian session
        if latest_time.hour < 7:
            asia_candles = pd.DataFrame()
        else:
            # Filter candles belonging to today's completed Asian session (00:00 - 07:00 UTC)
            asia_candles = df_copy[
                (df_copy["time_utc"].dt.date == latest_date) &
                (df_copy["time_utc"].dt.hour >= 0) &
                (df_copy["time_utc"].dt.hour < 7)
            ]

        if asia_candles.empty:
            # Fallback to the most recent previous trading day with completed Asian session data (e.g. yesterday, or Friday on Monday)
            unique_dates = sorted(df_copy["time_utc"].dt.date.unique(), reverse=True)
            for prev_date in unique_dates:
                if prev_date < latest_date:
                    past_asia = df_copy[
                        (df_copy["time_utc"].dt.date == prev_date) &
                        (df_copy["time_utc"].dt.hour >= 0) &
                        (df_copy["time_utc"].dt.hour < 7)
                    ]
                    if not past_asia.empty:
                        asia_candles = past_asia
                        break

        if not asia_candles.empty:
            asia_high = float(asia_candles["high"].max())
            asia_low = float(asia_candles["low"].min())
            return asia_high, asia_low

        return None, None

