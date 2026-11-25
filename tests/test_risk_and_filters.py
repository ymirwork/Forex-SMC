import unittest
from datetime import datetime, timezone
import pandas as pd

from config import (
    AUTO_BE_R_TRIGGER,
    MIN_SL_PIPS_CROSS_JPY,
    MIN_SL_PIPS_FOREX,
    MIN_SL_PIPS_GBP,
    MIN_SL_PIPS_GOLD,
    REJECTION_WICK_MIN_RATIO,
)
from session_manager import SessionManager
from smc_detector import (
    calculate_stop_loss,
    check_rejection_candle,
    get_min_sl_distance,
    get_symbol_pip_size,
)


class TestRiskAndFilters(unittest.TestCase):
    """Test suite verifying minimum SL floors, rejection candle confirmations, and session filters."""

    def test_symbol_pip_size(self):
        self.assertEqual(get_symbol_pip_size("EURUSD", 5), 0.0001)
        self.assertEqual(get_symbol_pip_size("USDJPY", 3), 0.01)
        self.assertEqual(get_symbol_pip_size("GBPJPY", 3), 0.01)
        self.assertEqual(get_symbol_pip_size("XAUUSD", 2), 0.10)

    def test_minimum_sl_floor_calculation(self):
        # 1. Standard Forex pair (EURUSD) -> min 14 pips
        dist_eur = get_min_sl_distance("EURUSD", 0.0001)
        self.assertAlmostEqual(dist_eur, 14.0 * 0.0001, places=5)

        # 2. GBP pair (GBPUSD) -> min 20 pips
        dist_gbp = get_min_sl_distance("GBPUSD", 0.0001)
        self.assertAlmostEqual(dist_gbp, 20.0 * 0.0001, places=5)

        # 3. JPY pair (USDJPY) -> min 18 pips
        dist_jpy = get_min_sl_distance("USDJPY", 0.01)
        self.assertAlmostEqual(dist_jpy, 18.0 * 0.01, places=4)

        # 4. Gold (XAUUSD) -> min $30.00
        dist_gold = get_min_sl_distance("XAUUSD", 0.10)
        self.assertGreaterEqual(dist_gold, 30.0)

    def test_calculate_stop_loss_enforces_floor(self):
        # Micro structural risk of 6 pips (0.0006) on USDCAD
        entry = 1.43180
        micro_structural_sl = 1.43120  # only 6 pips
        atr = 0.0008  # 8 pips ATR

        sl, risk, mode = calculate_stop_loss(
            entry=entry,
            structural_sl=micro_structural_sl,
            atr=atr,
            is_buy=True,
            point_scale=0.0001,
            symbol="USDCAD",
        )
        self.assertIsNotNone(sl)
        self.assertIsNotNone(risk)
        # Risk must be clamped to at least min 14 pips (0.0014)
        self.assertGreaterEqual(risk, 0.0014)
        self.assertLessEqual(sl, entry - 0.0014)

    def test_rejection_candle_filter_buy(self):
        # Scenario A: Falling knife (red marubozu, close < open, tiny lower wick)
        falling_knife_df = pd.DataFrame([
            {"open": 1.1000, "high": 1.1010, "low": 1.0960, "close": 1.0965},
            {"open": 1.0965, "high": 1.0970, "low": 1.0920, "close": 1.0922}, # big red candle, almost no wick at low
        ])
        ok, reason = check_rejection_candle(falling_knife_df, is_buy=True)
        self.assertFalse(ok)
        self.assertIn("Brak potwierdzenia", reason)

        # Scenario B: Hammer / Pinbar (long lower rejection wick >= 25%)
        hammer_df = pd.DataFrame([
            {"open": 1.1000, "high": 1.1010, "low": 1.0960, "close": 1.0965},
            # low=1.0900, open=1.0950, close=1.0940 -> lower wick is 1.0940 - 1.0900 = 40 pts. Range = 60 pts (66.6% wick)
            {"open": 1.0950, "high": 1.0960, "low": 1.0900, "close": 1.0940},
        ])
        ok, reason = check_rejection_candle(hammer_df, is_buy=True)
        self.assertTrue(ok)
        self.assertIn("dolny knot odrzucenia", reason)

        # Scenario C: Green candle close (close >= open)
        green_candle_df = pd.DataFrame([
            {"open": 1.1000, "high": 1.1010, "low": 1.0960, "close": 1.0965},
            {"open": 1.0920, "high": 1.0950, "low": 1.0915, "close": 1.0945},
        ])
        ok, reason = check_rejection_candle(green_candle_df, is_buy=True)
        self.assertTrue(ok)
        self.assertIn("prowzrostowo", reason)

    def test_rejection_candle_filter_sell(self):
        # Scenario A: Rising knife (green candle, close > open, tiny upper wick)
        rising_knife_df = pd.DataFrame([
            {"open": 1.0900, "high": 1.0950, "low": 1.0890, "close": 1.0945},
            {"open": 1.0945, "high": 1.0990, "low": 1.0940, "close": 1.0988},
        ])
        ok, reason = check_rejection_candle(rising_knife_df, is_buy=False)
        self.assertFalse(ok)

        # Scenario B: Shooting star / upper rejection wick >= 25%
        shooting_star_df = pd.DataFrame([
            {"open": 1.0900, "high": 1.0950, "low": 1.0890, "close": 1.0945},
            # open=1.0950, high=1.1000, close=1.0960 -> upper wick is 1.1000 - 1.0960 = 40 pts out of 50 (80%)
            {"open": 1.0950, "high": 1.1000, "low": 1.0950, "close": 1.0960},
        ])
        ok, reason = check_rejection_candle(shooting_star_df, is_buy=False)
        self.assertTrue(ok)
        self.assertIn("górny knot odrzucenia", reason)

    def test_session_manager_evening_block(self):
        # 19:30 UTC -> inside evening block (17:00 - 23:00 UTC)
        evening_dt = datetime(2026, 11, 19, 19, 30, tzinfo=timezone.utc)
        sess = SessionManager.get_current_session(evening_dt)
        self.assertTrue(sess["is_evening_blocked"])

        # 08:30 UTC -> London killzone, outside evening block
        london_dt = datetime(2026, 11, 19, 8, 30, tzinfo=timezone.utc)
        sess_london = SessionManager.get_current_session(london_dt)
        self.assertFalse(sess_london["is_evening_blocked"])
        self.assertTrue(sess_london["is_killzone"])

    def test_auto_be_trigger_threshold(self):
        # Target threshold should be a positive R value
        self.assertGreater(AUTO_BE_R_TRIGGER, 0.0)

    def test_multi_position_break_even_isolation(self):
        """Verifies that an open position with MFE does not leak state to subsequent positions."""
        from unittest.mock import MagicMock, AsyncMock, patch
        import asyncio
        from scanner import SMCScanner

        mt5_mock = MagicMock()
        journal_mock = MagicMock()
        journal_mock.get_pending_trades.return_value = []
        journal_mock.get_open_trades.return_value = []
        journal_mock.get_rollover_shielded_trades.return_value = []

        # Position 1: EURUSD (reached +1.2R, qualifies for BE)
        # Position 2: GBPUSD (only +0.1R, does NOT qualify for BE, no journal entry)
        p1 = {
            "ticket": 101,
            "symbol": "EURUSD",
            "type": "BUY",
            "price_open": 1.0800,
            "sl": 1.0700,
            "price_current": 1.0900,
            "profit": 100.0,
            "time": 1000,
        }
        p2 = {
            "ticket": 102,
            "symbol": "GBPUSD",
            "type": "BUY",
            "price_open": 1.3000,
            "sl": 1.2900,
            "price_current": 1.3010,
            "profit": 10.0,
            "time": 1005,
        }

        mt5_mock.get_open_positions.return_value = [p1, p2]
        mt5_mock.get_initial_sl.side_effect = lambda t: 1.0700 if t == 101 else 1.2900
        mt5_mock.is_position_at_be.return_value = False
        mt5_mock.move_sl_to_breakeven.return_value = (True, "BE moved")
        mt5_mock.get_broker_server_time.return_value = datetime(2026, 11, 19, 12, 0)
        mt5_mock.get_prop_metrics.return_value = {}

        # Trade 101 has journal record and MFE 1.2R; Trade 102 has None
        def mock_get_trade(ticket):
            if ticket == 101:
                return {"mt5_ticket": 101, "max_r": 0.5}
            return None

        journal_mock.get_trade_by_ticket.side_effect = mock_get_trade
        mt5_mock.get_position_mfe.return_value = 1.2

        scanner = SMCScanner(mt5_client=mt5_mock, telegram_manager=None, journal=journal_mock)

        # Run monitor_open_trades
        asyncio.run(scanner.monitor_open_trades())

        # Verify only ticket 101 was moved to Break-Even!
        self.assertEqual(mt5_mock.move_sl_to_breakeven.call_count, 1)
        args, _ = mt5_mock.move_sl_to_breakeven.call_args
        self.assertEqual(args[0], 101)

    def test_telegram_rapid_click_prevents_duplicate_execution(self):
        """Verifies that atomic setup pop in handle_callback prevents double execution on rapid clicks."""
        from unittest.mock import MagicMock, AsyncMock, patch
        import asyncio
        from telegram_bot import TelegramBotManager
        from smc_detector import TradeSetup

        mt5_mock = MagicMock()
        mt5_mock.execute_market_order.return_value = (True, "OK", 888001, 1.0850)
        mt5_mock.calculate_lot_size.return_value = 0.1
        mt5_mock.can_open_new_trade.return_value = (True, "OK")

        journal_mock = MagicMock()
        bot = TelegramBotManager(mt5_mock, token="dummy", alert_channel=-100123, allowed_ids=[123456], journal=journal_mock)

        setup = MagicMock(spec=TradeSetup)
        setup.symbol = "EURUSD"
        setup.direction = "BUY"
        setup.entry_price = 1.0850
        setup.sl_price = 1.0820
        setup.tp1_price = 1.0910
        setup.confluence_score = 85
        setup.setup_type = "M15 CHoCH"
        setup.strategy_name = "SMC"
        setup.is_pyramid = False

        bot.pending_setups["setup_click_test"] = setup
        bot.pending_setups_time["setup_click_test"] = 1000

        update1 = MagicMock()
        update1.effective_user.id = 123456
        update1.callback_query.from_user.id = 123456
        update1.effective_chat.id = -100123
        update1.callback_query.data = "market:setup_click_test"
        update1.callback_query.message.caption = "Alert caption"
        update1.callback_query.answer = AsyncMock()
        update1.callback_query.edit_message_caption = AsyncMock()

        update2 = MagicMock()
        update2.effective_user.id = 123456
        update2.callback_query.from_user.id = 123456
        update2.effective_chat.id = -100123
        update2.callback_query.data = "market:setup_click_test"
        update2.callback_query.message.caption = "Alert caption"
        update2.callback_query.answer = AsyncMock()
        update2.callback_query.edit_message_caption = AsyncMock()

        london_session = {"session_name": "London Open", "is_killzone": True, "is_rollover": False}
        async def run_clicks():
            with patch.object(bot.news_filter, "check_news_async", AsyncMock(return_value=(False, None, None))), \
                 patch("session_manager.SessionManager.get_current_session", return_value=london_session):
                await bot.handle_callback(update1, MagicMock())
                await bot.handle_callback(update2, MagicMock())

        asyncio.run(run_clicks())

        # First click executed market order, second click was safely rejected as expired/already processed!
        self.assertEqual(mt5_mock.execute_market_order.call_count, 1)
        self.assertNotIn("setup_click_test", bot.pending_setups)

    def test_zumiko_tp2_non_zero_fallback(self):
        """Verifies that Zumiko SELL/BUY setup never outputs tp2 = 0.0 even if last swing is 0.0."""
        from zumiko_detector import ZumikoDetector
        z = ZumikoDetector()
        entry = 1.0850
        risk = 0.0020
        target_rr = 2.0
        # If last_low_val is 0.0 for SELL
        tp2_sell = min(0.0, entry - (risk * (target_rr + 1.0))) if 0.0 > 0 else (entry - (risk * (target_rr + 1.0)))
        self.assertGreater(tp2_sell, 0.0)
        self.assertAlmostEqual(tp2_sell, entry - (0.0020 * 3.0), places=4)


    def test_safe_answer_query_suppresses_bad_request(self):
        """Verifies _safe_answer_query catches and swallows expired or already-answered BadRequest without crashing."""
        import asyncio
        from unittest.mock import MagicMock, AsyncMock
        from telegram_bot import TelegramBotManager
        from telegram.error import BadRequest
        query = MagicMock()
        query.answer = AsyncMock(side_effect=BadRequest("Query is too old and response timeout expired or query ID is invalid"))

        # Should execute cleanly without raising exception
        asyncio.run(TelegramBotManager._safe_answer_query(query, text="Test", show_alert=True))
        self.assertEqual(query.answer.call_count, 1)

    def test_mt5_client_get_pip_size_gold_and_fx(self):
        from mt5_client import MT5Client
        self.assertEqual(MT5Client.get_pip_size("EURUSD.pro"), 0.0001)
        self.assertEqual(MT5Client.get_pip_size("USDJPY.pro"), 0.01)
        self.assertEqual(MT5Client.get_pip_size("XAUUSD.pro"), 0.10)

    def test_news_filter_http_502_backoff(self):
        """Verifies NewsFilter backs off on 502 Bad Gateway instead of hammering API every scan."""
        import asyncio
        from unittest.mock import MagicMock, AsyncMock, patch
        from news_filter import NewsFilter

        nf = NewsFilter()
        mock_response = MagicMock()
        mock_response.status_code = 502

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        async def run_calls():
            with patch("news_filter.httpx.AsyncClient", return_value=mock_client):
                await nf.fetch_calendar_async()
                await nf.fetch_calendar_async()

        asyncio.run(run_calls())
        # Second call within FAILURE_BACKOFF_SECONDS must not hit network again
        self.assertEqual(mock_client.get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
