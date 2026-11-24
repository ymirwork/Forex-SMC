import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import numpy as np
import pandas as pd

from config import ENABLE_SWAP_ZONE_STRATEGY
from smc_detector import SMCDetector, TradeSetup


class TestSwapZone(unittest.TestCase):
    def setUp(self):
        self.detector = SMCDetector(min_confluence_score=60)
        self.detector.news_filter.check_news = MagicMock(return_value=(False, None, []))

    def _create_base_df(self, n_bars=50):
        dates = pd.date_range(end=datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc), periods=n_bars, freq="15min")
        df = pd.DataFrame({
            "time": dates,
            "open": [1.0850] * n_bars,
            "high": [1.0860] * n_bars,
            "low": [1.0840] * n_bars,
            "close": [1.0850] * n_bars,
            "volume": [100.0] * n_bars,
        })
        return df

    @patch("smc_detector.smc.ob")
    def test_bearish_swap_zone_detection(self, mock_ob):
        """
        Test Bearish Swap Zone (Demand -> Supply Flip):
        - Bullish OB at bar 10 with [Bottom=1.0850, Top=1.0870].
        - Price breaks down at bar 20 with close=1.0830 < 1.0850.
        - Price reaches low=1.0800 (clear departure).
        - Price retraces back up to 1.0855 (inside zone) with rejection wick.
        - Result: Valid SELL TradeSetup.
        """
        df = self._create_base_df(45)

        # Build realistic candles
        # Bar 10: OB formed
        # Bar 11-19: rallying above
        for i in range(11, 20):
            df.loc[i, "open"] = 1.0880
            df.loc[i, "close"] = 1.0890
            df.loc[i, "high"] = 1.0895
            df.loc[i, "low"] = 1.0875

        # Bar 20: Breakdown candle closing below bottom (1.0850)
        df.loc[20, "open"] = 1.0860
        df.loc[20, "high"] = 1.0865
        df.loc[20, "low"] = 1.0825
        df.loc[20, "close"] = 1.0830

        # Bar 21-35: Lower prices (departure reaching 1.0800)
        for i in range(21, 36):
            df.loc[i, "open"] = 1.0820
            df.loc[i, "close"] = 1.0815
            df.loc[i, "high"] = 1.0825
            df.loc[i, "low"] = 1.0800

        # Bar 36-44: Retracement back up into zone [1.0850 - 1.0870]
        for i in range(36, 44):
            df.loc[i, "open"] = 1.0830 + (i - 36) * 0.0003
            df.loc[i, "close"] = 1.0835 + (i - 36) * 0.0003
            df.loc[i, "high"] = df.loc[i, "close"] + 0.0005
            df.loc[i, "low"] = df.loc[i, "open"] - 0.0005

        # Current candle (bar 44): inside the zone with upper rejection wick
        df.loc[44, "open"] = 1.0858
        df.loc[44, "high"] = 1.0868  # Tested inside 1.0850 - 1.0870
        df.loc[44, "low"] = 1.0850
        df.loc[44, "close"] = 1.0852  # Bearish close with upper rejection

        # Mock OB dataframe from smart_money_concepts
        ob_data = pd.DataFrame(index=df.index)
        ob_data["OB"] = np.nan
        ob_data["Top"] = np.nan
        ob_data["Bottom"] = np.nan
        ob_data.loc[10, "OB"] = 1.0  # Bullish OB (Demand)
        ob_data.loc[10, "Top"] = 1.0870
        ob_data.loc[10, "Bottom"] = 1.0850
        mock_ob.return_value = ob_data

        swings_dummy = pd.DataFrame(index=df.index)
        current_candle = df.iloc[-1]
        session_info = {"session_name": "London", "is_killzone": True}

        setups = self.detector._detect_swap_zones(
            symbol="EURUSD",
            df_ltf=df,
            swings_ltf=swings_dummy,
            current_candle=current_candle,
            current_time=current_candle["time"],
            digits=4,
            point_scale=0.0001,
            buffer=0.0005,
            spread=0.0001,
            htf_trend="BEARISH",
            session_info=session_info,
            news_warn=None,
        )

        self.assertEqual(len(setups), 1)
        setup = setups[0]
        self.assertEqual(setup.direction, "SELL")
        self.assertIn("Swap Zone Retest", setup.setup_type)
        self.assertIn("Demand ➔ Supply", setup.setup_type)
        self.assertGreaterEqual(setup.sl_price, 1.0870)  # SL above broken zone
        self.assertLessEqual(setup.tp1_price, 1.0810)    # TP targeting the impulse low
        self.assertGreaterEqual(setup.confluence_score, 70)
        self.assertIn(setup.grade, ["A+", "A"])

    @patch("smc_detector.smc.ob")
    def test_bullish_swap_zone_detection(self, mock_ob):
        """
        Test Bullish Swap Zone (Supply -> Demand Flip):
        - Bearish OB at bar 10 with [Bottom=1.0820, Top=1.0840].
        - Price breaks up at bar 20 with close=1.0855 > 1.0840.
        - Price reaches high=1.0890 (clear departure).
        - Price retraces back down to 1.0835 (inside zone) with lower rejection wick.
        - Result: Valid BUY TradeSetup.
        """
        df = self._create_base_df(45)

        # Bar 11-19: dropping below
        for i in range(11, 20):
            df.loc[i, "open"] = 1.0810
            df.loc[i, "close"] = 1.0800
            df.loc[i, "high"] = 1.0815
            df.loc[i, "low"] = 1.0795

        # Bar 20: Breakout candle closing above top (1.0840)
        df.loc[20, "open"] = 1.0830
        df.loc[20, "high"] = 1.0860
        df.loc[20, "low"] = 1.0825
        df.loc[20, "close"] = 1.0855

        # Bar 21-35: Higher prices (departure reaching 1.0890)
        for i in range(21, 36):
            df.loc[i, "open"] = 1.0870
            df.loc[i, "close"] = 1.0885
            df.loc[i, "high"] = 1.0890
            df.loc[i, "low"] = 1.0865

        # Bar 36-44: Retracement back down into zone [1.0820 - 1.0840]
        for i in range(36, 44):
            df.loc[i, "open"] = 1.0860 - (i - 36) * 0.0003
            df.loc[i, "close"] = 1.0855 - (i - 36) * 0.0003
            df.loc[i, "high"] = df.loc[i, "open"] + 0.0005
            df.loc[i, "low"] = df.loc[i, "close"] - 0.0005

        # Current candle (bar 44): inside zone with lower rejection wick
        df.loc[44, "open"] = 1.0835
        df.loc[44, "high"] = 1.0845
        df.loc[44, "low"] = 1.0825  # Tested inside 1.0820 - 1.0840
        df.loc[44, "close"] = 1.0838  # Bullish close with lower wick

        ob_data = pd.DataFrame(index=df.index)
        ob_data["OB"] = np.nan
        ob_data["Top"] = np.nan
        ob_data["Bottom"] = np.nan
        ob_data.loc[10, "OB"] = -1.0  # Bearish OB (Supply)
        ob_data.loc[10, "Top"] = 1.0840
        ob_data.loc[10, "Bottom"] = 1.0820
        mock_ob.return_value = ob_data

        swings_dummy = pd.DataFrame(index=df.index)
        current_candle = df.iloc[-1]
        session_info = {"session_name": "New York", "is_killzone": True}

        setups = self.detector._detect_swap_zones(
            symbol="EURUSD",
            df_ltf=df,
            swings_ltf=swings_dummy,
            current_candle=current_candle,
            current_time=current_candle["time"],
            digits=4,
            point_scale=0.0001,
            buffer=0.0005,
            spread=0.0001,
            htf_trend="BULLISH",
            session_info=session_info,
            news_warn=None,
        )

        self.assertEqual(len(setups), 1)
        setup = setups[0]
        self.assertEqual(setup.direction, "BUY")
        self.assertIn("Swap Zone Retest", setup.setup_type)
        self.assertIn("Supply ➔ Demand", setup.setup_type)
        self.assertLessEqual(setup.sl_price, 1.0820)    # SL below broken zone
        self.assertGreaterEqual(setup.tp1_price, 1.0880) # TP targeting the impulse high
        self.assertGreaterEqual(setup.confluence_score, 70)

    @patch("smc_detector.smc.ob")
    def test_swap_zone_invalidation_when_blown_through(self, mock_ob):
        """
        Verify that if price blows through the opposite boundary of the zone
        after the initial break, the Swap Zone is invalidated and no setup is returned.
        """
        df = self._create_base_df(45)

        # Bullish OB at 10 [1.0850 - 1.0870]
        # Break below at bar 20
        df.loc[20, "close"] = 1.0830
        df.loc[21, "low"] = 1.0800

        # Bar 30 blows completely above the top (1.0870)
        df.loc[30, "close"] = 1.0890

        # Current candle inside zone
        df.loc[44, "high"] = 1.0865
        df.loc[44, "low"] = 1.0852
        df.loc[44, "close"] = 1.0855

        ob_data = pd.DataFrame(index=df.index)
        ob_data["OB"] = np.nan
        ob_data.loc[10, "OB"] = 1.0
        ob_data.loc[10, "Top"] = 1.0870
        ob_data.loc[10, "Bottom"] = 1.0850
        mock_ob.return_value = ob_data

        swings_dummy = pd.DataFrame(index=df.index)
        setups = self.detector._detect_swap_zones(
            symbol="EURUSD",
            df_ltf=df,
            swings_ltf=swings_dummy,
            current_candle=df.iloc[-1],
            current_time=df.iloc[-1]["time"],
            digits=4,
            point_scale=0.0001,
            buffer=0.0005,
            spread=0.0001,
            htf_trend="BEARISH",
            session_info={"session_name": "London", "is_killzone": True},
            news_warn=None,
        )

        self.assertEqual(len(setups), 0)

    def test_swap_zone_disabled_by_config(self):
        """Verify that when ENABLE_SWAP_ZONE_STRATEGY is False, no swap setups are evaluated."""
        with patch("smc_detector.ENABLE_SWAP_ZONE_STRATEGY", False):
            setups = self.detector._detect_swap_zones(
                symbol="EURUSD",
                df_ltf=self._create_base_df(45),
                swings_ltf=pd.DataFrame(),
                current_candle=pd.Series({"close": 1.0850, "open": 1.0850, "high": 1.0860, "low": 1.0840}),
                current_time=datetime.now(timezone.utc),
                digits=4,
                point_scale=0.0001,
                buffer=0.0005,
                spread=0.0001,
                htf_trend="BEARISH",
                session_info={},
                news_warn=None,
            )
            self.assertEqual(len(setups), 0)

    @patch("smc_detector.smc.ob")
    def test_swap_zone_room_to_run_rejected(self, mock_ob):
        """
        Verify that if an opposing HTF barrier is too close (< 1.5R),
        the Swap Zone trade is rejected by the room-to-run filter.
        """
        df = self._create_base_df(45)
        # Bullish OB at 10 [1.0850 - 1.0870] broken downwards
        df.loc[20, "close"] = 1.0830
        df.loc[25, "low"] = 1.0800
        df.loc[44, "high"] = 1.0860
        df.loc[44, "low"] = 1.0850
        df.loc[44, "close"] = 1.0852
        df.loc[44, "open"] = 1.0858

        ob_data = pd.DataFrame(index=df.index)
        ob_data["OB"] = np.nan
        ob_data.loc[10, "OB"] = 1.0
        ob_data.loc[10, "Top"] = 1.0870
        ob_data.loc[10, "Bottom"] = 1.0850
        mock_ob.return_value = ob_data

        # Mock opposing HTF barrier at 1.0845 (only 0.0007 below entry 1.0852, when risk is 0.0023 -> 0.3R < 1.5R)
        self.detector._find_nearest_opposing_htf_barrier = MagicMock(
            return_value=(1.0845, "HTF Demand OB [1.0835 - 1.0845]")
        )

        setups = self.detector._detect_swap_zones(
            symbol="EURUSD",
            df_ltf=df,
            swings_ltf=pd.DataFrame(index=df.index),
            current_candle=df.iloc[-1],
            current_time=df.iloc[-1]["time"],
            digits=4,
            point_scale=0.0001,
            buffer=0.0005,
            spread=0.0001,
            htf_trend="BEARISH",
            session_info={"session_name": "London", "is_killzone": True},
            news_warn=None,
            df_h1=df,
        )

        self.assertEqual(len(setups), 0)

    @patch("smc_detector.smc.ob")
    def test_swap_zone_tp_capping(self, mock_ob):
        """
        Verify that if an opposing HTF barrier allows >= 1.5R but is closer than TP1,
        TP1 is safely capped to the HTF barrier price.
        """
        df = self._create_base_df(45)
        # Bullish OB at 10 [1.0850 - 1.0870] broken downwards
        df.loc[20, "close"] = 1.0830
        df.loc[25, "low"] = 1.0750  # Far away low (original target)
        df.loc[44, "high"] = 1.0860
        df.loc[44, "low"] = 1.0850
        df.loc[44, "close"] = 1.0852
        df.loc[44, "open"] = 1.0858

        ob_data = pd.DataFrame(index=df.index)
        ob_data["OB"] = np.nan
        ob_data.loc[10, "OB"] = 1.0
        ob_data.loc[10, "Top"] = 1.0870
        ob_data.loc[10, "Bottom"] = 1.0850
        mock_ob.return_value = ob_data

        # Mock opposing HTF barrier at 1.0805 (1.0852 - 1.0805 = 0.0047, risk is ~0.0023 -> 2.0R, but above 1.0750)
        self.detector._find_nearest_opposing_htf_barrier = MagicMock(
            return_value=(1.0805, "HTF Demand OB [1.0795 - 1.0805]")
        )

        setups = self.detector._detect_swap_zones(
            symbol="EURUSD",
            df_ltf=df,
            swings_ltf=pd.DataFrame(index=df.index),
            current_candle=df.iloc[-1],
            current_time=df.iloc[-1]["time"],
            digits=4,
            point_scale=0.0001,
            buffer=0.0005,
            spread=0.0001,
            htf_trend="BEARISH",
            session_info={"session_name": "London", "is_killzone": True},
            news_warn=None,
            df_h1=df,
        )

        self.assertEqual(len(setups), 1)
        setup = setups[0]
        self.assertEqual(setup.tp1_price, 1.0805)  # Capped at barrier


if __name__ == "__main__":
    unittest.main()
