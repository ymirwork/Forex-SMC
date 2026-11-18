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


if __name__ == "__main__":
    unittest.main()
