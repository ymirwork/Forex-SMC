import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock

import pandas as pd

from adaptive_manager import AdaptivePositionManager, MarketRegime
from rr_optimizer import RROptimizer
from smc_detector import SMCDetector
from trade_journal import TradeJournal


class TestAdaptivePositionManager(unittest.TestCase):
    def setUp(self):
        self.manager = AdaptivePositionManager()

    def test_regime_classification_news_and_extreme_volatility(self):
        # News window active -> HIGH_VOLATILITY
        regime = self.manager.classify_regime("BULLISH", "BULLISH", atr_percentile=50.0, is_news_window=True)
        self.assertEqual(regime, MarketRegime.HIGH_VOLATILITY)

        # ATR percentile > 85.0 -> HIGH_VOLATILITY
        regime = self.manager.classify_regime("BULLISH", "BULLISH", atr_percentile=92.0, is_news_window=False)
        self.assertEqual(regime, MarketRegime.HIGH_VOLATILITY)

    def test_regime_classification_strong_trend(self):
        # HTF aligns with LTF and ATR in 40-85 percentile
        regime_bull = self.manager.classify_regime("BULLISH", "BULLISH", atr_percentile=60.0, is_news_window=False)
        self.assertEqual(regime_bull, MarketRegime.STRONG_TREND)

        regime_bear = self.manager.classify_regime("BEARISH", "BEARISH", atr_percentile=45.0, is_news_window=False)
        self.assertEqual(regime_bear, MarketRegime.STRONG_TREND)

        # Direction string normalization (BUY -> BULLISH, SELL -> BEARISH)
        regime_buy = self.manager.classify_regime("BULLISH", "BUY", atr_percentile=60.0, is_news_window=False)
        self.assertEqual(regime_buy, MarketRegime.STRONG_TREND)

        regime_sell = self.manager.classify_regime("BEARISH", "SELL", atr_percentile=50.0, is_news_window=False)
        self.assertEqual(regime_sell, MarketRegime.STRONG_TREND)

    def test_regime_classification_compression(self):
        # Conflicting timeframes
        regime_conflict = self.manager.classify_regime("BULLISH", "BEARISH", atr_percentile=60.0)
        self.assertEqual(regime_conflict, MarketRegime.COMPRESSION)

        # Low ATR percentile (< 40.0) even if aligned
        regime_low_vol = self.manager.classify_regime("BULLISH", "BULLISH", atr_percentile=25.0)
        self.assertEqual(regime_low_vol, MarketRegime.COMPRESSION)

        # Neutral HTF
        regime_neutral = self.manager.classify_regime("NEUTRAL", "BULLISH", atr_percentile=50.0)
        self.assertEqual(regime_neutral, MarketRegime.COMPRESSION)

    def test_dynamic_be_triggers(self):
        self.assertEqual(self.manager.get_dynamic_be_trigger(MarketRegime.STRONG_TREND), 1.15)
        self.assertEqual(self.manager.get_dynamic_be_trigger(MarketRegime.COMPRESSION), 0.80)
        self.assertEqual(self.manager.get_dynamic_be_trigger(MarketRegime.HIGH_VOLATILITY), 0.85)

    def test_pyramiding_qualification_gate(self):
        self.assertTrue(self.manager.can_pyramid(MarketRegime.STRONG_TREND))
        self.assertFalse(self.manager.can_pyramid(MarketRegime.COMPRESSION))
        self.assertFalse(self.manager.can_pyramid(MarketRegime.HIGH_VOLATILITY))

    def test_m15_trailing_sl_buy_valid(self):
        # BUY position: entry=1.0800, initial_sl=1.0750 (dist=50p), current_sl=1.0805 (at BE)
        # current_price=1.0920 (+2.4R)
        # m15_swing_low=1.0860, spread=0.0001, atr_m15=0.0015 (15p)
        should_trail, new_sl, reason = self.manager.evaluate_m15_trailing_sl(
            symbol="EURUSD",
            is_buy=True,
            current_sl=1.0805,
            current_price=1.0920,
            entry_price=1.0800,
            initial_sl_dist=0.0050,
            m15_swing_price=1.0860,
            atr_m15=0.0015,
            stops_level_points=10.0,
            point_scale=0.0001,
            spread=0.0001,
        )
        self.assertTrue(should_trail)
        # new_sl = swing_low - spread = 1.0860 - 0.0001 = 1.0859
        self.assertAlmostEqual(new_sl, 1.0859, places=4)
        self.assertIn("M15 Structural Trail", reason)

    def test_m15_trailing_sl_rejects_loss_or_unprotected_entry(self):
        # If swing price is below entry price, cannot trail
        should_trail, _, reason = self.manager.evaluate_m15_trailing_sl(
            symbol="EURUSD",
            is_buy=True,
            current_sl=1.0750,
            current_price=1.0810,
            entry_price=1.0800,
            initial_sl_dist=0.0050,
            m15_swing_price=1.0790,
            atr_m15=0.0015,
        )
        self.assertFalse(should_trail)
        self.assertIn("does not lock profit", reason)

    def test_m15_trailing_sl_rejects_too_close_to_market(self):
        # Current price = 1.0865, swing low = 1.0860 (only 5 pips away, but ATR=20 pips)
        should_trail, _, reason = self.manager.evaluate_m15_trailing_sl(
            symbol="EURUSD",
            is_buy=True,
            current_sl=1.0810,
            current_price=1.0865,
            entry_price=1.0800,
            initial_sl_dist=0.0050,
            m15_swing_price=1.0860,
            atr_m15=0.0020,
            stops_level_points=20.0,
        )
        self.assertFalse(should_trail)
        self.assertIn("too close to market", reason)

    def test_m15_trailing_sl_rejects_negligible_improvement(self):
        # Current SL is already 1.0850. New candidate is 1.0852 (only +0.04R improvement < 0.40R required)
        should_trail, _, reason = self.manager.evaluate_m15_trailing_sl(
            symbol="EURUSD",
            is_buy=True,
            current_sl=1.0850,
            current_price=1.0950,
            entry_price=1.0800,
            initial_sl_dist=0.0050,
            m15_swing_price=1.0853,
            atr_m15=0.0015,
        )
        self.assertFalse(should_trail)
        self.assertIn("SL improvement too small", reason)

    def test_m15_trailing_sl_sell_valid(self):
        # SELL position: entry=1.0800, initial_sl=1.0850 (dist=50p), current_sl=1.0795 (at BE)
        # current_price=1.0680 (+2.4R profit)
        # m15_swing_high=1.0740, spread=0.0001, atr_m15=0.0015 (15p)
        should_trail, new_sl, reason = self.manager.evaluate_m15_trailing_sl(
            symbol="EURUSD",
            is_buy=False,
            current_sl=1.0795,
            current_price=1.0680,
            entry_price=1.0800,
            initial_sl_dist=0.0050,
            m15_swing_price=1.0740,
            atr_m15=0.0015,
            stops_level_points=10.0,
            point_scale=0.0001,
            spread=0.0001,
        )
        self.assertTrue(should_trail)
        # new_sl = swing_high + spread = 1.0740 + 0.0001 = 1.0741
        self.assertAlmostEqual(new_sl, 1.0741, places=4)
        self.assertIn("M15 Structural Trail", reason)

    def test_m15_trailing_sl_sell_rejects_unprotected_entry(self):
        # For SELL, if candidate SL >= entry_price, cannot trail
        should_trail, _, reason = self.manager.evaluate_m15_trailing_sl(
            symbol="EURUSD",
            is_buy=False,
            current_sl=1.0850,
            current_price=1.0790,
            entry_price=1.0800,
            initial_sl_dist=0.0050,
            m15_swing_price=1.0810,
            atr_m15=0.0015,
        )
        self.assertFalse(should_trail)
        self.assertIn("does not lock profit", reason)

    def test_m15_trailing_sl_sell_rejects_too_close_to_market(self):
        # Current price = 1.0735, swing high = 1.0740 (candidate SL=1.0741, dist=6 pips, min_safe_dist=25 pips)
        should_trail, _, reason = self.manager.evaluate_m15_trailing_sl(
            symbol="EURUSD",
            is_buy=False,
            current_sl=1.0795,
            current_price=1.0735,
            entry_price=1.0800,
            initial_sl_dist=0.0050,
            m15_swing_price=1.0740,
            atr_m15=0.0020,
            stops_level_points=20.0,
        )
        self.assertFalse(should_trail)
        self.assertIn("too close to market", reason)


class TestRROptimizerMultiOutcomeSimulation(unittest.TestCase):
    def test_ev_simulation_protects_be_trades_from_counting_as_loss(self):
        # Trade 1: hit 3.0R (Win)
        # Trade 2: hit 1.5R peak, then reversed (with be_trigger_r=0.85, this is protected at BE!)
        # Trade 3: hit only 0.3R peak, then stopped out (-1.0R loss)
        trades = [
            {"status": "WIN", "realized_r": 3.0, "max_r": 3.2},
            {"status": "BE", "realized_r": 0.0, "max_r": 1.5},
            {"status": "LOSS", "realized_r": -1.0, "max_r": 0.3},
        ]

        # Test candidate RR = 2.0 with be_trigger_r = 0.85
        # Under the old binary formula:
        # Trade 1: Win (+2R)
        # Trade 2: max_r=1.5 < 2.0 -> Counted as -1.0R LOSS!
        # Trade 3: max_r=0.3 < 2.0 -> Counted as -1.0R LOSS!
        # Gross loss would be 2 * 1.0 = 2.0R, Net R = 0.0R!

        # Under the new empirical multi-outcome formula:
        # Trade 1: Win (+2R)
        # Trade 2: max_r=1.5 >= 0.85 (BE) -> Counted as 0.0R BE (Protected)!
        # Trade 3: max_r=0.3 < 0.85 -> Counted as -1.0R loss!
        # Gross gain = 2.0R, Gross loss = 1.0R, Net R = +1.0R!
        opt = RROptimizer.calculate_best_rr(trades, min_rr=2.0, max_rr=2.0, be_trigger_r=0.85)
        self.assertIsNotNone(opt)
        sim_row = opt.simulation_table[opt.simulation_table["target_rr"] == 2.0].iloc[0]

        self.assertEqual(sim_row["wins"], 1)
        self.assertEqual(sim_row["bes"], 1)
        self.assertEqual(sim_row["losses"], 1)
        self.assertEqual(sim_row["net_r"], 1.0)
        self.assertAlmostEqual(sim_row["ev"], 0.333, places=2)

    def test_counterfactual_mfe_utilized_in_simulation(self):
        # A trade closed at TP=1.8R, but post-close market ran to 3.5R (counterfactual_max_r=3.5)
        trades = [
            {"status": "WIN", "realized_r": 1.8, "max_r": 1.8, "counterfactual_max_r": 3.5},
            {"status": "LOSS", "realized_r": -1.0, "max_r": 0.2, "counterfactual_max_r": 0.2},
        ]
        # Testing candidate target 3.0R:
        # With counterfactual_max_r=3.5, trade 1 qualifies as WIN at 3.0R!
        opt = RROptimizer.calculate_best_rr(trades, min_rr=3.0, max_rr=3.0, be_trigger_r=0.85)
        self.assertIsNotNone(opt)
        sim_row = opt.simulation_table[opt.simulation_table["target_rr"] == 3.0].iloc[0]
        self.assertEqual(sim_row["wins"], 1)
        self.assertEqual(sim_row["losses"], 1)
        self.assertEqual(sim_row["net_r"], 2.0)  # +3.0R - 1.0R = +2.0R

    def test_regime_specific_optimization_filtering(self):
        trades = [
            {"status": "WIN", "realized_r": 3.0, "max_r": 3.5, "regime": "STRONG_TREND"},
            {"status": "WIN", "realized_r": 4.0, "max_r": 4.2, "regime": "STRONG_TREND"},
            {"status": "LOSS", "realized_r": -1.0, "max_r": 0.1, "regime": "STRONG_TREND"},
            {"status": "BE", "realized_r": 0.0, "max_r": 1.2, "regime": "COMPRESSION"},
            {"status": "LOSS", "realized_r": -1.0, "max_r": 0.5, "regime": "COMPRESSION"},
        ]

        # calculate_regime_rr with min_trades=2
        opt_trend = RROptimizer.calculate_regime_rr(trades, regime="STRONG_TREND", min_trades=2)
        self.assertIsNotNone(opt_trend)
        self.assertEqual(opt_trend.total_trades, 3)

        # COMPRESSION with min_trades=2
        opt_comp = RROptimizer.calculate_regime_rr(trades, regime="COMPRESSION", min_trades=2)
        self.assertIsNotNone(opt_comp)
        self.assertEqual(opt_comp.total_trades, 2)

        # Insufficient sample size returns None
        opt_insuff = RROptimizer.calculate_regime_rr(trades, regime="HIGH_VOLATILITY", min_trades=2)
        self.assertIsNone(opt_insuff)

    def test_profit_factor_zero_loss_zero_gain(self):
        # Two trades ended at BE: gross_gain = 0.0, gross_loss = 0.0
        trades = [
            {"status": "BE", "realized_r": 0.0, "max_r": 1.0},
            {"status": "BE", "realized_r": 0.0, "max_r": 1.2},
        ]
        opt = RROptimizer.calculate_best_rr(trades, min_rr=2.0, max_rr=2.0, be_trigger_r=0.85)
        self.assertIsNotNone(opt)
        sim_row = opt.simulation_table[opt.simulation_table["target_rr"] == 2.0].iloc[0]
        self.assertEqual(sim_row["profit_factor"], 0.0)

    def test_profit_factor_zero_loss_positive_gain(self):
        # Two trades ended at WIN: gross_gain = 4.0, gross_loss = 0.0
        trades = [
            {"status": "WIN", "realized_r": 2.5, "max_r": 2.5},
            {"status": "WIN", "realized_r": 3.0, "max_r": 3.0},
        ]
        opt = RROptimizer.calculate_best_rr(trades, min_rr=2.0, max_rr=2.0, be_trigger_r=0.85)
        self.assertIsNotNone(opt)
        sim_row = opt.simulation_table[opt.simulation_table["target_rr"] == 2.0].iloc[0]
        self.assertEqual(sim_row["profit_factor"], 99.0)


class TestSMCDetectorRRCache(unittest.TestCase):
    def test_rr_cache_avoids_repeated_journal_queries(self):
        mock_journal = MagicMock()
        mock_journal.get_all_trades.return_value = []
        detector = SMCDetector(journal=mock_journal, use_optimal_rr=False)

        # First call hits RROptimizer / sets cache
        rr1, is_opt1, src1 = detector.get_effective_rr(regime="STRONG_TREND", be_trigger_r=1.15)
        self.assertIn(("STRONG_TREND", 1.15), detector._rr_cache)

        # Modify cache payload directly to prove subsequent call returns from cache
        detector._rr_cache[("STRONG_TREND", 1.15)] = (detector._rr_cache[("STRONG_TREND", 1.15)][0], (4.5, True, "CACHED_VAL"))
        rr2, is_opt2, src2 = detector.get_effective_rr(regime="STRONG_TREND", be_trigger_r=1.15)
        self.assertEqual(rr2, 4.5)
        self.assertEqual(src2, "CACHED_VAL")


class TestTradeJournalAdaptiveIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_trades.db")
        self.journal = TradeJournal(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_add_trade_persists_regime_and_counterfactual_mfe(self):
        t_id = self.journal.add_trade(
            symbol="EURUSD",
            direction="BUY",
            entry_price=1.0800,
            sl_price=1.0750,
            tp_price=1.0950,
            regime="STRONG_TREND",
            counterfactual_max_r=3.2,
        )
        self.assertGreater(t_id, 0)

        trades = self.journal.get_all_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["regime"], "STRONG_TREND")
        self.assertEqual(trades[0]["counterfactual_max_r"], 3.2)

    def test_update_trade_counterfactual_mfe_and_regime(self):
        t_id = self.journal.add_trade(
            symbol="GBPUSD",
            direction="SELL",
            entry_price=1.2600,
            sl_price=1.2650,
            tp_price=1.2500,
            mt5_ticket=888999,
        )
        ok_cf = self.journal.update_trade_counterfactual_max_r(888999, 4.5)
        self.assertTrue(ok_cf)

        ok_reg = self.journal.update_trade_regime(888999, "HIGH_VOLATILITY")
        self.assertTrue(ok_reg)

        trade = self.journal.get_trade_by_ticket(888999)
        self.assertIsNotNone(trade)
        self.assertEqual(trade["counterfactual_max_r"], 4.5)
        self.assertEqual(trade["regime"], "HIGH_VOLATILITY")

    def test_get_trades_by_regime(self):
        self.journal.add_trade(
            symbol="USDJPY", direction="BUY", entry_price=150.0, sl_price=149.0, tp_price=152.0,
            status="CLOSED", regime="STRONG_TREND"
        )
        self.journal.add_trade(
            symbol="EURUSD", direction="SELL", entry_price=1.08, sl_price=1.085, tp_price=1.07,
            status="CLOSED", regime="COMPRESSION"
        )

        trend_trades = self.journal.get_trades_by_regime("STRONG_TREND")
        self.assertEqual(len(trend_trades), 1)
        self.assertEqual(trend_trades[0]["symbol"], "USDJPY")


if __name__ == "__main__":
    unittest.main()
