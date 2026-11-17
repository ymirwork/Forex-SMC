from datetime import datetime, timezone
import logging
import time
from typing import Dict, List, Optional, Tuple
import requests

logger = logging.getLogger("ForexSMC.NewsFilter")


class NewsFilter:
    """
    Monitors high-impact macroeconomic events (CPI, NFP, Rates, FOMC)
    using free keyless institutional calendar endpoints.
    Warns or filters setups when red-folder news is imminent (within window_minutes).
    """

    API_URL = "https://biquote.io/api/calendar?importance=high"
    CACHE_DURATION_SECONDS = 3600  # Refresh every hour

    def __init__(self, window_minutes: int = 45):
        self.window_minutes = window_minutes
        self._cached_events: List[Dict] = []
        self._last_fetch_time: float = 0.0

    def _extract_currencies(self, symbol: str) -> List[str]:
        """Extracts relevant currency codes from symbol (e.g. EURUSD -> ['EUR', 'USD'], XAUUSD -> ['USD'])."""
        clean = symbol.upper().replace(".F", "").replace("P", "").replace("_", "").replace("-", "")
        if "XAU" in clean or "GOLD" in clean:
            return ["USD"]
        if len(clean) >= 6:
            return [clean[:3], clean[3:6]]
        return ["USD"]

    def fetch_calendar(self, force: bool = False) -> List[Dict]:
        """Fetches high impact events with local caching."""
        now = time.time()
        if not force and self._cached_events and (now - self._last_fetch_time < self.CACHE_DURATION_SECONDS):
            return self._cached_events

        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            res = requests.get(self.API_URL, headers=headers, timeout=8)
            if res.status_code == 200:
                raw_data = res.json()
                parsed = []
                for item in raw_data:
                    try:
                        time_str = item.get("time", "")
                        if not time_str:
                            continue
                        # Standard ISO 8601 UTC
                        dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                        parsed.append({
                            "name": item.get("name", "High Impact Event"),
                            "currency": item.get("currency", "").upper(),
                            "importance": item.get("importance", "high"),
                            "datetime_utc": dt,
                            "forecast": str(item.get("forecast", "")),
                            "previous": str(item.get("previous", "")),
                        })
                    except Exception:
                        continue

                self._cached_events = parsed
                self._last_fetch_time = now
                logger.info(f"Economic calendar refreshed ({len(self._cached_events)} high impact events loaded).")
        except Exception as e:
            logger.warning(f"Failed to fetch economic calendar: {e}")

        return self._cached_events

    def check_news(self, symbol: str) -> Tuple[bool, Optional[str], Optional[Dict]]:
        """
        Checks if there is upcoming or recent High Impact news for the symbol.
        Returns (has_news, warning_text, event_dict).
        """
        events = self.fetch_calendar()
        if not events:
            return False, None, None

        currencies = self._extract_currencies(symbol)
        now_utc = datetime.now(timezone.utc)

        matching_events = [
            e for e in events
            if e["currency"] in currencies
        ]

        for e in matching_events:
            diff_minutes = (e["datetime_utc"] - now_utc).total_seconds() / 60.0

            # Window: between -15 min (recently released) and +window_minutes (upcoming)
            if -15 <= diff_minutes <= self.window_minutes:
                if diff_minutes > 0:
                    time_desc = f"za {int(diff_minutes)} min"
                else:
                    time_desc = f"{abs(int(diff_minutes))} min temu"

                warning = (
                    f"⚠️ **UWAGA RED FOLDER NEWS ({e['currency']}):**\n"
                    f"Wydarzenie: **{e['name']}** ({time_desc})\n"
                    f"Wysoka zmienność, ryzyko rozszerzenia spreadu i poślizgu cenowego."
                )
                return True, warning, e

        return False, None, None
