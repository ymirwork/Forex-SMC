import asyncio
import logging
import time
from typing import Dict, Set

from chart_generator import ChartGenerator
from config import (
    HTF_TIMEFRAME,
    LOOKBACK_BARS,
    LTF_TIMEFRAME,
    MIN_CONFLUENCE_SCORE,
    SCAN_INTERVAL_SECONDS,
    SWING_LENGTH,
    SYMBOLS,
)
from mt5_client import MT5Client
from smc_detector import SMCDetector, TradeSetup

logger = logging.getLogger("ForexSMC.Scanner")


class SMCScanner:
    """Continuously monitors selected Forex and Gold pairs for high probability SMC setups."""

    def __init__(self, mt5_client: MT5Client, telegram_manager=None):
        self.mt5_client = mt5_client
        self.telegram = telegram_manager
        self.detector = SMCDetector(
            swing_length=SWING_LENGTH,
            min_confluence_score=MIN_CONFLUENCE_SCORE,
            strict_htf_alignment=False,
        )
        self.chart_gen = ChartGenerator()
        self.is_running = False
        self.notified_setups: Dict[str, float] = {}  # key -> timestamp

    def _cleanup_old_notifications(self, max_age_hours: float = 6.0):
        """Clears old sent setup keys so memory does not leak."""
        now = time.time()
        expired = [k for k, ts in self.notified_setups.items() if (now - ts) > (max_age_hours * 3600)]
        for k in expired:
            del self.notified_setups[k]

    async def scan_once(self) -> int:
        """Runs a single scan across all configured symbols."""
        if not self.mt5_client.ensure_connection():
            logger.error("MT5 connection unavailable during scan.")
            return 0

        self._cleanup_old_notifications()
        found_count = 0
        logger.info(f"Scanning {len(SYMBOLS)} symbols across {LTF_TIMEFRAME} with {HTF_TIMEFRAME} trend...")

        for sym in SYMBOLS:
            try:
                df_ltf = self.mt5_client.fetch_ohlcv(sym, LTF_TIMEFRAME, LOOKBACK_BARS)
                df_htf = self.mt5_client.fetch_ohlcv(sym, HTF_TIMEFRAME, LOOKBACK_BARS)

                if df_ltf is None or df_htf is None:
                    continue

                setups = self.detector.scan_for_setups(sym, df_ltf, df_htf)
                for setup in setups:
                    # Create unique fingerprint for this specific setup zone
                    setup_key = f"{setup.symbol}_{setup.direction}_{setup.setup_type}_{setup.zone_bottom:.2f}_{setup.zone_top:.2f}"
                    if setup_key in self.notified_setups:
                        continue  # Already notified

                    found_count += 1
                    self.notified_setups[setup_key] = time.time()
                    setup_id = f"{sym}_{int(time.time())}"

                    logger.info(f"🔥 NEW SMC SETUP: {setup.symbol} {setup.direction} ({setup.setup_type})")

                    # Generate visual chart
                    chart_path = self.chart_gen.generate_setup_chart(df_ltf, setup)

                    # Send to Telegram
                    if self.telegram:
                        await self.telegram.send_setup_alert(setup, chart_path, setup_id)

            except Exception as e:
                logger.error(f"Error during scan of {sym}: {e}", exc_info=True)

        return found_count

    async def run_loop(self):
        """Main asynchronous scanner loop."""
        self.is_running = True
        logger.info(f"SMC Scanner loop started. Interval: {SCAN_INTERVAL_SECONDS}s")

        while self.is_running:
            try:
                await self.scan_once()
            except Exception as e:
                logger.error(f"Error in scanner loop: {e}", exc_info=True)

            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    def stop(self):
        self.is_running = False
        logger.info("SMC Scanner stopped.")
