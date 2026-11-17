import asyncio
import logging
import time
from typing import Dict, List, Set

from chart_generator import ChartGenerator
from config import (
    ENABLE_SMC_STRATEGY,
    ENABLE_ZUMIKO_STRATEGY,
    HTF_TIMEFRAME,
    LOOKBACK_BARS,
    LTF_TIMEFRAME,
    MIN_CONFLUENCE_SCORE,
    SCAN_INTERVAL_SECONDS,
    SWING_LENGTH,
    SYMBOLS,
    ZUMIKO_FAST_EMA,
    ZUMIKO_REQUIRE_H1,
    ZUMIKO_SLOW_EMA,
    ZUMIKO_SWING_LEN,
    ZUMIKO_TREND_EMA,
)
from mt5_client import MT5Client
from smc_detector import SMCDetector, TradeSetup
from zumiko_detector import ZumikoDetector

logger = logging.getLogger("ForexSMC.Scanner")


class SMCScanner:
    """
    Multi-Strategy Market Scanner:
    1. Smart Money Concepts (M15 CHoCH + Fibo OTE >= 61.8% + Asian Sweep)
    2. Zumiko KillMLM Strategy (EMA 238 Trend + EMA 10/21 Cross + MTF Alignment)
    3. Golden Combo (Co-incident alignment of both SMC & Zumiko in the same direction)
    """

    def __init__(self, mt5_client: MT5Client, telegram_manager=None):
        self.mt5_client = mt5_client
        self.telegram = telegram_manager
        self.smc_detector = SMCDetector(
            swing_length=SWING_LENGTH,
            min_confluence_score=MIN_CONFLUENCE_SCORE,
            strict_htf_alignment=False,
        )
        self.zumiko_detector = ZumikoDetector(
            fast_ema_len=ZUMIKO_FAST_EMA,
            slow_ema_len=ZUMIKO_SLOW_EMA,
            trend_ema_len=ZUMIKO_TREND_EMA,
            swing_len=ZUMIKO_SWING_LEN,
            min_confluence_score=MIN_CONFLUENCE_SCORE,
            require_h1_alignment=ZUMIKO_REQUIRE_H1,
        )
        self.chart_gen = ChartGenerator()
        self.is_running = False
        self.notified_setups: Dict[str, float] = {}

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
        logger.info(
            f"Scanning {len(SYMBOLS)} symbols (SMC: {ENABLE_SMC_STRATEGY}, Zumiko: {ENABLE_ZUMIKO_STRATEGY})..."
        )

        for sym in SYMBOLS:
            try:
                # Fetch multi-timeframe candles
                df_m15 = self.mt5_client.fetch_ohlcv(sym, "M15", LOOKBACK_BARS)
                df_h1 = self.mt5_client.fetch_ohlcv(sym, "H1", LOOKBACK_BARS)
                df_h4 = self.mt5_client.fetch_ohlcv(sym, "H4", LOOKBACK_BARS)

                if df_m15 is None or df_h4 is None:
                    continue

                smc_setups: List[TradeSetup] = []
                zumiko_setups: List[TradeSetup] = []

                if ENABLE_SMC_STRATEGY:
                    smc_setups = self.smc_detector.scan_for_setups(sym, df_m15, df_h4)

                if ENABLE_ZUMIKO_STRATEGY:
                    zumiko_setups = self.zumiko_detector.scan_for_setups(sym, df_m15, df_h1, df_h4)

                final_setups: List[TradeSetup] = []

                # Check for Golden Combo (Both SMC & Zumiko agreed on direction)
                if smc_setups and zumiko_setups:
                    for s_setup in smc_setups:
                        matched_zumiko = next(
                            (z for z in zumiko_setups if z.direction == s_setup.direction),
                            None
                        )
                        if matched_zumiko:
                            # Upgrade to Golden Combo
                            s_setup.strategy_name = "GOLDEN_COMBO"
                            s_setup.setup_type = "Złoty Setup (SMC Fibo OTE + Zumiko MTF Aligned)"
                            s_setup.grade = "A+"
                            s_setup.confluence_score = min(100, max(s_setup.confluence_score, matched_zumiko.confluence_score) + 10)
                            s_setup.mtf_table = matched_zumiko.mtf_table
                            s_setup.ema_fast = matched_zumiko.ema_fast
                            s_setup.ema_slow = matched_zumiko.ema_slow
                            s_setup.ema_238 = matched_zumiko.ema_238
                            s_setup.swing_pattern = matched_zumiko.swing_pattern
                            # Append Zumiko factors to breakdown
                            s_setup.confluence_breakdown.extend(matched_zumiko.confluence_breakdown[:2])
                            final_setups.append(s_setup)
                            # Remove matched to avoid duplicate alert
                            zumiko_setups.remove(matched_zumiko)
                        else:
                            final_setups.append(s_setup)
                    # Add any remaining unmerged Zumiko setups
                    final_setups.extend(zumiko_setups)
                else:
                    final_setups.extend(smc_setups)
                    final_setups.extend(zumiko_setups)

                # Process final alerts
                for setup in final_setups:
                    setup_key = f"{setup.symbol}_{setup.direction}_{setup.strategy_name}_{setup.entry_price:.2f}"
                    if setup_key in self.notified_setups:
                        continue  # Already notified recently

                    found_count += 1
                    self.notified_setups[setup_key] = time.time()
                    setup_id = f"{sym}_{setup.strategy_name}_{int(time.time())}"

                    logger.info(
                        f"🔥 NEW SETUP: {setup.symbol} {setup.direction} [{setup.strategy_name}] ({setup.setup_type})"
                    )

                    # Generate visual chart with Fibo or EMAs
                    chart_path = self.chart_gen.generate_setup_chart(df_m15, setup)

                    # Send to Telegram
                    if self.telegram:
                        await self.telegram.send_setup_alert(setup, chart_path, setup_id)

            except Exception as e:
                logger.error(f"Error during scan of {sym}: {e}", exc_info=True)

        return found_count

    async def run_loop(self):
        """Main asynchronous scanner loop."""
        self.is_running = True
        logger.info(f"SMC & Zumiko Multi-Strategy Scanner loop started. Interval: {SCAN_INTERVAL_SECONDS}s")

        while self.is_running:
            try:
                await self.scan_once()
            except Exception as e:
                logger.error(f"Error in scanner loop: {e}", exc_info=True)

            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    def stop(self):
        self.is_running = False
        logger.info("Scanner stopped.")
