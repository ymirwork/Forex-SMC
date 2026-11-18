import unittest
from datetime import datetime, timezone
import pandas as pd
import numpy as np

from config import (
    BLOCK_ROLLOVER_SESSION,
    MIN_TRADES_FOR_OPTIMAL_RR,
    OPTIMAL_RR_MIN_FLOOR,
    MAX_SL_ATR_MULTIPLIER,
    REQUIRE_STRUCTURAL_SPACE_FOR_RR,
)
from rr_optimizer import RROptimizer, RROptimizerResult
from smc_detector import SMCDetector, calculate_stop_loss
from liquidity_detector import SweptLevel, LiquidityEvaluation
from session_manager import SessionManager


class TestSignalGuards(unittest.TestCase):
    def test_rr_optimizer_enforces_floor(self):
        """Verify that RROptimizer never picks an RR below OPTIMAL_RR_MIN_FLOOR (2.0)."""
        # Create synthetic trades where trades reached 1.1R - 1.3R (the exact scenario from the screenshot)
        trades = [
            {"status": "BE", "max_r": 1.15, "realized_r": 0.0},
            {"status": "BE", "max_r": 1.25, "realized_r": 0.0},
            {"status": "BE", "max_r": 1.12, "realized_r": 0.0},
            {"status": "BE", "max_r": 1.20, "realized_r": 0.0},
            {"status": "WIN", "max_r": 2.50, "realized_r": 2.0},
            {"status": "WIN", "max_r": 2.80, "realized_r": 2.0},
        ] * 6  # 36 trades

        opt_res = RROptimizer.calculate_best_rr(trades)
        self.assertIsNotNone(opt_res)
        self.assertGreaterEqual(opt_res.best_rr, OPTIMAL_RR_MIN_FLOOR)
        self.assertGreaterEqual(opt_res.best_rr, 2.0)

    def test_rr_optimizer_requires_min_trades(self):
        """Verify that RROptimizer does not activate with fewer than MIN_TRADES_FOR_OPTIMAL_RR (30)."""
        class MockJournal:
            def get_all_trades(self):
                # 15 trades is less than required 30
                return [{"status": "WIN", "max_r": 3.0, "realized_r": 2.0}] * 15

        target_rr, is_optimal, src = RROptimizer.get_effective_rr(
            journal=MockJournal(),
            default_rr=2.0,
            use_optimal_rr=True,
            min_trades=30,
        )
        self.assertFalse(is_optimal)
        self.assertEqual(target_rr, 2.0)

    def test_rollover_session_lock(self):
        """Verify that during Rollover hours, scan_for_setups returns empty list."""
        detector = SMCDetector()
        # 21:30 UTC is Rollover (23:30 broker time if offset is 2)
        rollover_dt = datetime(2026, 11, 18, 23, 30)
        sess = SessionManager.get_current_session(rollover_dt)
        self.assertIn("Rollover", sess["session_name"])

        # Dummy dataframe with 70 candles ending at rollover time
        dates = pd.date_range(end=rollover_dt, periods=70, freq="15min")
        df_ltf = pd.DataFrame({
            "time": dates,
            "open": np.linspace(174.5, 174.8, 70),
            "high": np.linspace(174.6, 174.9, 70),
            "low": np.linspace(174.4, 174.7, 70),
            "close": np.linspace(174.55, 174.85, 70),
        })

        setups = detector.scan_for_setups("EURJPY", df_ltf)
        self.assertEqual(len(setups), 0)

    def test_sweep_sl_placement_and_protection(self):
        """Verify that when a liquidity sweep is detected, SL is placed beyond the sweep level."""
        detector = SMCDetector()
        # Mock liquidity evaluation with swept EQH at 174.94
        swept_lvl = SweptLevel(
            name="EQH",
            price=174.94,
            level_type="EQUAL_HIGHS_LOWS",
            bars_ago=8,
            description="Equal Highs swept",
        )
        detector.liquidity_detector.evaluate_sweeps = lambda **kwargs: LiquidityEvaluation(
            has_sweep=True,
            swept_levels=[swept_lvl],
            confluence_weight=15,
            bonus_weight=0,
            summary_msg="Sweep EQH (174.94)",
        )

        # 70 candles with London Open time
        london_dt = datetime(2026, 11, 18, 10, 0)
        dates = pd.date_range(end=london_dt, periods=70, freq="15min")
        # Leg with impulse high at 174.95, low at 174.20
        highs = np.full(70, 174.80)
        highs[-8] = 174.95  # sweep bar
        lows = np.full(70, 174.30)
        lows[10] = 173.50   # impulse low deeper to allow > 2.0R space
        closes = np.full(70, 174.82)

        df_ltf = pd.DataFrame({
            "time": dates,
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
        })
        current_candle = df_ltf.iloc[-1]
        leg_df = df_ltf.iloc[10:]

        setup = detector._build_smc_setup(
            direction="SELL",
            symbol="EURJPY",
            leg_df=leg_df,
            df_ltf=df_ltf,
            swings_ltf=pd.DataFrame(),
            current_candle=current_candle,
            htf_trend="BEARISH",
            broken_level=174.50,
            target_rr=2.0,
            is_optimal_rr=False,
            rr_source="Default",
            session_info={"session_name": "London Open Killzone", "is_killzone": True},
            news_warn=None,
            current_time=london_dt,
            digits=2,
            point_scale=0.01,
            buffer=0.02,
            spread=0.01,
        )

        self.assertIsNotNone(setup)
        # SL must be strictly above the swept level 174.94 (and 174.95)
        self.assertGreater(setup.sl_price, 174.94)
        self.assertGreaterEqual(setup.sl_price, 174.95)

    def test_structural_space_filter_rejects_insufficient_room(self):
        """Verify that when structural low (TP2) is closer than risk * target_rr, setup is rejected."""
        detector = SMCDetector()
        london_dt = datetime(2026, 11, 18, 10, 0)
        dates = pd.date_range(end=london_dt, periods=70, freq="15min")
        
        # Current entry at 174.82, but impulse low (TP2) is at 174.70 (space is only 0.12, i.e. 12 pips)
        # While SL is at 174.90 (risk is 8 pips). For RR=2.0, required space is 16 pips > 12 pips!
        highs = np.full(70, 174.90)
        lows = np.full(70, 174.70)
        closes = np.full(70, 174.82)

        df_ltf = pd.DataFrame({
            "time": dates,
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
        })
        current_candle = df_ltf.iloc[-1]
        leg_df = df_ltf.iloc[10:]

        setup = detector._build_smc_setup(
            direction="SELL",
            symbol="EURJPY",
            leg_df=leg_df,
            df_ltf=df_ltf,
            swings_ltf=pd.DataFrame(),
            current_candle=current_candle,
            htf_trend="BEARISH",
            broken_level=174.75,
            target_rr=2.0,
            is_optimal_rr=False,
            rr_source="Default",
            session_info={"session_name": "London Open Killzone", "is_killzone": True},
            news_warn=None,
            current_time=london_dt,
            digits=2,
            point_scale=0.01,
            buffer=0.02,
            spread=0.01,
        )
        # Should be rejected because space to impulse low (174.70) is less than 2.0R
        self.assertIsNone(setup)

    def test_liquidity_sfp_sweep_validation(self):
        """Verify that LiquidityDetector validates true SFP wick rejection and rejects non-returning breaks."""
        from liquidity_detector import LiquidityDetector

        detector = LiquidityDetector()
        level = 1.1000

        # Scenario 1: True SFP wick sweep (low dipped to 1.0990, but closed back above at 1.1005)
        df_sweep = pd.DataFrame([
            {"open": 1.1010, "high": 1.1015, "low": 1.0990, "close": 1.1005},
        ])
        is_swept, bars_ago = detector._check_level_sweep_low(df_sweep, level, current_price=1.1010)
        self.assertTrue(is_swept)
        self.assertEqual(bars_ago, 0)

        # Scenario 2: Two-bar reversal (bar 1 closed below at 1.0992, but bar 2 closed back above at 1.1008)
        df_two_bar = pd.DataFrame([
            {"open": 1.1010, "high": 1.1012, "low": 1.0988, "close": 1.0992},
            {"open": 1.0992, "high": 1.1015, "low": 1.0990, "close": 1.1008},
        ])
        is_swept, bars_ago = detector._check_level_sweep_low(df_two_bar, level, current_price=1.1008)
        self.assertTrue(is_swept)

        # Scenario 3: Structural breakdown (closed below and stayed below, no close back above)
        df_break = pd.DataFrame([
            {"open": 1.1010, "high": 1.1012, "low": 1.0970, "close": 1.0975},
            {"open": 1.0975, "high": 1.0980, "low": 1.0965, "close": 1.0970},
        ])
        is_swept, _ = detector._check_level_sweep_low(df_break, level, current_price=1.1010)
        self.assertFalse(is_swept)

    def test_smc_tp_capping_at_external_liquidity(self):
        """Verify that when structural RR to swing high is less than target RR, TP1 is capped at the swing high."""
        detector = SMCDetector(min_confluence_score=40)
        # Entry at 1.0990, SL at 1.0978 (risk = 12 pips), impulse_high at 1.1030 (space = 40 pips -> ~3.3R)
        # target_rr is 3.5R (requires 42 pips > 40 pips). TP1 should be capped at impulse_high (1.1030) so price takes profit at BSL!
        setup = detector._build_smc_setup(
            direction="BUY",
            symbol="EURUSD",
            htf_trend="BULLISH",
            impulse_low=1.0970,
            impulse_high=1.1030,
            current_candle=pd.Series({"close": 1.0990, "open": 1.0995, "high": 1.0998, "low": 1.0988, "time": datetime.now(timezone.utc)}),
            df_ltf=pd.DataFrame([
                {"open": 1.0990, "high": 1.0995, "low": 1.0988, "close": 1.0990}
            ] * 20),
            swings_ltf=pd.DataFrame(),
            broken_level=1.1010,
            event_name="BOS",
            target_rr=3.5,
            digits=4,
            point_scale=0.0001,
            buffer=0.0005,
        )
        self.assertIsNotNone(setup)
        # TP1 must be capped at impulse_high (1.1030), not at 3.5R fantasy beyond structure
        self.assertEqual(setup.tp1_price, 1.1030)
        self.assertEqual(setup.setup_type, "M15 BOS + Fibo >= 61.8% Pullback (OTE)")

    def test_max_sl_atr_multiplier_filter(self):
        """Verify that when risk exceeds MAX_SL_ATR_MULTIPLIER * ATR, setup is rejected."""
        detector = SMCDetector(min_confluence_score=40)
        # ATR is (high - low) = 0.0010 (10 pips). Max allowed SL is 2.5 * ATR = 25 pips.
        # Here SL is 0.0035 (35 pips > 25 pips). Must be rejected!
        setup = detector._build_smc_setup(
            direction="BUY",
            symbol="EURUSD",
            impulse_low=1.0900,
            impulse_high=1.1100,
            current_candle=pd.Series({"close": 1.1000, "open": 1.1005, "high": 1.1008, "low": 1.0998, "time": datetime.now(timezone.utc)}),
            df_ltf=pd.DataFrame([
                {"open": 1.1000, "high": 1.1005, "low": 1.0995, "close": 1.1000}
            ] * 20),
            swings_ltf=pd.DataFrame(),
            broken_level=1.1050,
            event_name="BOS",
            target_rr=2.0,
            digits=4,
            point_scale=0.0001,
            buffer=0.0002,
        )
        # Structural SL in HYBRID mode or swing low is at 1.0900 (100 pips risk > 2.5 * 10 pips)
        self.assertIsNone(setup)

    def test_smc_confluence_penalizes_counter_trend_h4(self):
        """Verify that a counter-trend setup against H4 receives a -20% penalty."""
        detector = SMCDetector(min_confluence_score=70)
        # BUY setup against BEARISH H4
        setup = detector._build_smc_setup(
            direction="BUY",
            symbol="EURUSD",
            htf_trend="BEARISH",
            impulse_low=1.0970,
            impulse_high=1.1030,
            current_candle=pd.Series({"close": 1.0990, "open": 1.0995, "high": 1.0998, "low": 1.0988, "time": datetime.now(timezone.utc)}),
            df_ltf=pd.DataFrame([
                {"open": 1.0990, "high": 1.0995, "low": 1.0988, "close": 1.0990}
            ] * 20),
            swings_ltf=pd.DataFrame(),
            broken_level=1.1010,
            event_name="CHoCH",
            target_rr=2.0,
            digits=4,
            point_scale=0.0001,
            buffer=0.0005,
        )
        # Should be None because penalty brings score below 70%
        self.assertIsNone(setup)

    def test_smc_confluence_breakdown_factors(self):
        """Verify that aligned H4 + Killzone awards proper confluence weights."""
        detector = SMCDetector(min_confluence_score=30)
        setup = detector._build_smc_setup(
            direction="BUY",
            symbol="EURUSD",
            htf_trend="BULLISH",
            impulse_low=1.0970,
            impulse_high=1.1030,
            current_candle=pd.Series({"close": 1.0990, "open": 1.0995, "high": 1.0998, "low": 1.0988, "time": datetime.now(timezone.utc)}),
            df_ltf=pd.DataFrame([
                {"open": 1.0990, "high": 1.0995, "low": 1.0988, "close": 1.0990}
            ] * 20),
            swings_ltf=pd.DataFrame(),
            broken_level=1.1010,
            event_name="BOS",
            target_rr=2.0,
            digits=4,
            point_scale=0.0001,
            buffer=0.0005,
            session_info={"session_name": "London Open Killzone", "is_killzone": True},
        )
        self.assertIsNotNone(setup)
        breakdown_dict = {item.factor: item for item in setup.confluence_breakdown}
        # H4 trend should award 25%
        self.assertEqual(breakdown_dict["Zgodność z trendem H4"].weight, 25)
        self.assertTrue(breakdown_dict["Zgodność z trendem H4"].achieved)
        # Killzone should award 10%
        self.assertEqual(breakdown_dict["Timing Sesyjny (Killzone)"].weight, 10)
        self.assertTrue(breakdown_dict["Timing Sesyjny (Killzone)"].achieved)
        # Total score: 25 (H4) + 15 (structure) + 10 (killzone) + 5 (fibo 66%) = 55%
        self.assertEqual(setup.confluence_score, 55)

    def test_zumiko_killmlm_opposing_h4_penalty(self):
        """Verify that Zumiko KillMLM penalizes opposing H4 with -15% penalty and rejects trade below 70%."""
        from zumiko_detector import ZumikoDetector
        detector = ZumikoDetector(min_confluence_score=70)

        dates = pd.date_range(end=datetime(2026, 11, 18, 14, 0), periods=300, freq="15min")
        closes = np.linspace(1.1000, 1.1050, 300)
        df_m15 = pd.DataFrame({
            "time": dates,
            "open": closes - 0.0005,
            "high": closes + 0.0010,
            "low": closes - 0.0010,
            "close": closes,
        })
        e10 = pd.Series(closes)
        e21 = pd.Series(closes - 0.0005)

        # Call _evaluate_zumiko_direction with BUY but h4_dir="SELL" (opposing)
        setup = detector._evaluate_zumiko_direction(
            direction="BUY",
            symbol="EURUSD",
            df_m15=df_m15,
            c=1.1050,
            h=1.1055,
            l=1.1040,
            curr_e10=1.1048,
            curr_e21=1.1042,
            curr_e238=1.1010,
            prev_e10=1.1040,
            prev_e21=1.1041,
            e10=e10,
            e21=e21,
            last_high_val=1.1060,
            last_high_type="HH",
            last_low_val=1.1020,
            last_low_type="HL",
            h1_dir="BUY",
            h4_dir="SELL",  # Opposing!
            mtf={"M15": "BUY", "H1": "BUY", "H4": "SELL"},
            target_rr=2.0,
            is_optimal_rr=False,
            rr_source="Default",
            session_info={"session_name": "New York Open Killzone", "is_killzone": True},
            news_warn=None,
            current_time=datetime(2026, 11, 18, 14, 0),
            digits=4,
            point_scale=0.0001,
            buffer=0.0002,
        )
        # Should be None because H4 penalty (-15%) brings score below 70%
        self.assertIsNone(setup)

    def test_zumiko_killmlm_full_confluence(self):
        """Verify that Zumiko KillMLM with aligned H4 + Killzone achieves Grade A+ (100%)."""
        from zumiko_detector import ZumikoDetector
        detector = ZumikoDetector(min_confluence_score=70)

        dates = pd.date_range(end=datetime(2026, 11, 18, 14, 0), periods=300, freq="15min")
        closes = np.linspace(1.1000, 1.1050, 300)
        df_m15 = pd.DataFrame({
            "time": dates,
            "open": closes - 0.0005,
            "high": closes + 0.0010,
            "low": closes - 0.0010,
            "close": closes,
        })
        e10 = pd.Series(closes)
        e21 = pd.Series(closes - 0.0005)

        setup = detector._evaluate_zumiko_direction(
            direction="BUY",
            symbol="EURUSD",
            df_m15=df_m15,
            c=1.1050,
            h=1.1055,
            l=1.1040,
            curr_e10=1.1048,
            curr_e21=1.1042,
            curr_e238=1.1030,
            prev_e10=1.1040,
            prev_e21=1.1041,
            e10=e10,
            e21=e21,
            last_high_val=1.1060,
            last_high_type="HH",
            last_low_val=1.1020,
            last_low_type="HL",
            h1_dir="BUY",
            h4_dir="BUY",  # Full MTF!
            mtf={"M15": "BUY", "H1": "BUY", "H4": "BUY"},
            target_rr=2.0,
            is_optimal_rr=False,
            rr_source="Default",
            session_info={"session_name": "New York Open Killzone", "is_killzone": True},
            news_warn=None,
            current_time=datetime(2026, 11, 18, 14, 0),
            digits=4,
            point_scale=0.0001,
            buffer=0.0002,
        )
        self.assertIsNotNone(setup)
        # 30 (MTF) + 20 (EMA238) + 20 (fresh cross) + 15 (HL swing) + 15 (killzone) = 100%
        self.assertEqual(setup.confluence_score, 100)
        self.assertEqual(setup.grade, "A+")


if __name__ == "__main__":
    unittest.main()
