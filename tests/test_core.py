import unittest
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


if __name__ == "__main__":
    unittest.main()
