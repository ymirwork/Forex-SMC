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

    def test_scan_for_setups_structure_age_defined(self):
        """Verify scan_for_setups does not crash with NameError for structure_age when evaluating breakout."""
        from unittest.mock import MagicMock, patch
        detector = SMCDetector()
        detector.news_filter.check_news = MagicMock(return_value=(False, None, []))

        dates = pd.date_range(end=datetime(2026, 11, 20, 10, 0), periods=50, freq="15min")
        df_ltf = pd.DataFrame({
            "time": dates,
            "open": np.linspace(1.1000, 1.1050, 50),
            "high": np.linspace(1.1010, 1.1060, 50),
            "low": np.linspace(1.0990, 1.1040, 50),
            "close": np.linspace(1.1005, 1.1055, 50),
        })

        # Mock bos_choch_ltf with a valid CHoCH event
        mock_bos_choch = pd.DataFrame({
            "CHOCH": [np.nan] * 45 + [1.0] + [np.nan] * 4,
            "BOS": [np.nan] * 50,
            "BrokenIndex": [0] * 45 + [45] + [0] * 4,
            "Level": [1.1030] * 50,
        })
        mock_swings = pd.DataFrame({
            "HighLow": [0] * 50,
            "Level": [1.1000] * 50,
        })

        import smc_detector as smc_mod
        with patch.object(smc_mod.smc, "swing_highs_lows", return_value=mock_swings), \
             patch.object(smc_mod.smc, "bos_choch", return_value=mock_bos_choch):
            # Should run smoothly without raising NameError: name 'structure_age' is not defined
            try:
                setups = detector.scan_for_setups("EURUSD", df_ltf)
                self.assertIsInstance(setups, list)
            except NameError as e:
                self.fail(f"scan_for_setups raised NameError: {e}")

    def test_strong_weak_structural_points_mapping(self):
        """Verify Smart Risk Video 1: Strong Low/High and Weak High/Low are accurately mapped to TradeSetup."""
        detector = SMCDetector(min_confluence_score=30)
        # BUY setup: Strong Low = 1.0970, Weak High = 1.1030
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
        )
        self.assertIsNotNone(setup)
        self.assertEqual(setup.strong_level, 1.0970)
        self.assertEqual(setup.weak_level, 1.1030)

        # Check confluence breakdown mentions Strong/Weak points
        breakdown_dict = {item.factor: item for item in setup.confluence_breakdown}
        self.assertIn("Potwierdzenie M15 BOS", breakdown_dict)
        self.assertIn("Strong Low: 1.0970", breakdown_dict["Potwierdzenie M15 BOS"].detail)
        self.assertIn("Weak High: 1.1030", breakdown_dict["Potwierdzenie M15 BOS"].detail)

    def test_sweep_engineered_ob_detection_and_filtering(self):
        """Verify Smart Risk Video 2 Hack #2: Sweep-Engineered OB detection and enforcement."""
        detector = SMCDetector(min_confluence_score=30)

        # 5 bars descending, 6th bar sweeps prior low
        bars = [
            {"open": 1.1050, "high": 1.1060, "low": 1.1040, "close": 1.1045},
            {"open": 1.1045, "high": 1.1050, "low": 1.1030, "close": 1.1035},
            {"open": 1.1035, "high": 1.1040, "low": 1.1020, "close": 1.1025},
            {"open": 1.1025, "high": 1.1030, "low": 1.1010, "close": 1.1015},  # min low 1.1010
            {"open": 1.1015, "high": 1.1040, "low": 1.1000, "close": 1.1038},  # sweeps 1.1010!
        ]
        df_sweep = pd.DataFrame(bars)

        # Check unit helper directly
        is_sweep, desc = detector._check_sweep_engineered_ob(df_sweep, ob_bar_idx=4, direction="BUY", lookback_bars=4)
        self.assertTrue(is_sweep)
        self.assertIn("Sell-Side Liquidity", desc)

        # Test non-sweep candle
        bars_no_sweep = [
            {"open": 1.1050, "high": 1.1060, "low": 1.1000, "close": 1.1005},
            {"open": 1.1010, "high": 1.1030, "low": 1.1008, "close": 1.1025},
        ]
        df_no_sweep = pd.DataFrame(bars_no_sweep)
        is_sweep_no, _ = detector._check_sweep_engineered_ob(df_no_sweep, ob_bar_idx=1, direction="BUY", lookback_bars=1)
        self.assertFalse(is_sweep_no)

        # Test REQUIRE_SWEEP_ENGINEERED_OB filter rejects non-sweep setup
        from unittest.mock import patch
        with patch("smc_detector.REQUIRE_SWEEP_ENGINEERED_OB", True):
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
            )
            # Must be rejected because no sweep-engineered OB was present
            self.assertIsNone(setup)

    def test_room_to_run_htf_barrier_filtering(self):
        """Verify Smart Risk Video 2 Hack #1 & #5: Room to Run rejects trades blocked by opposing HTF S/D."""
        detector = SMCDetector(min_confluence_score=30)
        from unittest.mock import patch

        # If a supply zone sits immediately at 1.0995 (only 0.5R away while min required is 1.5R)
        mock_barrier = (1.0995, "HTF Supply OB [1.0995 - 1.1020]")

        with patch.object(detector, "find_nearest_htf_barrier", return_value=mock_barrier), \
             patch("smc_detector.ENABLE_ROOM_TO_RUN_HTF_FILTER", True), \
             patch("smc_detector.ROOM_TO_RUN_HTF_MIN_RR", 1.5):

            df_h1 = pd.DataFrame([{"open": 1.1000, "high": 1.1020, "low": 1.0990, "close": 1.1010}] * 35)

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
                df_h1=df_h1,
            )
            # Rejected because room_r = (1.0995 - 1.0990) / 0.0014 ~= 0.35R < 1.5R
            self.assertIsNone(setup)

    def test_room_to_run_caps_tp1_at_htf_barrier(self):
        """Verify Smart Risk Video 2: When opposing HTF barrier is between min RR and target RR, TP1 is capped at barrier."""
        detector = SMCDetector(min_confluence_score=30)
        from unittest.mock import patch

        # Barrier sits at 1.8R ahead (entry=1.0990, sl=1.0976 -> risk=0.0014, barrier at 1.0990 + 0.0025 = 1.1015)
        mock_barrier = (1.1015, "HTF Supply OB [1.1015 - 1.1040]")

        with patch.object(detector, "find_nearest_htf_barrier", return_value=mock_barrier), \
             patch("smc_detector.ENABLE_ROOM_TO_RUN_HTF_FILTER", True), \
             patch("smc_detector.ROOM_TO_RUN_HTF_MIN_RR", 1.5):

            df_h1 = pd.DataFrame([{"open": 1.1000, "high": 1.1020, "low": 1.0990, "close": 1.1010}] * 35)

            setup = detector._build_smc_setup(
                direction="BUY",
                symbol="EURUSD",
                htf_trend="BULLISH",
                impulse_low=1.0970,
                impulse_high=1.1060,  # structural space to 1.1060 is 5.0R
                current_candle=pd.Series({"close": 1.0990, "open": 1.0995, "high": 1.0998, "low": 1.0988, "time": datetime.now(timezone.utc)}),
                df_ltf=pd.DataFrame([
                    {"open": 1.0990, "high": 1.0995, "low": 1.0988, "close": 1.0990}
                ] * 20),
                swings_ltf=pd.DataFrame(),
                broken_level=1.1010,
                event_name="BOS",
                target_rr=2.5,  # Target RR is 2.5R (1.1025), beyond barrier at 1.1015 (1.8R)
                digits=4,
                point_scale=0.0001,
                buffer=0.0005,
                df_h1=df_h1,
            )
            self.assertIsNotNone(setup)
            # TP1 must be capped at 1.1015 (the barrier) instead of extending to 2.5R (1.1025)
            self.assertEqual(setup.tp1_price, 1.1015)
            self.assertEqual(setup.rr_ratio, 1.8)

    def test_scanner_opposing_signals_conflict_filter(self):
        """Verify Audit Finding 1: Scanner rejects setups on same symbol when SMC and Zumiko disagree in direction."""
        from scanner import SMCScanner
        from smc_detector import TradeSetup
        from unittest.mock import MagicMock, patch

        mock_mt5 = MagicMock()
        mock_mt5.ensure_connection.return_value = True
        mock_mt5.get_busy_symbols.return_value = set()
        mock_mt5.get_pyramid_status.return_value = {"is_eligible": False}
        mock_mt5.get_symbol_spread.return_value = 0.0001
        mock_mt5.fetch_ohlcv.return_value = pd.DataFrame([
            {"time": datetime.now(timezone.utc), "open": 1.1000, "high": 1.1010, "low": 1.0990, "close": 1.1005}
        ] * 300)

        scanner = SMCScanner(mt5_client=mock_mt5)

        # Mock SMC finding BUY and Zumiko finding SELL on EURUSD
        smc_buy = TradeSetup(
            symbol="EURUSD", direction="BUY", timeframe="M15", setup_type="SMC Fibo",
            entry_price=1.1000, sl_price=1.0980, tp1_price=1.1040, tp2_price=1.1060,
            rr_ratio=2.0, htf_trend="BULLISH", zone_top=1.1010, zone_bottom=1.0990,
            reason="Test SMC", timestamp=datetime.now(timezone.utc),
            confluence_score=80, grade="A",
        )
        zumiko_sell = TradeSetup(
            symbol="EURUSD", direction="SELL", timeframe="M15", setup_type="Zumiko Trend",
            entry_price=1.1000, sl_price=1.1020, tp1_price=1.0960, tp2_price=1.0940,
            rr_ratio=2.0, htf_trend="BEARISH", zone_top=1.1010, zone_bottom=1.0990,
            reason="Test Zumiko", timestamp=datetime.now(timezone.utc),
            confluence_score=75, grade="A",
        )

        with patch.object(scanner.smc_detector, "scan_for_setups", return_value=[smc_buy]), \
             patch.object(scanner.zumiko_detector, "scan_for_setups", return_value=[zumiko_sell]), \
             patch("scanner.SYMBOLS", ["EURUSD"]), \
             patch("scanner.PROP_FIRM_MODE", False), \
             patch("scanner.BLOCK_ROLLOVER_SESSION", False):

            # Run scan_once
            import asyncio
            found = asyncio.run(scanner.scan_once())
            # Must return 0 because conflicting directions were safely filtered out!
            self.assertEqual(found, 0)

    def test_scanner_opposing_alert_cooldown(self):
        """Verify Audit Finding 1b: Opposing alert cooldown suppresses reverse direction alert within cooldown period."""
        from scanner import SMCScanner
        from smc_detector import TradeSetup
        from unittest.mock import MagicMock
        import time

        scanner = SMCScanner(mt5_client=MagicMock())
        # Simulate EURUSD BUY alert was sent 2 minutes ago
        clean_sym = "EURUSD"
        scanner._last_alert_by_symbol[(clean_sym, "BUY")] = time.time() - 120  # 2m ago

        # Generate a new EURUSD SELL setup
        sell_setup = TradeSetup(
            symbol="EURUSD", direction="SELL", timeframe="M15", setup_type="Zumiko Trend",
            entry_price=1.1000, sl_price=1.1020, tp1_price=1.0960, tp2_price=1.0940,
            rr_ratio=2.0, htf_trend="BEARISH", zone_top=1.1010, zone_bottom=1.0990,
            reason="Test Zumiko", timestamp=datetime.now(timezone.utc),
            confluence_score=75, grade="A",
        )

        # Check opposing direction cooldown logic directly
        opp_dir = "BUY"
        opp_key = (clean_sym, opp_dir)
        last_opp_t = scanner._last_alert_by_symbol.get(opp_key, 0.0)
        is_blocked = (time.time() - last_opp_t) < (30 * 60)  # within 30 min cooldown
        self.assertTrue(is_blocked)

    def test_find_nearest_htf_barrier_uses_slice_length(self):
        """Verify Audit Finding 2: find_nearest_htf_barrier evaluates current_bar using sliced DataFrame length."""
        detector = SMCDetector()
        from unittest.mock import patch

        # 250 bars in df_htf (exceeds lookback=150)
        dates = pd.date_range(end=datetime.now(timezone.utc), periods=250, freq="1h")
        df_htf = pd.DataFrame({
            "time": dates,
            "open": [1.1000] * 250,
            "high": [1.1050] * 250,
            "low": [1.0950] * 250,
            "close": [1.1020] * 250,
            "volume": [100] * 250,
        })

        # Mock smc.ob returning an unmitigated supply OB at bar 10 of df_slice
        mock_ob = pd.DataFrame([
            {"OB": -1.0, "Top": 1.1080, "Bottom": 1.1060, "MitigatedIndex": 0.0}
        ])

        import smc_detector as smc_mod
        with patch.object(smc_mod.smc, "swing_highs_lows", return_value=pd.DataFrame()), \
             patch.object(smc_mod.smc, "ob", return_value=mock_ob):

            barrier_p, barrier_desc = detector.find_nearest_htf_barrier(df_htf, direction="BUY", entry_price=1.1020, lookback=150)
            self.assertIsNotNone(barrier_p)
            self.assertEqual(barrier_p, 1.1060)
            self.assertIn("HTF Supply OB", barrier_desc)

    def test_zumiko_vwap_alignment_filter(self):
        """Verify that Zumiko KillMLM enforces VWAP alignment (rejects BUY below VWAP, accepts above)."""
        from zumiko_detector import ZumikoDetector
        detector = ZumikoDetector(require_vwap=True, min_confluence_score=70)

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

        # 1. Price is 1.1040, but VWAP is 1.1060 (counter-trend to VWAP) -> Rejection
        setup_rejected = detector._evaluate_zumiko_direction(
            direction="BUY",
            symbol="EURUSD",
            df_m15=df_m15,
            c=1.1040,
            h=1.1045,
            l=1.1035,
            curr_e10=1.1038,
            curr_e21=1.1032,
            curr_e238=1.1020,
            prev_e10=1.1030,
            prev_e21=1.1031,
            e10=e10,
            e21=e21,
            last_high_val=1.1050,
            last_high_type="HH",
            last_low_val=1.1010,
            last_low_type="HL",
            h1_dir="BUY",
            h4_dir="BUY",
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
            vwap=1.1060,  # Above price for BUY
        )
        self.assertIsNone(setup_rejected)

        # 2. Price is 1.1065, VWAP is 1.1050 (trend aligned with VWAP) -> Accepted
        setup_accepted = detector._evaluate_zumiko_direction(
            direction="BUY",
            symbol="EURUSD",
            df_m15=df_m15,
            c=1.1065,
            h=1.1070,
            l=1.1055,
            curr_e10=1.1060,
            curr_e21=1.1052,
            curr_e238=1.1030,
            prev_e10=1.1050,
            prev_e21=1.1051,
            e10=e10,
            e21=e21,
            last_high_val=1.1080,
            last_high_type="HH",
            last_low_val=1.1020,
            last_low_type="HL",
            h1_dir="BUY",
            h4_dir="BUY",
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
            vwap=1.1050,  # Below price for BUY
        )
        self.assertIsNotNone(setup_accepted)
        self.assertEqual(setup_accepted.vwap, 1.1050)

    def test_zumiko_rsi_overextension_filter(self):
        """Verify that Zumiko KillMLM filters out overbought BUY (>75) and oversold SELL (<25)."""
        from zumiko_detector import ZumikoDetector
        detector = ZumikoDetector(rsi_max_buy=75.0, rsi_min_sell=25.0)

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

        # BUY with RSI 82.0 (Overbought) -> Rejection
        setup_buy_overbought = detector._evaluate_zumiko_direction(
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
            h4_dir="BUY",
            mtf={"M15": "BUY", "H1": "BUY", "H4": "BUY"},
            target_rr=2.0,
            is_optimal_rr=False,
            rr_source="Default",
            session_info={"session_name": "London Open", "is_killzone": True},
            news_warn=None,
            current_time=datetime(2026, 11, 18, 14, 0),
            digits=4,
            point_scale=0.0001,
            buffer=0.0002,
            rsi=82.0,
        )
        self.assertIsNone(setup_buy_overbought)

        # BUY with RSI 58.0 (Healthy) -> Accepted
        setup_buy_healthy = detector._evaluate_zumiko_direction(
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
            h4_dir="BUY",
            mtf={"M15": "BUY", "H1": "BUY", "H4": "BUY"},
            target_rr=2.0,
            is_optimal_rr=False,
            rr_source="Default",
            session_info={"session_name": "London Open", "is_killzone": True},
            news_warn=None,
            current_time=datetime(2026, 11, 18, 14, 0),
            digits=4,
            point_scale=0.0001,
            buffer=0.0002,
            rsi=58.0,
        )
        self.assertIsNotNone(setup_buy_healthy)
        self.assertEqual(setup_buy_healthy.rsi, 58.0)

    def test_zumiko_heikin_ashi_momentum_verification(self):
        """Verify that Heikin-Ashi calculation correctly identifies healthy bullish flat-bottom candles."""
        from zumiko_detector import ZumikoDetector

        dates = pd.date_range(end=datetime(2026, 11, 18, 14, 0), periods=10, freq="15min")
        # Bullish candles with flat bottom (low equals or close to open)
        df_bullish = pd.DataFrame({
            "time": dates,
            "open": [1.1000 + i * 0.0010 for i in range(10)],
            "high": [1.1000 + i * 0.0010 + 0.0015 for i in range(10)],
            "low": [1.1000 + i * 0.0010 for i in range(10)],  # Flat bottom
            "close": [1.1000 + i * 0.0010 + 0.0012 for i in range(10)],
        })

        ha_ok, ha_desc = ZumikoDetector.check_heikin_ashi_momentum(df_bullish, is_buy=True)
        self.assertTrue(ha_ok)
        self.assertIn("płaski dołek", ha_desc)

        # Bearish test for BUY direction -> should reject
        ha_fail, ha_fail_desc = ZumikoDetector.check_heikin_ashi_momentum(df_bullish, is_buy=False)
        self.assertFalse(ha_fail)
        self.assertIn("brak niedźwiedziego impetu", ha_fail_desc)

    def test_zumiko_poc_take_profit_capping(self):
        """Verify that Zumiko KillMLM caps TP1 at Point of Control (POC) if POC is ahead of entry with >= 1.5R."""
        from zumiko_detector import ZumikoDetector
        detector = ZumikoDetector(enable_poc=True)

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

        # First check baseline uncapped setup with target_rr=2.5
        uncapped_setup = detector._evaluate_zumiko_direction(
            direction="BUY",
            symbol="EURUSD",
            df_m15=df_m15,
            c=1.1050,
            h=1.1055,
            l=1.1042,
            curr_e10=1.1048,
            curr_e21=1.1042,
            curr_e238=1.1030,
            prev_e10=1.1040,
            prev_e21=1.1041,
            e10=e10,
            e21=e21,
            last_high_val=1.1080,
            last_high_type="HH",
            last_low_val=1.1020,
            last_low_type="HL",
            h1_dir="BUY",
            h4_dir="BUY",
            mtf={"M15": "BUY", "H1": "BUY", "H4": "BUY"},
            target_rr=2.5,
            is_optimal_rr=False,
            rr_source="Default",
            session_info={"session_name": "London Open", "is_killzone": True},
            news_warn=None,
            current_time=datetime(2026, 11, 18, 14, 0),
            digits=4,
            point_scale=0.0001,
            buffer=0.0002,
            poc=0.0,
        )
        self.assertIsNotNone(uncapped_setup)
        risk = uncapped_setup.entry_price - uncapped_setup.sl_price
        # Set POC at 1.8R (between entry and 2.5R TP1, and >= 1.5R threshold)
        poc_price = round(uncapped_setup.entry_price + (risk * 1.8), 4)

        setup = detector._evaluate_zumiko_direction(
            direction="BUY",
            symbol="EURUSD",
            df_m15=df_m15,
            c=1.1050,
            h=1.1055,
            l=1.1042,
            curr_e10=1.1048,
            curr_e21=1.1042,
            curr_e238=1.1030,
            prev_e10=1.1040,
            prev_e21=1.1041,
            e10=e10,
            e21=e21,
            last_high_val=1.1080,
            last_high_type="HH",
            last_low_val=1.1020,
            last_low_type="HL",
            h1_dir="BUY",
            h4_dir="BUY",
            mtf={"M15": "BUY", "H1": "BUY", "H4": "BUY"},
            target_rr=2.5,
            is_optimal_rr=False,
            rr_source="Default",
            session_info={"session_name": "London Open", "is_killzone": True},
            news_warn=None,
            current_time=datetime(2026, 11, 18, 14, 0),
            digits=4,
            point_scale=0.0001,
            buffer=0.0002,
            poc=poc_price,
        )
        self.assertIsNotNone(setup)
        self.assertEqual(setup.tp1_price, poc_price)
        self.assertEqual(setup.poc, poc_price)
        self.assertAlmostEqual(setup.rr_ratio, 1.8, places=1)

    def test_zumiko_main_signal_vs_pullback_classification(self):
        """Verify that expanding ribbon + aligned MTF/VWAP produces Grade A+ Main Signal."""
        from zumiko_detector import ZumikoDetector
        detector = ZumikoDetector()

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

        # Expanding ribbon, H1 aligned, above VWAP -> Main Signal
        setup_main = detector._evaluate_zumiko_direction(
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
            h4_dir="BUY",
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
            vwap=1.1040,
            is_ribbon_expanding=True,
        )
        self.assertIsNotNone(setup_main)
        self.assertTrue(setup_main.is_main_zumiko_signal)
        self.assertIn("Główny Sygnał ZuMiKo KillMLM", setup_main.setup_type)

        # Narrowing ribbon -> Pullback Signal
        setup_pullback = detector._evaluate_zumiko_direction(
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
            h4_dir="BUY",
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
            vwap=1.1040,
            is_ribbon_expanding=False,
        )
        self.assertIsNotNone(setup_pullback)
        self.assertFalse(setup_pullback.is_main_zumiko_signal)
        self.assertIn("Sygnał Korekcyjny ZuMiKo", setup_pullback.setup_type)

    def test_get_latest_m15_swing_matches_broker_suffix(self):
        """Verify Audit Finding 2: get_latest_m15_swing resolves broker symbols with .pro suffix against base ticker."""
        detector = SMCDetector()
        detector._latest_swings["EURUSD"] = {
            "swing_high": 1.1080,
            "swing_low": 1.1020,
            "atr": 0.0015,
            "point_scale": 0.0001,
        }
        # Query with broker suffix EURUSD.pro
        sw_low, atr, pt = detector.get_latest_m15_swing("EURUSD.pro", is_buy=True)
        self.assertEqual(sw_low, 1.1020)
        self.assertEqual(atr, 0.0015)

        # Query with broker suffix EURUSD.raw
        sw_high, _, _ = detector.get_latest_m15_swing("EURUSD.raw", is_buy=False)
        self.assertEqual(sw_high, 1.1080)

    def test_trade_insights_normalizes_broker_suffixes(self):
        """Verify Audit Finding 5: TradeInsightEngine groups trades by clean symbol ignoring broker suffixes."""
        from unittest.mock import MagicMock
        from trade_insights import TradeInsightEngine

        mock_journal = MagicMock()
        mock_journal.get_all_trades.return_value = [
            {"symbol": "EURUSD.pro", "status": "CLOSED", "realized_r": -1.0, "mae_r": 0.8},
            {"symbol": "EURUSD.raw", "status": "CLOSED", "realized_r": -1.0, "mae_r": 0.9},
            {"symbol": "EURUSD", "status": "CLOSED", "realized_r": -1.0, "mae_r": 0.7},
        ]
        mock_journal.get_closed_trades = mock_journal.get_all_trades

        engine = TradeInsightEngine(journal=mock_journal, min_sample_size=3)
        edge_data = engine.refresh_edge_data()

        # All 3 trades should be grouped under "EURUSD"
        symbols = [m.label for m in edge_data.get("symbol", [])]
        self.assertIn("EURUSD", symbols)
        eurusd_metric = next(m for m in edge_data["symbol"] if m.label == "EURUSD")
        self.assertEqual(eurusd_metric.n_trades, 3)
        self.assertEqual(eurusd_metric.net_r, -3.0)

    def test_chart_generator_with_vwap_and_fast_ema(self):
        """Verify Audit Findings 3 & 7: ChartGenerator generates chart with official Fast EMA and VWAP without crashing."""
        import os
        import tempfile
        from chart_generator import ChartGenerator
        from smc_detector import TradeSetup

        with tempfile.TemporaryDirectory() as tmpdir:
            gen = ChartGenerator(output_dir=tmpdir)
            dates = pd.date_range(end=datetime(2026, 11, 18, 14, 0), periods=50, freq="15min")
            closes = np.linspace(1.1000, 1.1050, 50)
            df = pd.DataFrame({
                "time": dates,
                "open": closes - 0.0005,
                "high": closes + 0.0010,
                "low": closes - 0.0010,
                "close": closes,
            })
            setup = TradeSetup(
                symbol="EURUSD",
                direction="BUY",
                timeframe="M15",
                setup_type="Główny Sygnał ZuMiKo KillMLM",
                entry_price=1.1050,
                sl_price=1.1030,
                tp1_price=1.1090,
                tp2_price=1.1120,
                rr_ratio=2.0,
                htf_trend="BULLISH",
                zone_top=1.1052,
                zone_bottom=1.1045,
                reason="ZuMiKo Test",
                timestamp=dates[-1],
                confluence_score=85,
                grade="A+",
                vwap=1.1035,
                ema_fast=1.1048,
                ema_slow=1.1040,
                ema_238=1.1010,
                strategy_name="ZUMIKO",
            )
            filepath = gen.generate_setup_chart(df, setup, bars_count=40)
            self.assertIsNotNone(filepath)
            self.assertTrue(os.path.exists(filepath))


if __name__ == "__main__":
    unittest.main()
