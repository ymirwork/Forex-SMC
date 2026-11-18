import asyncio
import logging
import time
from typing import Dict, List, Set

from chart_generator import ChartGenerator
from config import (
    AUTO_BE_AT_1R,
    ENABLE_SMC_STRATEGY,
    ENABLE_ZUMIKO_STRATEGY,
    HTF_TIMEFRAME,
    LOOKBACK_BARS,
    LTF_TIMEFRAME,
    MIN_CONFLUENCE_SCORE,
    PROP_FIRM_MODE,
    PROP_MAX_DRAWDOWN_PERCENT,
    PROP_MAX_OPEN_TRADES,
    PROP_SAFETY_BUFFER_PERCENT,
    SCAN_INTERVAL_SECONDS,
    TRADE_MONITOR_INTERVAL_SECONDS,
    SWING_LENGTH,
    SYMBOLS,
    ZUMIKO_FAST_EMA,
    ZUMIKO_REQUIRE_H1,
    ZUMIKO_SLOW_EMA,
    ZUMIKO_SWING_LEN,
    ZUMIKO_TREND_EMA,
)
from mt5_client import MT5Client
from notion_journal import NotionJournal
from smc_detector import SMCDetector, TradeSetup
from trade_journal import TradeJournal
from zumiko_detector import ZumikoDetector

logger = logging.getLogger("ForexSMC.Scanner")


class SMCScanner:
    """
    Multi-Strategy Market Scanner:
    1. Smart Money Concepts (M15 CHoCH + Fibo OTE >= 61.8% + Asian Sweep)
    2. Zumiko KillMLM Strategy (EMA 238 Trend + EMA 10/21 Cross + MTF Alignment)
    3. Golden Combo (Co-incident alignment of both SMC & Zumiko in the same direction)
    4. Active Position Lock: Automatically locks symbols with open trades / pending orders until closed.
    5. Real-Time Trade Monitor: Detects TP / SL hits, logs results, and sends notifications.
    """

    def __init__(self, mt5_client: MT5Client, telegram_manager=None):
        self.mt5_client = mt5_client
        self.telegram = telegram_manager
        self.journal = TradeJournal()
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
        self._prop_warned: bool = False
        self._be_moved_tickets: Set[int] = set()
        self.notion = NotionJournal()

    def _cleanup_old_notifications(self, max_age_hours: float = 6.0):
        """Clears old sent setup keys so memory does not leak."""
        now = time.time()
        expired = [k for k, ts in self.notified_setups.items() if (now - ts) > (max_age_hours * 3600)]
        for k in expired:
            del self.notified_setups[k]

    async def monitor_open_trades(self):
        """
        Monitors active positions tracked in trades.db and checks Prop Firm risk thresholds.
        When a position closes (TP, SL, or manual), updates the journal
        and dispatches a Telegram notification.
        """
        # 1. Prop Firm Drawdown Safety Check
        if PROP_FIRM_MODE and self.telegram:
            metrics = self.mt5_client.get_prop_metrics()
            if metrics:
                if metrics.get("is_safety_halt") and not self._prop_warned:
                    msg = (
                        "🚨 **PROP FIRM: OSTRZEŻENIE DRAWDOWNU!** 🚨\n\n"
                        f"⚠️ Aktualny Drawdown osiągnął: `{metrics['current_dd_pct']:.2f}%`!\n"
                        f"🛑 Próg bezpieczeństwa: `{metrics['safety_dd_pct']:.1f}%` (Max DD: `{metrics['max_dd_pct']:.1f}%`)\n"
                        f"💰 Pozostały bufor kapitału: `{metrics['remaining_buffer_usd']:.2f} {metrics['currency']}`\n\n"
                        "🛡️ **Nowe skanowanie i transakcje zostały wstrzymane**, aby chronić Twoje konto przed utratą!"
                    )
                    await self.telegram.send_message(msg)
                    self._prop_warned = True
                elif not metrics.get("is_safety_halt") and self._prop_warned:
                    self._prop_warned = False  # Reset once equity recovers

        # 2. Closed Trades Monitor
        open_trades = self.journal.get_open_trades()
        if not open_trades:
            return

        for t in open_trades:
            ticket = t.get("mt5_ticket")
            if not ticket:
                continue

            closed_info = self.mt5_client.get_closed_deal_info(ticket)
            if closed_info:
                profit = closed_info.get("profit", 0.0)
                reason = closed_info.get("reason", 0)

                # Determine outcome based on deal reason and profit
                if reason == 5 or profit > 0:
                    status = "WIN"
                    status_desc = "🎯 TAKE PROFIT OSIĄGNIĘTY!"
                    realized_r = t.get("max_r", 1.8) if t.get("max_r", 0) > 0 else 1.8
                elif reason == 4 or profit < 0:
                    status = "LOSS"
                    status_desc = "🛑 STOP LOSS TRAFIONY"
                    realized_r = -1.0
                else:
                    status = "WIN" if profit > 0 else "LOSS"
                    status_desc = "✋ ZAMKNIĘCIE RĘCZNE"
                    realized_r = 0.5 if profit > 0 else -0.5

                comment_str = f"{status_desc} | Zysk: {profit:+.2f} USD"
                self.journal.close_trade_by_ticket(ticket, realized_r, status, comment_str)

                # Update trade result in Notion database
                if self.notion.enabled:
                    asyncio.create_task(
                        self.notion.update_closed_trade(
                            ticket=ticket,
                            realized_r=realized_r,
                            status=status,
                            profit=profit,
                            comment=comment_str,
                            page_id=t.get("notion_page_id"),
                        )
                    )

                logger.info(
                    f"Trade #{ticket} ({t['symbol']}) closed! Status: {status}, "
                    f"Realized R: {realized_r:+.1f}R, Profit: {profit:+.2f} USD"
                )

                if self.telegram:
                    msg = (
                        f"🏁 **POZYCJA ZAMKNIĘTA: {t['symbol']} [{t['direction']}]**\n\n"
                        f"🎫 **Ticket:** `#{ticket}`\n"
                        f"🎯 **Status:** **{status_desc}**\n"
                        f"💵 **Wynik:** `{profit:+.2f} USD` (`{realized_r:+.1f}R`)\n\n"
                        f"🔓 **Para {t['symbol']} została odblokowana i wraca do skanera!**\n"
                        f"📓 Dziennik zaktualizowany. Wpisz `/journal` lub `/optimizer`"
                    )
                    await self.telegram.send_message(msg)

        # 3. Auto Break-Even Assistant (Locks 0 risk when trade reaches +1.0R)
        if AUTO_BE_AT_1R:
            open_positions = self.mt5_client.get_open_positions()
            for p in open_positions:
                ticket = p["ticket"]
                if ticket in self._be_moved_tickets:
                    continue

                entry_p = p["price_open"]
                sl_p = p["sl"]
                curr_p = p["price_current"]
                pnl = p["profit"]

                if sl_p <= 0 or entry_p <= 0:
                    continue

                initial_sl_dist = abs(entry_p - sl_p)
                # If SL is already moved to BE (within 15% distance to entry)
                if abs(entry_p - sl_p) < (initial_sl_dist * 0.15):
                    self._be_moved_tickets.add(ticket)
                    continue

                is_buy = p["type"] == "BUY"
                favorable_move = (curr_p - entry_p) if is_buy else (entry_p - curr_p)

                # If market moved >= 1.0R in favor of trade
                if favorable_move >= initial_sl_dist:
                    ok, msg = self.mt5_client.move_sl_to_breakeven(ticket)
                    if ok:
                        self._be_moved_tickets.add(ticket)
                        if self.telegram:
                            alert_msg = (
                                f"⚡ **AUTO BREAK-EVEN (+1.0R)!**\n\n"
                                f"🎯 Pozycja: `{p['symbol']}` [{p['type']}] `#{ticket}`\n"
                                f"📈 Aktualny zysk: `+{pnl:.2f} USD` (Osiągnięto +1.0R!)\n"
                                f"🛡️ **Stop Loss przestawiony na wejście ({entry_p})!**\n"
                                f"🔒 Pozycja jest już wolna od ryzyka (Risk-Free Trade)."
                            )
                            await self.telegram.send_message(alert_msg)

    async def scan_once(self) -> int:
        """Runs a single scan across all configured symbols."""
        if not self.mt5_client.ensure_connection():
            logger.error("MT5 connection unavailable during scan.")
            return 0

        self._cleanup_old_notifications()
        # Monitor open positions for closures
        await self.monitor_open_trades()

        # Prop Firm checks before scanning
        if PROP_FIRM_MODE:
            metrics = self.mt5_client.get_prop_metrics()
            if metrics.get("is_safety_halt"):
                logger.warning(
                    f"🛡️ [PROP GUARDIAN] Drawdown safety buffer reached ({metrics.get('current_dd_pct', 0):.2f}%). "
                    f"Wstrzymano skanowanie, aby chronić konto Prop Firm."
                )
                return 0
            open_cnt = metrics.get("open_positions_count", 0)
            if open_cnt >= PROP_MAX_OPEN_TRADES:
                logger.info(
                    f"🛡️ [PROP LIMIT] Osiągnięto limit otwartych pozycji ({open_cnt}/{PROP_MAX_OPEN_TRADES}). "
                    f"Skaner czeka na zwolnienie slotu pozycji."
                )
                return 0

        found_count = 0
        busy_symbols = self.mt5_client.get_busy_symbols()

        logger.info(
            f"Scanning {len(SYMBOLS)} symbols (SMC: {ENABLE_SMC_STRATEGY}, Zumiko: {ENABLE_ZUMIKO_STRATEGY}). "
            f"Locked symbols with active trades: {busy_symbols}"
        )

        for sym in SYMBOLS:
            # Active Position Lock: Skip symbol if it currently has an open position or pending order
            if sym.upper() in busy_symbols:
                logger.info(f"🔒 [SYMBOL LOCK] {sym} posiada aktywną pozycję/zlecenie w MT5. Pomijam skanowanie do czasu zamknięcia.")
                continue

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

                    # Prop Firm trade eligibility check before alert
                    if PROP_FIRM_MODE:
                        sl_dist = abs(setup.entry_price - setup.sl_price)
                        can_trade, reason = self.mt5_client.can_open_new_trade(setup.symbol, sl_distance=sl_dist)
                        if not can_trade:
                            logger.info(f"🛡️ [PROP SHIELD] Pomijam powiadomienie dla {setup.symbol}: {reason}")
                            continue

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
        """Main asynchronous scanner loop with real-time trade monitoring."""
        self.is_running = True
        logger.info(f"SMC & Zumiko Multi-Strategy Scanner loop started. Interval: {SCAN_INTERVAL_SECONDS}s")

        last_scan_time = 0.0

        while self.is_running:
            try:
                now = time.time()
                # Fast trade monitoring every TRADE_MONITOR_INTERVAL_SECONDS for instant TP / SL closing alerts
                await self.monitor_open_trades()

                # Full market scan every SCAN_INTERVAL_SECONDS (e.g. 60s)
                if (now - last_scan_time) >= SCAN_INTERVAL_SECONDS:
                    await self.scan_once()
                    last_scan_time = now

            except Exception as e:
                logger.error(f"Error in scanner loop: {e}", exc_info=True)

            await asyncio.sleep(TRADE_MONITOR_INTERVAL_SECONDS)

    def stop(self):
        self.is_running = False
        logger.info("Scanner stopped.")
