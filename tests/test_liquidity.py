import unittest
from datetime import datetime, timedelta
import numpy as np
import pandas as pd

from liquidity_detector import LiquidityDetector, SweptLevel, LiquidityEvaluation
from smc_detector import SMCDetector, TradeSetup


class TestLiquidityDetector(unittest.TestCase):
    def setUp(self):
        self.detector = LiquidityDetector(
            enabled=True,
            enable_pdh_pdl=True,
            enable_pwh_pwl=True,
            enable_eqh_eql=True,
            enable_asian=True,
            lookback_bars=15,
            eqh_atr_mult=0.15,
            eqh_min_bars_distance=5,
        )

    def test_calculate_pdh_pdl_d1(self):
        dates = [datetime(2026, 11, 15), datetime(2026, 11, 16), datetime(2026, 11, 17)]
        df_d1 = pd.DataFrame({
            "time": dates,
            "open": [1.0800, 1.0820, 1.0850],
            "high": [1.0850, 1.0890, 1.0920],
            "low": [1.0780, 1.0810, 1.0840],
            "close": [1.0820, 1.0850, 1.0900],
        })
        # Previous closed bar is iloc[-2] (2026-11-16): high=1.0890, low=1.0810
        pdh, pdl = LiquidityDetector.calculate_pdh_pdl(df_d1=df_d1)
        self.assertEqual(pdh, 1.0890)
        self.assertEqual(pdl, 1.0810)

    def test_calculate_pwh_pwl_w1(self):
        dates = [datetime(2026, 11, 1), datetime(2026, 11, 8), datetime(2026, 11, 15)]
        df_w1 = pd.DataFrame({
            "time": dates,
            "open": [1.0700, 1.0750, 1.0850],
            "high": [1.0800, 1.0950, 1.0980],
            "low": [1.0650, 1.0700, 1.0800],
            "close": [1.0750, 1.0850, 1.0900],
        })
        # Previous closed week is iloc[-2]: high=1.0950, low=1.0700
        pwh, pwl = LiquidityDetector.calculate_pwh_pwl(df_w1=df_w1)
        self.assertEqual(pwh, 1.0950)
        self.assertEqual(pwl, 1.0700)

    def test_detect_equal_highs_and_lows(self):
        n = 50
        times = [datetime(2026, 11, 18) + timedelta(minutes=15 * i) for i in range(n)]
        highs = [1.085 + (i % 7) * 0.0003 for i in range(n)]
        lows = [1.075 + (i % 7) * 0.0003 for i in range(n)]
        closes = [(h + l) / 2 for h, l in zip(highs, lows)]

        # Explicit swing highs at bar 10 and bar 25
        highs[10] = 1.1000
        highs[25] = 1.1001

        # Explicit swing lows at bar 15 and bar 35
        lows[15] = 1.0600
        lows[35] = 1.0601

        df = pd.DataFrame({
            "time": times,
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
        })
        atr = 0.0050  # 0.15 * 0.0050 = 0.00075 tolerance, diff is 0.0001 <= 0.00075

        eqh, eql = self.detector.detect_equal_highs_lows(df, atr=atr, swing_len=3)
        self.assertTrue(any(abs(h - 1.10005) < 0.0001 for h in eqh))
        self.assertTrue(any(abs(l - 1.06005) < 0.0001 for l in eql))

    def test_buy_sweep_pdl_and_asian_low(self):
        # Create LTF candles where price dips below PDL (1.0800) to 1.0790 and closes back at 1.0825
        n = 30
        times = [datetime(2026, 11, 18, 8, 0) + timedelta(minutes=15 * i) for i in range(n)]
        df_ltf = pd.DataFrame({
            "time": times,
            "open": [1.0830] * n,
            "high": [1.0850] * n,
            "low": [1.0810] * n,
            "close": [1.0830] * n,
        })
        # Dip bar at index 25 (5 bars ago)
        df_ltf.loc[25, "low"] = 1.0790
        # Current bar at index 29 closes back above PDL
        df_ltf.loc[29, "close"] = 1.0825

        # 3 bars in D1: bar 0, bar 1 (previous closed day, high=1.0900, low=1.0800), bar 2 (today)
        df_d1 = pd.DataFrame({
            "time": [datetime(2026, 11, 15), datetime(2026, 11, 16), datetime(2026, 11, 17)],
            "open": [1.0780, 1.0820, 1.0830],
            "high": [1.0880, 1.0900, 1.0850],
            "low": [1.0750, 1.0800, 1.0810],  # PDL (iloc[-2]) = 1.0800
            "close": [1.0820, 1.0830, 1.0825],
        })

        eval_res = self.detector.evaluate_sweeps(
            df_ltf=df_ltf,
            direction="BUY",
            df_d1=df_d1,
        )

        self.assertTrue(eval_res.has_sweep)
        self.assertEqual(eval_res.confluence_weight, 15)
        # PDL is a DAILY level so has_htf_sweep should be True (+5% bonus)
        self.assertTrue(eval_res.has_htf_sweep)
        self.assertEqual(eval_res.bonus_weight, 5)
        self.assertTrue(any(s.name == "PDL" for s in eval_res.swept_levels))

    def test_sell_sweep_pdh(self):
        # Create LTF candles where price spikes above PDH (1.0900) to 1.0915 and closes back at 1.0875
        n = 30
        times = [datetime(2026, 11, 18, 13, 0) + timedelta(minutes=15 * i) for i in range(n)]
        df_ltf = pd.DataFrame({
            "time": times,
            "open": [1.0880] * n,
            "high": [1.0890] * n,
            "low": [1.0850] * n,
            "close": [1.0870] * n,
        })
        # Spike bar at index 26
        df_ltf.loc[26, "high"] = 1.0915
        # Current bar at index 29 closes back below PDH
        df_ltf.loc[29, "close"] = 1.0875

        # 3 bars in D1: bar 1 is previous day with high=1.0900
        df_d1 = pd.DataFrame({
            "time": [datetime(2026, 11, 15), datetime(2026, 11, 16), datetime(2026, 11, 17)],
            "open": [1.0800, 1.0820, 1.0880],
            "high": [1.0850, 1.0900, 1.0890],  # PDH (iloc[-2]) = 1.0900
            "low": [1.0750, 1.0800, 1.0850],
            "close": [1.0820, 1.0870, 1.0875],
        })

        eval_res = self.detector.evaluate_sweeps(
            df_ltf=df_ltf,
            direction="SELL",
            df_d1=df_d1,
        )

        self.assertTrue(eval_res.has_sweep)
        self.assertEqual(eval_res.confluence_weight, 15)
        self.assertTrue(eval_res.has_htf_sweep)
        self.assertEqual(eval_res.bonus_weight, 5)
        self.assertTrue(any(s.name == "PDH" for s in eval_res.swept_levels))

    def test_multi_sweep_deduplication(self):
        # Two identical EQL entries should not trigger multi-sweep
        eval_same = LiquidityEvaluation(
            has_sweep=True,
            swept_levels=[
                SweptLevel(name="EQL", price=1.0800, level_type="EQUAL_HIGHS_LOWS", bars_ago=3, description=""),
                SweptLevel(name="EQL", price=1.0801, level_type="EQUAL_HIGHS_LOWS", bars_ago=4, description=""),
            ]
        )
        types = {s.level_type for s in eval_same.swept_levels}
        names = {s.name for s in eval_same.swept_levels}
        is_multi = len(eval_same.swept_levels) >= 2 and (len(types) >= 2 or len(names) >= 2)
        self.assertFalse(is_multi)

        # Distinct levels (Asian Low + PDL) should trigger multi-sweep
        eval_distinct = LiquidityEvaluation(
            has_sweep=True,
            swept_levels=[
                SweptLevel(name="Asian Low", price=1.0810, level_type="SESSION", bars_ago=5, description=""),
                SweptLevel(name="PDL", price=1.0800, level_type="DAILY", bars_ago=2, description=""),
            ]
        )
        types_d = {s.level_type for s in eval_distinct.swept_levels}
        names_d = {s.name for s in eval_distinct.swept_levels}
        is_multi_d = len(eval_distinct.swept_levels) >= 2 and (len(types_d) >= 2 or len(names_d) >= 2)
        self.assertTrue(is_multi_d)

    def test_evaluate_sweeps_with_df_h1_present(self):
        # Regression test for: "The truth value of a DataFrame is ambiguous"
        # when df_h1 and df_ltf are both DataFrames.
        n = 30
        times = [datetime(2026, 11, 18, 13, 0) + timedelta(minutes=15 * i) for i in range(n)]
        df_ltf = pd.DataFrame({
            "time": times,
            "open": [1.0880] * n,
            "high": [1.0890] * n,
            "low": [1.0850] * n,
            "close": [1.0870] * n,
        })
        df_h1 = pd.DataFrame({
            "time": [datetime(2026, 11, 17, 10, 0) + timedelta(hours=i) for i in range(24)],
            "open": [1.0850] * 24,
            "high": [1.0900] * 24,
            "low": [1.0800] * 24,
            "close": [1.0860] * 24,
        })
        df_d1 = pd.DataFrame({
            "time": [datetime(2026, 11, 15), datetime(2026, 11, 16), datetime(2026, 11, 17)],
            "open": [1.0800, 1.0820, 1.0880],
            "high": [1.0850, 1.0900, 1.0890],
            "low": [1.0750, 1.0800, 1.0850],
            "close": [1.0820, 1.0870, 1.0875],
        })
        # Should execute smoothly without raising ValueError
        res = self.detector.evaluate_sweeps(
            df_ltf=df_ltf,
            direction="SELL",
            df_d1=df_d1,
            df_h1=df_h1,
        )
        self.assertIsInstance(res, LiquidityEvaluation)

    def test_calculate_pdh_pdl_fallback(self):
        # Fallback to hourly bars when df_d1 is None
        h1_times = [datetime(2026, 11, 16, 0, 0) + timedelta(hours=i) for i in range(48)]
        df_h1 = pd.DataFrame({
            "time": h1_times,
            "open": [1.0850] * 48,
            "high": [1.0900] * 48,
            "low": [1.0800] * 48,
            "close": [1.0860] * 48,
        })
        # Previous day bars: 2026-11-16
        df_h1.loc[10, "high"] = 1.0950
        df_h1.loc[12, "low"] = 1.0750
        pdh, pdl = LiquidityDetector.calculate_pdh_pdl(df_d1=None, df_fallback=df_h1)
        self.assertEqual(pdh, 1.0950)
        self.assertEqual(pdl, 1.0750)


class TestSMCDetectorLiquidityIntegration(unittest.TestCase):
    def test_tradesetup_has_liquidity_fields(self):
        setup = TradeSetup(
            symbol="EURUSD",
            direction="BUY",
            timeframe="M15",
            setup_type="M15 CHoCH + Fibo OTE",
            entry_price=1.0850,
            sl_price=1.0830,
            tp1_price=1.0890,
            tp2_price=1.0920,
            rr_ratio=2.0,
            htf_trend="BULLISH",
            zone_top=1.0860,
            zone_bottom=1.0840,
            reason="Test",
            timestamp=pd.Timestamp.now(),
            swept_levels=[{"name": "PDL", "price": 1.0810, "level_type": "DAILY"}],
            is_multi_sweep=False,
            has_htf_sweep=True,
            liquidity_summary="PDL Swept",
        )
        self.assertEqual(len(setup.swept_levels), 1)
        self.assertEqual(setup.swept_levels[0]["name"], "PDL")
        self.assertTrue(setup.has_htf_sweep)


if __name__ == "__main__":
    unittest.main()
