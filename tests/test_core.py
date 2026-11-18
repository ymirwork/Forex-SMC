import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
import pandas as pd

from mt5_client import MT5Client
from news_filter import NewsFilter
from rr_optimizer import RROptimizer
from session_manager import SessionManager


class TestCurrencyParsing(unittest.TestCase):
    def test_standard_pairs(self):
        self.assertEqual(MT5Client._parse_currencies("EURUSD"), ("EUR", "USD"))
        self.assertEqual(MT5Client._parse_currencies("GBPUSD"), ("GBP", "USD"))
        self.assertEqual(MT5Client._parse_currencies("USDJPY"), ("USD", "JPY"))

    def test_broker_suffixes(self):
        self.assertEqual(MT5Client._parse_currencies("EURUSD.pro"), ("EUR", "USD"))
        self.assertEqual(MT5Client._parse_currencies("EURUSD.raw"), ("EUR", "USD"))
        self.assertEqual(MT5Client._parse_currencies("pro.EURUSD"), ("EUR", "USD"))
        self.assertEqual(MT5Client._parse_currencies("EURUSD.stp"), ("EUR", "USD"))
        self.assertEqual(MT5Client._parse_currencies("EURUSD.c"), ("EUR", "USD"))

    def test_single_char_suffixes(self):
        # Exness (m), Roboforex (p), IC Markets (f)
        self.assertEqual(MT5Client._parse_currencies("EURUSDm"), ("EUR", "USD"))
        self.assertEqual(MT5Client._parse_currencies("GBPUSDp"), ("GBP", "USD"))
        self.assertEqual(MT5Client._parse_currencies("USDJPYf"), ("USD", "JPY"))
        self.assertEqual(MT5Client._parse_currencies("USDCADc"), ("USD", "CAD"))

    def test_metals(self):
        self.assertEqual(MT5Client._parse_currencies("XAUUSD"), ("XAU", "USD"))
        self.assertEqual(MT5Client._parse_currencies("XAGUSD"), ("XAG", "USD"))
        self.assertEqual(MT5Client._parse_currencies("XAUUSD.pro"), ("XAU", "USD"))


class TestBreakEvenLogic(unittest.TestCase):
    def test_buy_break_even(self):
        entry = 1.1000
        dist = 0.0100
        # Exactly at entry
        self.assertTrue(MT5Client.is_position_at_be(entry, 1.1000, is_buy=True, initial_sl_dist=dist))
        # In profit
        self.assertTrue(MT5Client.is_position_at_be(entry, 1.1050, is_buy=True, initial_sl_dist=dist))
        # Within 15% tolerance
        self.assertTrue(MT5Client.is_position_at_be(entry, 1.0990, is_buy=True, initial_sl_dist=dist))
        # Still in loss
        self.assertFalse(MT5Client.is_position_at_be(entry, 1.0950, is_buy=True, initial_sl_dist=dist))
        # Zero SL
        self.assertFalse(MT5Client.is_position_at_be(entry, 0.0, is_buy=True, initial_sl_dist=dist))

    def test_sell_break_even(self):
        entry = 1.1000
        dist = 0.0100
        # Exactly at entry
        self.assertTrue(MT5Client.is_position_at_be(entry, 1.1000, is_buy=False, initial_sl_dist=dist))
        # In profit
        self.assertTrue(MT5Client.is_position_at_be(entry, 1.0950, is_buy=False, initial_sl_dist=dist))
        # Within 15% tolerance
        self.assertTrue(MT5Client.is_position_at_be(entry, 1.1010, is_buy=False, initial_sl_dist=dist))
        # Still in loss
        self.assertFalse(MT5Client.is_position_at_be(entry, 1.1050, is_buy=False, initial_sl_dist=dist))
        # Zero SL must NEVER be treated as Break-Even
        self.assertFalse(MT5Client.is_position_at_be(entry, 0.0, is_buy=False, initial_sl_dist=dist))
        self.assertFalse(MT5Client.is_position_at_be(entry, -1.0, is_buy=False, initial_sl_dist=dist))

    @patch("mt5_client.mt5")
    def test_symbol_info_stops_level_attribute_safety(self, mock_mt5):
        # Position mock
        mock_pos = MagicMock()
        mock_pos.ticket = 12345
        mock_pos.symbol = "EURUSD"
        mock_pos.type = 0  # BUY
        mock_pos.price_open = 1.0800
        mock_pos.price_current = 1.0830
        mock_pos.tp = 1.0900
        mock_mt5.positions_get.return_value = [mock_pos]

        # SymbolInfo with modern MT5 trade_stops_level
        mock_sym = MagicMock()
        mock_sym.digits = 5
        mock_sym.point = 0.00001
        mock_sym.trade_stops_level = 10
        mock_sym.stops_level = 0
        mock_mt5.symbol_info.return_value = mock_sym

        mock_send_res = MagicMock()
        mock_send_res.retcode = 10009  # TRADE_RETCODE_DONE
        mock_mt5.order_send.return_value = mock_send_res
        mock_mt5.TRADE_RETCODE_DONE = 10009
        mock_mt5.ORDER_TYPE_BUY = 0

        client = MT5Client()
        client.is_connected = True
        client.get_initial_sl = MagicMock(return_value=1.0700)

        ok, msg = client.move_sl_to_breakeven(12345, offset_pips=1.0)
        self.assertTrue(ok)
        self.assertIn("przestawiony na Break-Even", msg)

        # Verify call to order_send with trade_stops_level safety
        req = mock_mt5.order_send.call_args[0][0]
        self.assertEqual(req["symbol"], "EURUSD")
        self.assertGreaterEqual(req["sl"], 1.0800)

        # Fallback when trade_stops_level attribute does not exist (legacy)
        del mock_sym.trade_stops_level
        mock_sym.stops_level = 15
        ok2, msg2 = client.move_sl_to_breakeven(12345, offset_pips=1.0)
        self.assertTrue(ok2)



class TestRROptimizer(unittest.TestCase):
    def test_filters_out_open_trades(self):
        trades = [
            {"id": 1, "realized_r": 2.0, "max_r": 2.5, "status": "WIN"},
            {"id": 2, "realized_r": -1.0, "max_r": 0.4, "status": "LOSS"},
            {"id": 3, "realized_r": 1.8, "max_r": 3.0, "status": "WIN"},
            # Open trades that must not be counted as stop-loss
            {"id": 4, "realized_r": 0.0, "max_r": 0.2, "status": "OPEN"},
            {"id": 5, "realized_r": 0.0, "max_r": 0.5, "status": "OPEN"},
        ]
        result = RROptimizer.calculate_best_rr(trades)
        self.assertIsNotNone(result)
        # Should only evaluate the 3 closed trades
        self.assertEqual(result.total_trades, 3)
        self.assertGreater(result.best_rr, 1.0)

    def test_insufficient_closed_trades(self):
        trades = [
            {"id": 1, "realized_r": 2.0, "max_r": 2.5, "status": "WIN"},
            {"id": 2, "realized_r": 0.0, "max_r": 0.3, "status": "OPEN"},
        ]
        # Only 1 closed trade, minimum is 2
        result = RROptimizer.calculate_best_rr(trades)
        self.assertIsNone(result)


class TestNewsFilterCurrencies(unittest.TestCase):
    def setUp(self):
        self.nf = NewsFilter()

    def test_extract_currencies(self):
        self.assertEqual(self.nf._extract_currencies("EURUSD"), ["EUR", "USD"])
        self.assertEqual(self.nf._extract_currencies("EURUSDm"), ["EUR", "USD"])
        self.assertEqual(self.nf._extract_currencies("XAUUSD"), ["USD"])
        self.assertEqual(self.nf._extract_currencies("GOLD"), ["USD"])


class TestSessionManager(unittest.TestCase):
    def test_current_session_structure(self):
        dt = datetime(2026, 11, 18, 8, 30, tzinfo=timezone.utc)
        res = SessionManager.get_current_session(dt)
        self.assertIn("session_name", res)
        self.assertIn("is_killzone", res)
        self.assertIn("time_utc", res)
        self.assertEqual(res["session_name"], "London Open Killzone")
        self.assertTrue(res["is_killzone"])


class TestAntiSpamAndSLModes(unittest.TestCase):
    def test_telegram_dismiss_mute(self):
        from telegram_bot import TelegramBotManager
        bot = TelegramBotManager(None, token="dummy", alert_channel=123)
        self.assertFalse(bot.is_symbol_muted("EURUSD", "BUY"))
        bot.mute_symbol("EURUSD", "BUY", hours=1.0)
        self.assertTrue(bot.is_symbol_muted("EURUSD", "BUY"))
        self.assertFalse(bot.is_symbol_muted("EURUSD", "SELL"))
        self.assertFalse(bot.is_symbol_muted("GBPUSD", "BUY"))

    def test_sl_modes_config(self):
        import config
        self.assertIn(config.SL_CALCULATION_MODE, ("STRUCTURE", "ATR", "HYBRID"))
        self.assertGreater(config.SL_ATR_MULTIPLIER, 0.0)
        self.assertGreater(config.SL_HYBRID_MIN_ATR_MULT, 0.0)
        self.assertGreater(config.ALERT_COOLDOWN_MINUTES, 0)
        self.assertGreater(config.DISMISS_MUTE_HOURS, 0.0)


class TestStopLossCalculation(unittest.TestCase):
    def test_stop_loss_modes(self):
        import smc_detector
        from smc_detector import calculate_stop_loss
        
        orig_mode = smc_detector.SL_CALCULATION_MODE
        orig_mult = smc_detector.SL_ATR_MULTIPLIER
        orig_hybrid = smc_detector.SL_HYBRID_MIN_ATR_MULT
        try:
            smc_detector.SL_ATR_MULTIPLIER = 1.5
            smc_detector.SL_HYBRID_MIN_ATR_MULT = 0.8
            entry = 1.1000
            atr = 0.0010  # 10 pips
            spread = 0.0001  # 1 pip

            # 1. STRUCTURE MODE
            smc_detector.SL_CALCULATION_MODE = "STRUCTURE"
            sl, risk, mode = calculate_stop_loss(entry=entry, structural_sl=1.0980, atr=atr, is_buy=True, spread_buffer=spread)
            self.assertEqual(mode, "STRUCTURE")
            self.assertAlmostEqual(sl, 1.0980)
            self.assertAlmostEqual(risk, 0.0020)

            # 2. ATR MODE (BUY): entry - (1.5 * 0.0010 + 0.0001) = 1.1000 - 0.0016 = 1.0984
            smc_detector.SL_CALCULATION_MODE = "ATR"
            sl, risk, mode = calculate_stop_loss(entry=entry, structural_sl=1.0980, atr=atr, is_buy=True, spread_buffer=spread)
            self.assertEqual(mode, "ATR")
            self.assertAlmostEqual(sl, 1.0984)
            self.assertAlmostEqual(risk, 0.0016)

            # 3. HYBRID MODE (BUY - Structural is wider than min ATR):
            # min ATR risk = (0.8 * 0.0010) + 0.0001 = 0.0009. Structural risk = 0.0020.
            # max(0.0020, 0.0009) = 0.0020. sl = 1.0980
            smc_detector.SL_CALCULATION_MODE = "HYBRID"
            sl, risk, mode = calculate_stop_loss(entry=entry, structural_sl=1.0980, atr=atr, is_buy=True, spread_buffer=spread)
            self.assertEqual(mode, "HYBRID")
            self.assertAlmostEqual(sl, 1.0980)
            self.assertAlmostEqual(risk, 0.0020)

            # 4. HYBRID MODE (BUY - Structural is tighter than min ATR):
            # structural_sl = 1.0995 (5 pips risk). min ATR risk = 9 pips (0.0009).
            # max(0.0005, 0.0009) = 0.0009. sl = 1.1000 - 0.0009 = 1.0991
            sl, risk, mode = calculate_stop_loss(entry=entry, structural_sl=1.0995, atr=atr, is_buy=True, spread_buffer=spread)
            self.assertEqual(mode, "HYBRID")
            self.assertAlmostEqual(sl, 1.0991)
            self.assertAlmostEqual(risk, 0.0009)

            # 5. HYBRID MODE (SELL - Structural is tighter than min ATR):
            # entry = 1.1000. structural_sl = 1.1005 (5 pips).
            # sl = 1.1000 + 0.0009 = 1.1009
            sl, risk, mode = calculate_stop_loss(entry=entry, structural_sl=1.1005, atr=atr, is_buy=False, spread_buffer=spread)
            self.assertEqual(mode, "HYBRID")
            self.assertAlmostEqual(sl, 1.1009)
            self.assertAlmostEqual(risk, 0.0009)

            # 6. Invalid risk (too tight <= 5 points)
            sl, risk, mode = calculate_stop_loss(entry=entry, structural_sl=1.09999, atr=0.00001, is_buy=True, spread_buffer=0.0)
            self.assertIsNone(sl)
            self.assertIsNone(risk)
        finally:
            smc_detector.SL_CALCULATION_MODE = orig_mode
            smc_detector.SL_ATR_MULTIPLIER = orig_mult
            smc_detector.SL_HYBRID_MIN_ATR_MULT = orig_hybrid


class TestAuthorizationSecurity(unittest.TestCase):
    def test_unauthorized_channel_member_callback_rejected(self):
        from unittest.mock import MagicMock
        from telegram_bot import TelegramBotManager

        # Trader configures their personal user ID (123456) and a broadcast channel (-100999999)
        bot = TelegramBotManager(None, token="dummy", alert_channel=-100999999, allowed_ids=[123456, -100999999])

        # Authorized user clicking button
        update_auth = MagicMock()
        update_auth.callback_query = MagicMock()
        update_auth.effective_user.id = 123456
        update_auth.effective_chat.id = -100999999
        update_auth.callback_query.message.chat.id = -100999999
        self.assertTrue(bot._is_authorized(update_auth))

        # Random member in the channel clicking button (user ID 999999 not in allowed_chat_ids)
        update_unauth = MagicMock()
        update_unauth.callback_query = MagicMock()
        update_unauth.effective_user.id = 999999
        update_unauth.effective_chat.id = -100999999
        update_unauth.callback_query.message.chat.id = -100999999
        self.assertFalse(bot._is_authorized(update_unauth))


class TestPropFirmRiskMin(unittest.TestCase):
    @patch("MetaTrader5.symbol_info")
    @patch("MetaTrader5.account_info")
    @patch.object(MT5Client, "ensure_connection", return_value=True)
    def test_edge_filter_reduced_risk_preserved_in_prop_mode(self, mock_conn, mock_acc, mock_sym):
        from config import PROP_RISK_PER_TRADE_PERCENT
        sym_mock = MagicMock()
        sym_mock.digits = 5
        sym_mock.point = 0.00001
        sym_mock.trade_tick_value = 1.0
        sym_mock.trade_tick_size = 0.00001
        sym_mock.volume_min = 0.01
        sym_mock.volume_max = 100.0
        sym_mock.volume_step = 0.01
        mock_sym.return_value = sym_mock

        acc_mock = MagicMock()
        acc_mock.balance = 10000.0
        acc_mock.currency = "USD"
        mock_acc.return_value = acc_mock

        client = MT5Client()
        lot_full = client.calculate_lot_size("EURUSD", PROP_RISK_PER_TRADE_PERCENT, 1.07800, 1.08000)
        lot_halved = client.calculate_lot_size("EURUSD", 0.25, 1.07800, 1.08000)
        self.assertLess(lot_halved, lot_full)
        self.assertAlmostEqual(lot_halved, lot_full * 0.5, delta=0.02)


class TestPyramidMinLotRiskProtection(unittest.TestCase):
    @patch("MetaTrader5.symbol_info")
    @patch("MetaTrader5.account_info")
    @patch.object(MT5Client, "ensure_connection", return_value=True)
    def test_pyramid_rejects_when_min_lot_exceeds_locked_risk(self, mock_conn, mock_acc, mock_sym):
        sym_mock = MagicMock()
        sym_mock.digits = 5
        sym_mock.point = 0.00001
        sym_mock.trade_tick_value = 1.0
        sym_mock.trade_tick_size = 0.00001
        sym_mock.volume_min = 0.01
        sym_mock.volume_max = 100.0
        sym_mock.volume_step = 0.01
        mock_sym.return_value = sym_mock

        acc_mock = MagicMock()
        acc_mock.balance = 1000.0
        mock_acc.return_value = acc_mock

        cluster_status = {
            "direction": "BUY",
            "next_level": 2,
            "positions": [
                {
                    "ticket": 101,
                    "symbol": "EURUSD",
                    "type": "BUY",
                    "price_open": 1.08000,
                    "volume": 0.01,
                    "sl": 1.08010,
                }
            ],
        }
        client = MT5Client()
        lot, locked, allowed, reason = client.calculate_pyramid_position_size(
            symbol="EURUSD",
            entry_price=1.08200,
            sl_price=1.08010,
            setup_grade="A+",
            cluster_status=cluster_status,
        )
        self.assertEqual(lot, 0.0)
        self.assertIn("przekraczając dozwolone ryzyko", reason)


class TestAsianRangeMondayFallback(unittest.TestCase):
    def test_monday_fallback_to_friday(self):
        fri_bars = pd.date_range("2026-11-13 00:00", "2026-11-13 06:45", freq="15min")
        mon_bars = pd.date_range("2026-11-16 08:00", "2026-11-16 09:30", freq="15min")
        times = list(fri_bars) + list(mon_bars)

        highs = [1.0950 if i == 5 else 1.0930 for i in range(len(times))]
        lows = [1.0910 if i == 10 else 1.0920 for i in range(len(times))]
        closes = [1.0925] * len(times)

        df = pd.DataFrame({"time": times, "high": highs, "low": lows, "close": closes})

        with patch("session_manager.get_broker_utc_offset", return_value=0):
            # Monday at 09:30 (today's Asian session candles are absent from df, falls back to Friday)
            asia_h, asia_l = SessionManager.calculate_asian_range(df)
            self.assertEqual(asia_h, 1.0950)
            self.assertEqual(asia_l, 1.0910)


class TestTelegramCallbacksAndOrderExecution(unittest.IsolatedAsyncioTestCase):
    async def test_callback_dismiss_mutes_symbol(self):
        from telegram_bot import TelegramBotManager
        from smc_detector import TradeSetup
        mt5_mock = MagicMock()
        journal_mock = MagicMock()
        bot = TelegramBotManager(mt5_mock, token="dummy", alert_channel=-100123, allowed_ids=[123456], journal=journal_mock)

        setup = MagicMock(spec=TradeSetup)
        setup.symbol = "EURUSD"
        setup.direction = "BUY"
        bot.pending_setups["setup_1"] = setup
        bot.pending_setups_time["setup_1"] = 1000

        update = MagicMock()
        update.effective_user.id = 123456
        update.effective_chat.id = -100123
        update.callback_query.data = "dismiss:setup_1"
        update.callback_query.message.caption = "Old caption"
        update.callback_query.answer = unittest.mock.AsyncMock()
        update.callback_query.edit_message_caption = unittest.mock.AsyncMock()

        await bot.handle_callback(update, MagicMock())

        self.assertTrue(bot.is_symbol_muted("EURUSD", "BUY"))
        self.assertNotIn("setup_1", bot.pending_setups)

    async def test_callback_limit_order_logs_as_pending(self):
        from telegram_bot import TelegramBotManager
        from smc_detector import TradeSetup
        mt5_mock = MagicMock()
        mt5_mock.execute_limit_order.return_value = (True, "OK", 777001, 1.0850)
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
        setup.confluence_score = 80
        setup.setup_type = "M15 CHoCH"
        setup.strategy_name = "SMC"
        setup.is_pyramid = False
        bot.pending_setups["setup_2"] = setup
        bot.pending_setups_time["setup_2"] = 1000

        update = MagicMock()
        update.effective_user.id = 123456
        update.effective_chat.id = -100123
        update.callback_query.data = "limit:setup_2"
        update.callback_query.message.caption = "Old caption"
        update.callback_query.answer = unittest.mock.AsyncMock()
        update.callback_query.edit_message_caption = unittest.mock.AsyncMock()

        with patch.object(bot.news_filter, "check_news_async", unittest.mock.AsyncMock(return_value=(False, None, None))):
            await bot.handle_callback(update, MagicMock())

        mt5_mock.execute_limit_order.assert_called_once()
        journal_mock.add_trade.assert_called_once()
        _, kwargs = journal_mock.add_trade.call_args
        self.assertEqual(kwargs.get("status"), "PENDING")
        self.assertEqual(kwargs.get("mt5_ticket"), 777001)


if __name__ == "__main__":
    unittest.main()
