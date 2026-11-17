from datetime import datetime, timezone, time as dtime
from typing import Dict, Optional, Tuple
import pandas as pd


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
        if dt is None:
            dt = datetime.now(timezone.utc)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

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
        is_post_ny = dtime(20, 0) <= t or t < dtime(0, 0)

        if is_london_killzone:
            session_name = "London Open Killzone"
            is_active_killzone = True
        elif is_ny_killzone:
            session_name = "New York Open Killzone"
            is_active_killzone = True
        elif dtime(10, 0) < t < dtime(12, 0):
            session_name = "London Continuation"
            is_active_killzone = False
        elif dtime(15, 0) < t <= dtime(20, 0):
            session_name = "New York Afternoon"
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
            "time_utc": dt.strftime("%H:%M UTC"),
        }

    @staticmethod
    def calculate_asian_range(df: pd.DataFrame) -> Tuple[Optional[float], Optional[float]]:
        """
        Calculates the Asian Range High and Low for the most recent day (00:00 to 07:00 UTC).
        """
        if df is None or df.empty:
            return None, None

        df_copy = df.copy()
        if "time" not in df_copy.columns:
            return None, None

        # Convert to UTC if needed
        df_copy["time_utc"] = pd.to_datetime(df_copy["time"])
        latest_date = df_copy["time_utc"].iloc[-1].date()

        # Filter candles belonging to today's Asian session
        asia_candles = df_copy[
            (df_copy["time_utc"].dt.date == latest_date) &
            (df_copy["time_utc"].dt.hour >= 0) &
            (df_copy["time_utc"].dt.hour < 7)
        ]

        if asia_candles.empty:
            # Fallback to previous day's Asian session
            prev_date = latest_date - pd.Timedelta(days=1)
            asia_candles = df_copy[
                (df_copy["time_utc"].dt.date == prev_date) &
                (df_copy["time_utc"].dt.hour >= 0) &
                (df_copy["time_utc"].dt.hour < 7)
            ]

        if not asia_candles.empty:
            asia_high = float(asia_candles["high"].max())
            asia_low = float(asia_candles["low"].min())
            return asia_high, asia_low

        return None, None

    @classmethod
    def check_asian_sweep(
        cls,
        df: pd.DataFrame,
        direction: str,
    ) -> Tuple[bool, str]:
        """
        Detects if today's London/NY price action swept the Asian session liquidity pool.
        For BUY: price dipped below Asian Low and closed back above.
        For SELL: price spiked above Asian High and closed back below.
        """
        asia_high, asia_low = cls.calculate_asian_range(df)
        if asia_high is None or asia_low is None:
            return False, "Brak danych sesji azjatyckiej"

        recent_bars = df.tail(20)
        current_price = df["close"].iloc[-1]

        if direction.upper() == "BUY":
            min_recent_low = recent_bars["low"].min()
            # If price dipped below Asian low and is now back above it
            if min_recent_low < asia_low and current_price >= asia_low:
                return True, f"Asian Low Swept ({asia_low:.2f} wybite i odrzucone)"
            return False, f"Brak sweepu Asian Low ({asia_low:.2f})"

        elif direction.upper() == "SELL":
            max_recent_high = recent_bars["high"].max()
            # If price spiked above Asian high and is now back below it
            if max_recent_high > asia_high and current_price <= asia_high:
                return True, f"Asian High Swept ({asia_high:.2f} wybite i odrzucone)"
            return False, f"Brak sweepu Asian High ({asia_high:.2f})"

        return False, "Nieokreślony"
