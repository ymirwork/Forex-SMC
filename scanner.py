import asyncio
from datetime import datetime
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import MetaTrader5 as mt5
import pandas as pd

from chart_generator import ChartGenerator
from config import (
    ADD_SPREAD_TO_SL,
    ALERT_COOLDOWN_MINUTES,
    AUTO_BE_AT_1R,
    AUTO_BE_OFFSET_PIPS,
    AUTO_BE_R_TRIGGER,
    BLOCK_ON_HIGH_IMPACT_NEWS,
    BLOCK_ROLLOVER_SESSION,
    DYNAMIC_FILTER_MODE,
    ENABLE_DYNAMIC_EDGE_FILTER,
    ENABLE_PYRAMIDING,
    ENABLE_ROLLOVER_SL_SHIELD,
    ENABLE_SMC_STRATEGY,
    ENABLE_ZUMIKO_STRATEGY,
    HTF_TIMEFRAME,
    INSIGHTS_MIN_SAMPLE_SIZE,
    LOOKBACK_BARS,
    LTF_TIMEFRAME,
    MAX_PYRAMID_LEVEL,
    MIN_CONFLUENCE_SCORE,
    PROP_ENFORCE_NEWS_FILTER,
    PROP_FIRM_MODE,
    PROP_MAX_DRAWDOWN_PERCENT,
    PROP_MAX_OPEN_TRADES,
    PROP_SAFETY_BUFFER_PERCENT,
    PYRAMID_AUTO_EXECUTE,
    PYRAMID_LOCK_PREVIOUS_PROFIT,
    PYRAMID_MIN_PROFIT_R,
    PYRAMID_MIN_RR_TO_TP,
    ROLLOVER_DISASTER_SL_R,
    ROLLOVER_MAX_SPREAD_GOLD_PIPS,
    ROLLOVER_MAX_SPREAD_MULT,
    ROLLOVER_MAX_SPREAD_PIPS,
    ROLLOVER_SHIELD_END_MINUTES,
    ROLLOVER_SHIELD_START_MINUTES,
    SCAN_INTERVAL_SECONDS,
    TRADE_MONITOR_INTERVAL_SECONDS,
    SWING_LENGTH,
    SYMBOLS,
    ZUMIKO_FAST_EMA,
    ZUMIKO_REQUIRE_H1,
    ZUMIKO_SLOW_EMA,
    ZUMIKO_SWING_LEN,
    ZUMIKO_TREND_EMA,
    clean_symbol,
)
from mt5_client import MT5Client
from news_filter import NewsFilter
from notion_journal import NotionJournal
from session_manager import SessionManager
from smc_detector import SMCDetector, TradeSetup
from trade_journal import TradeJournal
from zumiko_detector import ZumikoDetector
from trade_insights import TradeInsightEngine

logger = logging.getLogger("ForexSMC.Scanner")


def is_in_rollover_window(broker_dt: datetime, start_mins: int = 15, end_mins: int = 15) -> bool:
    """
    Checks if broker server time falls within the daily rollover window.
    Default: 23:(60 - start_mins) to 00:(end_mins) broker time.
    """
    h = broker_dt.hour
    m = broker_dt.minute
    if h == 23 and (60 - m) <= start_mins:
        return True
    if h == 0 and m < end_mins:
        return True
    return False


class SMCScanner:
    """
    Multi-Strategy Market Scanner:
    1. Smart Money Concepts (M15 CHoCH + Fibo OTE >= 61.8% + Asian Sweep)
    2. Zumiko KillMLM Strategy (EMA 238 Trend + EMA 10/21 Cross + MTF Alignment)
    3. Golden Combo (Co-incident alignment of both SMC & Zumiko in the same direction)
    4. Active Position Lock: Automatically locks symbols with open trades / pending orders until closed.
    5. Real-Time Trade Monitor: Detects TP / SL hits, logs results, and sends notifications.
    """

    def __init__(self, mt5_client: MT5Client, telegram_manager=None, journal=None, news_filter=None):
        self.mt5_client = mt5_client
        self.telegram = telegram_manager
        self.journal = journal or TradeJournal()
        self.news_filter = news_filter or NewsFilter()
        self.smc_detector = SMCDetector(
            swing_length=SWING_LENGTH,
            min_confluence_score=MIN_CONFLUENCE_SCORE,
            strict_htf_alignment=False,
            journal=self.journal,
            news_filter=self.news_filter,
        )
        self.zumiko_detector = ZumikoDetector(
            fast_ema=ZUMIKO_FAST_EMA,
            slow_ema=ZUMIKO_SLOW_EMA,
            trend_ema=ZUMIKO_TREND_EMA,
            swing_len=ZUMIKO_SWING_LEN,
            min_confluence_score=MIN_CONFLUENCE_SCORE,
            require_h1_alignment=ZUMIKO_REQUIRE_H1,
            journal=self.journal,
            news_filter=self.news_filter,
        )
        self.chart_gen = ChartGenerator()
        self.is_running = False
        self.notified_setups: Dict[str, float] = {}
        self._last_alert_by_symbol: Dict[Tuple[str, str], float] = {}
        self._prop_warned: bool = False
        self._be_moved_tickets: Set[int] = set()
        self._shielded_trades: Dict[int, float] = {}
        try:
            stored_shielded = self.journal.get_rollover_shielded_trades()
            for st in stored_shielded:
                t_num = st.get("mt5_ticket")
                orig_sl = float(st.get("shielded_original_sl") or 0.0)
                if t_num and orig_sl > 0:
                    self._shielded_trades[t_num] = orig_sl
        except Exception as e:
            logger.warning(f"Failed to restore shielded trades from database: {e}")
        self._htf_cache: Dict[Tuple[str, str], Tuple[float, pd.DataFrame]] = {}
        self.notion = NotionJournal()
        self.insight_engine = TradeInsightEngine(self.journal, min_sample_size=INSIGHTS_MIN_SAMPLE_SIZE) if ENABLE_DYNAMIC_EDGE_FILTER else None
        self._cleanup_old_charts(max_days=3)

    def _cleanup_old_charts(self, max_days: int = 3):
        """Removes generated chart PNGs older than max_days to prevent disk accumulation."""
        charts_dir = getattr(self.chart_gen, "output_dir", "charts")
        if not os.path.exists(charts_dir):
            return
        now = time.time()
        max_age_seconds = max_days * 86400
        try:
            for fname in os.listdir(charts_dir):
                if fname.lower().endswith(".png"):
                    fpath = os.path.join(charts_dir, fname)
                    if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > max_age_seconds:
                        try:
                            os.remove(fpath)
                        except Exception:
                            pass
        except Exception as e:
            logger.debug(f"Error during chart cleanup: {e}")

    def _cleanup_old_notifications(self, max_age_hours: float = 2.0):
        """Clears old sent setup keys so memory does not leak."""
        now = time.time()
        expired = [k for k, ts in self.notified_setups.items() if (now - ts) > (max_age_hours * 3600)]
        for k in expired:
            del self.notified_setups[k]
        expired_symbols = [k for k, ts in self._last_alert_by_symbol.items() if (now - ts) > (max_age_hours * 3600)]
        for k in expired_symbols:
            del self._last_alert_by_symbol[k]

        # Extra safety cap: ensure dictionaries never exceed 500 keys under extreme multi-pair bursts
        if len(self.notified_setups) > 500:
            sorted_keys = sorted(self.notified_setups.items(), key=lambda x: x[1])
            for k, _ in sorted_keys[: len(sorted_keys) - 250]:
                self.notified_setups.pop(k, None)

        self._cleanup_old_charts()

    def _fetch_cached_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        n_bars: int,
        max_age_seconds: float = 1800.0,
    ) -> Optional[pd.DataFrame]:
        """Caches slower HTF candles (D1, W1) to eliminate redundant MT5 IPC queries."""
        key = (symbol.upper(), timeframe.upper())
        now = time.time()
        cached = self._htf_cache.get(key)
        if cached and (now - cached[0]) < max_age_seconds:
            return cached[1]
        df = self.mt5_client.fetch_ohlcv(symbol, timeframe, n_bars)
        if df is not None and not df.empty:
            self._htf_cache[key] = (now, df)
        return df

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

        # 1.5. Pending Orders Fill Monitor (Audit finding 2 & Review finding 4)
        try:
            pending_trades = self.journal.get_pending_trades()
        except Exception as e:
            logger.error(f"Failed to query pending trades from journal: {e}")
            pending_trades = []

        for pt in pending_trades:
            ticket = pt.get("mt5_ticket")
            if not ticket:
                continue

            # Check if ticket now exists in open positions (order filled!)
            pos = mt5.positions_get(ticket=ticket)
            if pos and len(pos) > 0:
                p_item = pos[0]
                fill_price = getattr(p_item, "price_open", None)
                if fill_price is None and isinstance(p_item, dict):
                    fill_price = p_item.get("price_open", pt.get("entry_price", 0.0))
                fill_price = float(fill_price or pt.get("entry_price", 0.0))
                self.journal.mark_trade_as_open(ticket, fill_price, comment="Zlecenie limit zrealizowane w MT5")
                logger.info(f"✅ Pending order #{ticket} ({pt['symbol']}) filled at {fill_price}!")

                # If this was a pyramid scale-in, synchronize cluster SL!
                sync_msg = ""
                if pt.get("is_pyramid") or pt.get("parent_ticket"):
                    sync_results = self.mt5_client.sync_cluster_stop_loss(
                        symbol=pt["symbol"],
                        new_sl=pt["sl_price"],
                    )
                    synced_count = sum(1 for _, ok, _ in sync_results if ok)
                    sync_msg = f"\n🔒 **Stop Loss klastra zsynchronizowany** ({synced_count} pozycji przestawionych na `{pt['sl_price']}`)."

                if self.telegram:
                    pyr_badge = f" [PIRAMIDA L{pt.get('pyramid_level', 2)}]" if pt.get("is_pyramid") else ""
                    alert_msg = (
                        f"⚡ **ZLECENIE LIMIT ZREALIZOWANE!{pyr_badge}**\n\n"
                        f"🎯 Pozycja: `{pt['symbol']}` [{pt['direction']}] `#{ticket}`\n"
                        f"📍 Cena wejścia: `{fill_price}` | SL: `{pt['sl_price']}` | TP: `{pt['tp_price']}`"
                        f"{sync_msg}"
                    )
                    await self.telegram.send_message(alert_msg)
                continue

            # Check if pending order was cancelled in MT5
            closed_info = self.mt5_client.get_closed_deal_info(ticket)
            if closed_info and closed_info.get("status") == "CANCELLED":
                self._be_moved_tickets.discard(ticket)
                self._shielded_trades.pop(ticket, None)
                self.journal.cancel_trade_by_ticket(
                    ticket,
                    comment=closed_info.get("comment", "Zlecenie oczekujące anulowane w MT5"),
                )
                logger.info(f"Pending order #{ticket} ({pt['symbol']}) marked as CANCELLED.")
                if self.telegram:
                    await self.telegram.send_message(
                        f"ℹ️ **ZLECENIE ANULOWANE: {pt['symbol']} [{pt['direction']}]**\n\n"
                        f"🎫 **Ticket:** `#{ticket}`\n"
                        f"Status: Zlecenie oczekujące wygasło lub zostało anulowane w MT5."
                    )
                continue

        # 2. Closed Trades Monitor
        try:
            open_trades = self.journal.get_open_trades()
        except Exception as e:
            logger.error(f"Failed to query open trades from journal: {e}")
            open_trades = []

        for t in (open_trades or []):
            ticket = t.get("mt5_ticket")
            if not ticket:
                continue

            closed_info = self.mt5_client.get_closed_deal_info(ticket)
            if closed_info:
                if closed_info.get("status") == "CANCELLED":
                    self._be_moved_tickets.discard(ticket)
                    self._shielded_trades.pop(ticket, None)
                    self.journal.cancel_trade_by_ticket(
                        ticket,
                        comment=closed_info.get("comment", "Zlecenie oczekujące anulowane w MT5"),
                    )
                    logger.info(f"Pending order #{ticket} ({t['symbol']}) marked as CANCELLED.")
                    if self.telegram:
                        await self.telegram.send_message(
                            f"ℹ️ **ZLECENIE ANULOWANE: {t['symbol']} [{t['direction']}]**\n\n"
                            f"🎫 **Ticket:** `#{ticket}`\n"
                            f"Status: Zlecenie oczekujące wygasło lub zostało anulowane w MT5."
                        )
                    continue

                profit = closed_info.get("profit", 0.0)
                reason = closed_info.get("reason", 0)
                price_close = closed_info.get("price_close", 0.0)

                entry_p = t.get("entry_price", 0.0)
                sl_p = t.get("sl_price", 0.0)
                tp_p = t.get("tp_price", 0.0)
                is_buy = t.get("direction", "").upper() == "BUY"

                # Mathematically exact Realized R calculation
                initial_risk_dist = abs(entry_p - sl_p) if (entry_p > 0 and sl_p > 0) else 0.0
                if initial_risk_dist > 0 and price_close > 0:
                    price_diff = (price_close - entry_p) if is_buy else (entry_p - price_close)
                    realized_r = round(price_diff / initial_risk_dist, 2)
                else:
                    realized_r = 1.8 if profit > 0 else -1.0

                # Determine accurate status and description
                if profit > 0 and realized_r > 0.1:
                    status = "WIN"
                    status_desc = "🎯 TAKE PROFIT OSIĄGNIĘTY!" if (reason == 5 or (tp_p > 0 and abs(price_close - tp_p) <= (initial_risk_dist * 0.15))) else "📈 ZAMKNIĘCIE W ZYSKU"
                elif profit < 0 or realized_r < -0.2:
                    status = "LOSS"
                    status_desc = "🛑 STOP LOSS TRAFIONY" if reason == 4 else "📉 ZAMKNIĘCIE ZE STRATĄ"
                else:
                    status = "BE"
                    status_desc = "🛡️ ZAMKNIĘCIE NA BREAK-EVEN (0.0R)"
                    realized_r = 0.0

                # Calculate exact MFE (Max R) between trade open and close
                time_open = closed_info.get("time_open", 0)
                time_close = closed_info.get("time_close", 0)
                calc_mfe = self.mt5_client.get_position_mfe(
                    symbol=t["symbol"],
                    is_buy=is_buy,
                    entry_price=entry_p,
                    initial_sl_price=sl_p,
                    time_open=time_open,
                    time_close=time_close,
                )
                max_r = max(calc_mfe, realized_r, float(t.get("max_r") or 0.0))

                comment_str = f"{status_desc} | Zysk: {profit:+.2f} USD | Max R: {max_r:.2f}R"
                self._be_moved_tickets.discard(ticket)
                self._shielded_trades.pop(ticket, None)
                self.journal.close_trade_by_ticket(ticket, realized_r, max_r=max_r, status=status, comment=comment_str)

                # Record close-time telemetry
                exit_r = "TP" if status == "WIN" else ("SL" if status == "LOSS" else "BE")
                self.journal.update_trade_close_telemetry(ticket, exit_reason=exit_r, mae_r=0.0)

                # Update trade result in Notion database
                if self.notion.enabled:
                    asyncio.create_task(
                        self.notion.update_closed_trade(
                            ticket=ticket,
                            realized_r=realized_r,
                            status=status,
                            profit=profit,
                            comment=comment_str,
                            max_r=max_r,
                            page_id=t.get("notion_page_id"),
                        )
                    )

                logger.info(
                    f"Trade #{ticket} ({t['symbol']}) closed! Status: {status}, "
                    f"Exact Realized R: {realized_r:+.2f}R, Max R: {max_r:.2f}R, Profit: {profit:+.2f} USD"
                )

                pyramid_badge = ""
                if t.get("is_pyramid") or t.get("parent_ticket"):
                    pyramid_badge = f"\n🔥 **Transakcja Piramidy (Dokładka do #{t.get('parent_ticket')})**"

                if self.telegram:
                    msg = (
                        f"🏁 **POZYCJA ZAMKNIĘTA: {t['symbol']} [{t['direction']}]**{pyramid_badge}\n\n"
                        f"🎫 **Ticket:** `#{ticket}`\n"
                        f"🎯 **Status:** **{status_desc}**\n"
                        f"💵 **Wynik:** `{profit:+.2f} USD` (`{realized_r:+.2f}R` | Max R: `{max_r:.2f}R`)\n\n"
                        f"🔓 **Para {t['symbol']} została odblokowana i wraca do skanera!**\n"
                        f"📓 Dziennik zaktualizowany. Wpisz `/journal` lub `/optimizer`"
                    )
                    await self.telegram.send_message(msg)

        # 3. Auto Break-Even Assistant & Live Max R Tracking
        open_positions = self.mt5_client.get_open_positions()
        for p in open_positions:
            ticket = p["ticket"]
            entry_p = p["price_open"]
            sl_p = p["sl"]
            curr_p = p["price_current"]
            pnl = p["profit"]
            is_buy = p["type"] == "BUY"
            current_mfe = None

            orig_sl = self.mt5_client.get_initial_sl(ticket)
            if orig_sl <= 0:
                orig_sl = sl_p

            if orig_sl <= 0 or entry_p <= 0:
                continue

            initial_sl_dist = abs(entry_p - orig_sl)

            # Live Max R (MFE) tracking for open positions in trades.db
            journal_trade = self.journal.get_trade_by_ticket(ticket)
            if journal_trade and initial_sl_dist > 0:
                current_mfe = self.mt5_client.get_position_mfe(
                    symbol=p["symbol"],
                    is_buy=is_buy,
                    entry_price=entry_p,
                    initial_sl_price=orig_sl,
                    time_open=p.get("time", 0),
                )
                prev_max = float(journal_trade.get("max_r") or 0.0)
                if current_mfe > prev_max:
                    self.journal.update_trade_max_r(ticket, current_mfe)

            # Auto Break-Even Assistant (Locks 0 risk when trade reaches configured R, e.g. +0.85R)
            if AUTO_BE_AT_1R:
                if ticket in self._be_moved_tickets or ticket in self._shielded_trades:
                    continue

                # If SL is already moved to BE or into profit
                is_already_be = self.mt5_client.is_position_at_be(
                    entry=entry_p,
                    sl=sl_p,
                    is_buy=is_buy,
                    initial_sl_dist=initial_sl_dist,
                    tolerance_ratio=0.15,
                )
                if is_already_be:
                    self._be_moved_tickets.add(ticket)
                    continue

                favorable_move = (curr_p - entry_p) if is_buy else (entry_p - curr_p)
                trigger_dist = initial_sl_dist * AUTO_BE_R_TRIGGER
                current_trade_mfe = current_mfe if current_mfe is not None else (favorable_move / initial_sl_dist if initial_sl_dist > 0 else 0.0)

                # If market moved >= AUTO_BE_R_TRIGGER (e.g. 0.85R) in favor of trade (checked via live tick or MFE)
                if favorable_move >= trigger_dist or current_trade_mfe >= AUTO_BE_R_TRIGGER:
                    ok, msg = self.mt5_client.move_sl_to_breakeven(ticket, offset_pips=AUTO_BE_OFFSET_PIPS)
                    if ok:
                        self._be_moved_tickets.add(ticket)
                        if self.telegram:
                            offset_txt = f" +{AUTO_BE_OFFSET_PIPS:.1f} pips na pokrycie kosztów" if AUTO_BE_OFFSET_PIPS > 0 else ""
                            alert_msg = (
                                f"⚡ **AUTO BREAK-EVEN (+{AUTO_BE_R_TRIGGER:.2f}R)!**\n\n"
                                f"🎯 Pozycja: `{p['symbol']}` [{p['type']}] `#{ticket}`\n"
                                f"📈 Aktualny zysk: `+{pnl:.2f} USD` (Osiągnięto +{AUTO_BE_R_TRIGGER:.2f}R!)\n"
                                f"🛡️ **Stop Loss przestawiony na wejście{offset_txt}!**\n"
                                f"🔒 Pozycja jest w 100% bezpieczna (Guaranteed Profit / Risk-Free)."
                            )
                            await self.telegram.send_message(alert_msg)

        # 4. Rollover SL Shield (Protects tight SL and BE trades during rollover spread spike)
        await self._handle_rollover_sl_shield(open_positions)

    async def _handle_rollover_sl_shield(self, open_positions: List[Dict]):
        """
        Rollover SL Shield:
        Protects tight SL and Break-Even trades during broker rollover spread spikes (23:45 - 00:15 broker time).
        - Moves physical SL in MT5 to Disaster SL (e.g. 2.5R) so spread widening doesn't prematurely trigger SL.
        - Bot monitors true market MID price = (Bid + Ask) / 2. If MID hits the original SL, position is closed at market.
        - Restores original SL once rollover window ends AND symbol spread normalizes.
        """
        if not ENABLE_ROLLOVER_SL_SHIELD or not open_positions:
            return

        broker_now = self.mt5_client.get_broker_server_time()
        in_rollover = is_in_rollover_window(
            broker_now,
            start_mins=ROLLOVER_SHIELD_START_MINUTES,
            end_mins=ROLLOVER_SHIELD_END_MINUTES,
        )

        open_by_ticket = {p["ticket"]: p for p in open_positions}

        # Clean up shielded trades that are no longer in open positions
        for t_num in list(self._shielded_trades.keys()):
            if t_num not in open_by_ticket:
                self._shielded_trades.pop(t_num, None)
                self.journal.unmark_trade_rollover_shielded(t_num)

        if in_rollover:
            # 1. Activation Phase: Shield qualifying open positions not yet shielded
            for p in open_positions:
                ticket = p["ticket"]
                if ticket in self._shielded_trades:
                    continue

                entry_p = float(p.get("price_open", 0.0))
                sl_p = float(p.get("sl", 0.0))
                if sl_p <= 0 or entry_p <= 0:
                    continue

                is_buy = p["type"] == "BUY"
                orig_sl = self.mt5_client.get_initial_sl(ticket)
                if orig_sl <= 0:
                    orig_sl = sl_p

                initial_dist = abs(entry_p - orig_sl)
                pip_size = self.mt5_client.get_pip_size(p["symbol"])
                if initial_dist <= 0:
                    initial_dist = 25 * pip_size

                # Check qualification: Only shield trades at Initial SL or at Break-Even
                is_be = self.mt5_client.is_position_at_be(
                    entry=entry_p,
                    sl=sl_p,
                    is_buy=is_buy,
                    initial_sl_dist=initial_dist,
                    tolerance_ratio=0.15,
                )
                is_deep_trailing = (sl_p > entry_p + (initial_dist * 0.5)) if is_buy else (sl_p < entry_p - (initial_dist * 0.5))

                if is_deep_trailing and not is_be:
                    continue

                # Calculate Disaster SL (placed safely away at 2.5R)
                sym_info = mt5.symbol_info(p["symbol"])
                digits = getattr(sym_info, "digits", 5) if sym_info else 5

                disaster_distance = initial_dist * ROLLOVER_DISASTER_SL_R
                if is_buy:
                    disaster_sl = round(entry_p - disaster_distance, digits)
                    if disaster_sl >= sl_p:
                        disaster_sl = round(sl_p - disaster_distance, digits)
                else:
                    disaster_sl = round(entry_p + disaster_distance, digits)
                    if disaster_sl <= sl_p:
                        disaster_sl = round(sl_p + disaster_distance, digits)

                # Send SL update to MT5
                ok, msg = self.mt5_client.modify_position_sl(ticket, disaster_sl)
                if ok:
                    self._shielded_trades[ticket] = sl_p
                    self.journal.mark_trade_rollover_shielded(ticket, sl_p)
                    logger.info(
                        f"🛡️ [ROLLOVER SHIELD] Pozycja #{ticket} ({p['symbol']}) zabezpieczona: "
                        f"Oryginalny SL={sl_p}, Disaster SL={disaster_sl} ({ROLLOVER_DISASTER_SL_R:.1f}R)"
                    )
                    start_min_clamped = min(59, max(1, ROLLOVER_SHIELD_START_MINUTES))
                    start_time_str = f"23:{60 - start_min_clamped:02d}"
                    if self.telegram:
                        await self.telegram.send_message(
                            f"🛡️ **ROLLOVER SL SHIELD: AKTYWACJA ({start_time_str} MT5)**\n\n"
                            f"🎯 Pozycja: `{p['symbol']}` [{p['type']}] `#{ticket}`\n"
                            f"📍 Wejście: `{entry_p}` | Pierwotny SL: `{sl_p}`\n"
                            f"⚠️ **Awaryjny Disaster SL:** `{disaster_sl}` ({ROLLOVER_DISASTER_SL_R:.1f}R)\n"
                            f"🔒 Bot czuwa nad ceną rynkową MID – ochrona przed sztucznym rozszerzeniem nocnego spreadu."
                        )

            # 2. Monitoring Phase: Check if MID price legitimately crossed original SL (Cached per symbol)
            tick_cache: Dict[str, Any] = {}
            for ticket, orig_sl in list(self._shielded_trades.items()):
                p = open_by_ticket.get(ticket)
                if not p:
                    continue

                sym = p["symbol"]
                if sym not in tick_cache:
                    tick_cache[sym] = mt5.symbol_info_tick(sym)
                tick = tick_cache[sym]
                if not tick or tick.bid <= 0 or tick.ask <= 0:
                    continue

                mid_price = (tick.bid + tick.ask) / 2.0
                is_buy = p["type"] == "BUY"

                mid_hit_sl = (mid_price <= orig_sl) if is_buy else (mid_price >= orig_sl)
                if mid_hit_sl:
                    logger.warning(
                        f"🚨 [ROLLOVER SHIELD] Cena MID ({mid_price}) przebiła oryginalny SL ({orig_sl}) na #{ticket}!"
                    )
                    close_ok, close_msg = self.mt5_client.close_market_position(
                        ticket,
                        comment="Rollover Shield: Mid hit SL",
                    )
                    if close_ok:
                        self._shielded_trades.pop(ticket, None)
                        self.journal.unmark_trade_rollover_shielded(ticket)
                        if self.telegram:
                            await self.telegram.send_message(
                                f"🚨 **AWARYJNE ZAMKNIĘCIE W TRAKCIE ROLLOVERU!**\n\n"
                                f"🎯 Pozycja: `{p['symbol']}` [{p['type']}] `#{ticket}`\n"
                                f"📉 Rzeczywista cena MID: `{mid_price:.5f}` wybiła pierwotny SL: `{orig_sl}`\n"
                                f"🛑 Pozycja została zamknięta rynkowo, aby zapobiec powiększeniu straty."
                            )

        else:
            # 3. Restoration Phase: After rollover, check spread normalization and restore SL (Cached per symbol)
            if not self._shielded_trades:
                return

            spread_cache: Dict[str, float] = {}
            for ticket, orig_sl in list(self._shielded_trades.items()):
                p = open_by_ticket.get(ticket)
                if not p:
                    self._shielded_trades.pop(ticket, None)
                    self.journal.unmark_trade_rollover_shielded(ticket)
                    continue

                sym = p["symbol"]
                if sym not in spread_cache:
                    spread_cache[sym] = self.mt5_client.get_symbol_spread(sym)
                spread_pips = spread_cache[sym]

                is_gold = "XAU" in clean_symbol(sym)
                max_spread = ROLLOVER_MAX_SPREAD_GOLD_PIPS if is_gold else ROLLOVER_MAX_SPREAD_PIPS

                if spread_pips <= max_spread:
                    ok, msg = self.mt5_client.modify_position_sl(ticket, orig_sl)
                    if ok:
                        self._shielded_trades.pop(ticket, None)
                        self.journal.unmark_trade_rollover_shielded(ticket)
                        logger.info(
                            f"✅ [ROLLOVER SHIELD] Spread unormowany ({spread_pips:.1f} pips). "
                            f"Przywrócono oryginalny SL={orig_sl} dla #{ticket} ({p['symbol']})."
                        )
                        if self.telegram:
                            await self.telegram.send_message(
                                f"✅ **ROLLOVER SL SHIELD: ZAKOŃCZONO (Spread w normie)**\n\n"
                                f"🎯 Pozycja: `{p['symbol']}` [{p['type']}] `#{ticket}`\n"
                                f"📊 Aktualny spread: `{spread_pips:.1f} pips` (<= `{max_spread:.1f}`)\n"
                                f"🛡️ **Przywrócono ciasny Stop Loss:** `{orig_sl}`\n"
                                f"Pozycja kontynuuje standardowe monitorowanie."
                            )
                    else:
                        logger.warning(f"Could not restore SL for #{ticket}: {msg}")
                else:
                    logger.debug(
                        f"Rollover Shield waiting for spread normalization on {p['symbol']}: "
                        f"current={spread_pips:.1f} pips > threshold={max_spread:.1f} pips"
                    )

    async def scan_once(self) -> int:
        """Runs a single scan across all configured symbols."""
        if not self.mt5_client.ensure_connection():
            logger.error("MT5 connection unavailable during scan.")
            return 0

        self._cleanup_old_notifications()

        # Refresh macroeconomic news calendar asynchronously
        await self.news_filter.fetch_calendar_async()

        # Rollover & Low Liquidity Session Lock
        if BLOCK_ROLLOVER_SESSION:
            curr_session = SessionManager.get_current_session()
            if "Rollover" in curr_session.get("session_name", ""):
                logger.info(
                    f"🛡️ [SESSION LOCK] Aktywna sesja {curr_session.get('session_name')}. "
                    f"Wstrzymano skanowanie rynków z powodu niskiej płynności i rozszerzonych spreadów (BLOCK_ROLLOVER_SESSION=True)."
                )
                return 0

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
            # Check pyramiding eligibility for this symbol
            pyramid_status = self.mt5_client.get_pyramid_status(sym) if ENABLE_PYRAMIDING else {"is_eligible": False}
            is_pyramid = pyramid_status.get("is_eligible", False)

            # Active Position Lock: Skip symbol if it currently has an open position and is NOT eligible for pyramiding
            if sym.upper() in busy_symbols and not is_pyramid:
                logger.info(f"🔒 [SYMBOL LOCK] {sym} posiada aktywną pozycję/zlecenie w MT5. Pomijam skanowanie do czasu zamknięcia.")
                continue

            if is_pyramid:
                logger.info(
                    f"🔥 [PYRAMID SCAN] {sym} posiada pozycję bazową w zysku (+{pyramid_status.get('profit_r')}R) "
                    f"i zabezpieczoną na BE. Skanuję pod kątem dokładki ({pyramid_status.get('direction')})."
                )

            try:
                # Fetch multi-timeframe candles with at least 300 bars for Zumiko EMA 238 (Audit finding 1)
                scan_bars = max(LOOKBACK_BARS, 300)
                df_ltf = self.mt5_client.fetch_ohlcv(sym, LTF_TIMEFRAME, scan_bars)
                df_h1 = self.mt5_client.fetch_ohlcv(sym, "H1", scan_bars)
                df_htf = self.mt5_client.fetch_ohlcv(sym, HTF_TIMEFRAME, scan_bars)
                df_d1 = self._fetch_cached_ohlcv(sym, "D1", 10, max_age_seconds=1800.0)
                df_w1 = self._fetch_cached_ohlcv(sym, "W1", 5, max_age_seconds=7200.0)

                if df_ltf is None or df_htf is None:
                    continue

                smc_setups: List[TradeSetup] = []
                zumiko_setups: List[TradeSetup] = []

                # Spread buffer for Stop Loss
                spread_val = self.mt5_client.get_symbol_spread(sym) if ADD_SPREAD_TO_SL else 0.0

                if ENABLE_SMC_STRATEGY:
                    smc_setups = self.smc_detector.scan_for_setups(
                        sym,
                        df_ltf,
                        df_htf,
                        spread=spread_val,
                        df_d1=df_d1,
                        df_w1=df_w1,
                        df_h1=df_h1,
                    )

                if ENABLE_ZUMIKO_STRATEGY:
                    zumiko_setups = self.zumiko_detector.scan_for_setups(sym, df_ltf, df_h1, df_htf, spread=spread_val)

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
                    # If this is a pyramiding scan, filter out counter-trend setups & check distance to Master TP
                    if is_pyramid:
                        if setup.direction != pyramid_status.get("direction"):
                            continue  # Never pyramid against active position

                        master_tp = pyramid_status.get("master_tp", 0.0)
                        if master_tp > 0:
                            sl_dist = abs(setup.entry_price - setup.sl_price)
                            tp_dist = abs(master_tp - setup.entry_price)
                            rr_to_master = (tp_dist / sl_dist) if sl_dist > 0 else 0.0
                            if rr_to_master < PYRAMID_MIN_RR_TO_TP:
                                logger.info(
                                    f"⏭️ [PYRAMID SKIP] {sym}: Zbyt blisko Master TP "
                                    f"(R:R do celu: {rr_to_master:.2f}R < wymagane {PYRAMID_MIN_RR_TO_TP}R)."
                                )
                                continue
                            setup.master_tp = master_tp
                            setup.tp1_price = master_tp

                        setup.is_pyramid = True
                        setup.parent_ticket = pyramid_status.get("base_ticket")
                        setup.pyramid_level = pyramid_status.get("next_level", 2)
                        setup.pyramid_profit_r = pyramid_status.get("profit_r", 0.0)
                        setup.cluster_tickets = pyramid_status.get("cluster_tickets", [])
                        setup.cluster_locked_profit_usd = pyramid_status.get("profit_usd", 0.0)
                        setup.setup_type = f"[DOKŁADKA TRENDOWA L{setup.pyramid_level}] {setup.setup_type}"

                        # Strict Risk-Neutral volume verification
                        pyr_lot, locked_usd, allowed_risk, pyr_reason = self.mt5_client.calculate_pyramid_position_size(
                            symbol=setup.symbol,
                            entry_price=setup.entry_price,
                            sl_price=setup.sl_price,
                            setup_grade=getattr(setup, "grade", "A"),
                            cluster_status=pyramid_status,
                        )
                        if pyr_lot <= 0:
                            logger.info(f"⏭️ [PYRAMID SKIP] {sym}: Kwalifikacja wolumenu odrzucona: {pyr_reason}")
                            continue
                        setup._pyramid_lot = pyr_lot
                        setup.cluster_locked_profit_usd = locked_usd

                    # Anti-Spam Check 1: User Dismissed / Muted Check
                    if self.telegram and self.telegram.is_symbol_muted(setup.symbol, setup.direction):
                        logger.info(f"🔇 [MUTED] Pomijam alert dla {setup.symbol} {setup.direction} (użytkownik odrzucił ten setup).")
                        continue

                    # Anti-Spam Check 2: M15 Candle Lock (1 alert max per M15 candle timestamp)
                    candle_str = setup.timestamp.strftime("%Y%m%d_%H%M") if hasattr(setup.timestamp, "strftime") else str(setup.timestamp)
                    setup_key = f"{setup.symbol}_{setup.direction}_{setup.strategy_name}_{candle_str}"
                    if setup_key in self.notified_setups:
                        continue  # Already notified for this M15 candle

                    # Anti-Spam Check 3: Time Cooldown per Symbol & Direction
                    sym_dir_key = (clean_symbol(setup.symbol), setup.direction.upper())
                    last_alert_t = self._last_alert_by_symbol.get(sym_dir_key, 0.0)
                    if (time.time() - last_alert_t) < (ALERT_COOLDOWN_MINUTES * 60):
                        logger.debug(f"⏳ [COOLDOWN] Pomijam alert dla {setup.symbol} {setup.direction} (aktywny cooldown {ALERT_COOLDOWN_MINUTES}m).")
                        continue

                    # Macroeconomic High-Impact News Filter Check
                    should_block_news = BLOCK_ON_HIGH_IMPACT_NEWS or (PROP_FIRM_MODE and PROP_ENFORCE_NEWS_FILTER)
                    if should_block_news and setup.news_warning:
                        logger.warning(
                            f"🚫 [NEWS SHIELD] Wstrzymuję alert dla {setup.symbol} z powodu nadchodzących wiadomości: {setup.news_warning}"
                        )
                        continue

                    # Prop Firm trade eligibility check before alert
                    if PROP_FIRM_MODE:
                        sl_dist = abs(setup.entry_price - setup.sl_price)
                        can_trade, reason = self.mt5_client.can_open_new_trade(
                            setup.symbol,
                            sl_distance=sl_dist,
                            is_pyramid=setup.is_pyramid,
                        )
                        if not can_trade:
                            logger.info(f"🛡️ [PROP SHIELD] Pomijam powiadomienie dla {setup.symbol}: {reason}")
                            continue

                    # Dynamic Edge Filter: Check if this setup is historically weak
                    if self.insight_engine:
                        edge_eval = self.insight_engine.evaluate_setup(
                            symbol=setup.symbol,
                            session_name=getattr(setup, "session_name", ""),
                            strategy=getattr(setup, "strategy_name", "SMC"),
                            confluence_score=setup.confluence_score,
                            filter_mode=DYNAMIC_FILTER_MODE,
                        )
                        if edge_eval.is_flagged:
                            setup._edge_warnings = edge_eval.warnings
                            setup._risk_multiplier = edge_eval.risk_multiplier
                            if edge_eval.risk_multiplier <= 0.0:
                                logger.info(f"🛑 [EDGE FILTER STRICT] Zablokowano {setup.symbol} {setup.direction}: {edge_eval.summary}")
                                continue
                            else:
                                logger.info(f"⚠️ [EDGE FILTER] {setup.symbol}: {edge_eval.summary}")

                    found_count += 1
                    self.notified_setups[setup_key] = time.time()
                    self._last_alert_by_symbol[sym_dir_key] = time.time()
                    setup_id = f"{sym}_{setup.strategy_name}_{int(time.time())}"

                    logger.info(
                        f"🔥 NEW SETUP: {setup.symbol} {setup.direction} [{setup.strategy_name}] ({setup.setup_type})"
                    )

                    # Generate visual chart with Fibo or EMAs in worker thread (non-blocking)
                    chart_path = await asyncio.to_thread(self.chart_gen.generate_setup_chart, df_ltf, setup)

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
            now = time.time()
            try:
                # Fast trade monitoring every TRADE_MONITOR_INTERVAL_SECONDS for instant TP / SL closing alerts
                await self.monitor_open_trades()
            except Exception as e:
                logger.error(f"Error in trade monitor loop: {e}", exc_info=True)

            # Full market scan every SCAN_INTERVAL_SECONDS (e.g. 60s)
            if (now - last_scan_time) >= SCAN_INTERVAL_SECONDS:
                try:
                    await self.scan_once()
                    last_scan_time = now
                except Exception as e:
                    logger.error(f"Error during scan_once: {e}", exc_info=True)

            await asyncio.sleep(TRADE_MONITOR_INTERVAL_SECONDS)

    def stop(self):
        self.is_running = False
        logger.info("Scanner stopped.")
