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
        # Default target threshold should be 0.85R
        self.assertEqual(AUTO_BE_R_TRIGGER, 0.85)


if __name__ == "__main__":
    unittest.main()
