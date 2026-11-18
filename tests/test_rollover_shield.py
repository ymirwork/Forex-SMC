import unittest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timezone
import tempfile
import os

from scanner import is_in_rollover_window, SMCScanner
from trade_journal import TradeJournal


class TestRolloverWindowDetection(unittest.TestCase):
    def test_rollover_window_boundaries(self):
        # Window: 23:40 to 00:30 (20 min before, 30 min after)
        dt_before = datetime(2026, 11, 18, 23, 39, tzinfo=timezone.utc)
        dt_start = datetime(2026, 11, 18, 23, 40, tzinfo=timezone.utc)
        dt_midnight = datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc)
        dt_inside = datetime(2026, 11, 19, 0, 29, tzinfo=timezone.utc)
        dt_end = datetime(2026, 11, 19, 0, 30, tzinfo=timezone.utc)
        dt_after = datetime(2026, 11, 19, 0, 31, tzinfo=timezone.utc)
        dt_daytime = datetime(2026, 11, 18, 14, 30, tzinfo=timezone.utc)

        self.assertFalse(is_in_rollover_window(dt_before, 20, 30))
        self.assertTrue(is_in_rollover_window(dt_start, 20, 30))
        self.assertTrue(is_in_rollover_window(dt_midnight, 20, 30))
        self.assertTrue(is_in_rollover_window(dt_inside, 20, 30))
        self.assertFalse(is_in_rollover_window(dt_end, 20, 30))
        self.assertFalse(is_in_rollover_window(dt_after, 20, 30))
        self.assertFalse(is_in_rollover_window(dt_daytime, 20, 30))


class TestRolloverJournalPersistence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_shield.db")
        self.journal = TradeJournal(self.db_path)

    def tearDown(self):
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
        except Exception:
            pass

    def test_mark_and_unmark_shielded(self):
        # Insert a sample trade
        self.journal.add_trade(
            symbol="EURUSD",
            direction="BUY",
            entry_price=1.1000,
            sl_price=1.0980,
            tp_price=1.1050,
            confluence_score=80,
            mt5_ticket=12345,
            status="OPEN",
        )

        # Mark as shielded
        marked = self.journal.mark_trade_rollover_shielded(12345, original_sl=1.0980)
        self.assertTrue(marked)

        shielded = self.journal.get_rollover_shielded_trades()
        self.assertEqual(len(shielded), 1)
        self.assertEqual(shielded[0]["mt5_ticket"], 12345)
        self.assertAlmostEqual(shielded[0]["shielded_original_sl"], 1.0980)
        self.assertEqual(shielded[0]["is_rollover_shielded"], 1)

        # Unmark
        unmarked = self.journal.unmark_trade_rollover_shielded(12345)
        self.assertTrue(unmarked)

        shielded_after = self.journal.get_rollover_shielded_trades()
        self.assertEqual(len(shielded_after), 0)


class TestRolloverSLShieldAsync(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_mt5_client = MagicMock()
        self.mock_telegram = AsyncMock()
        self.mock_journal = MagicMock()
        self.mock_journal.get_rollover_shielded_trades.return_value = []

        self.scanner = SMCScanner(
            mt5_client=self.mock_mt5_client,
            telegram_manager=self.mock_telegram,
            journal=self.mock_journal,
        )

    @patch("scanner.ENABLE_ROLLOVER_SL_SHIELD", True)
    @patch("scanner.is_in_rollover_window", return_value=True)
    async def test_shield_activation(self, mock_window):
        # Setup open position needing shield (BUY at 1.1000, SL at 1.0980)
        open_positions = [{
            "ticket": 555,
            "symbol": "EURUSD",
            "type": "BUY",
            "price_open": 1.1000,
            "sl": 1.0980,
            "tp": 1.1050,
            "price_current": 1.1010,
            "profit": 10.0,
        }]

        self.mock_mt5_client.get_initial_sl.return_value = 1.0980
        self.mock_mt5_client.get_pip_size.return_value = 0.0001
        self.mock_mt5_client.is_position_at_be.return_value = False
        self.mock_mt5_client.modify_position_sl.return_value = (True, "SL updated")

        sym_mock = MagicMock()
        sym_mock.digits = 5
        with patch("MetaTrader5.symbol_info", return_value=sym_mock):
            await self.scanner._handle_rollover_sl_shield(open_positions)

        # Disaster SL at 2.5R from 1.1000 (risk = 0.0020 * 2.5 = 0.0050 -> 1.0950)
        self.mock_mt5_client.modify_position_sl.assert_called_once_with(555, 1.0950)
        self.mock_journal.mark_trade_rollover_shielded.assert_called_once_with(555, 1.0980)
        self.assertIn(555, self.scanner._shielded_trades)
        self.mock_telegram.send_message.assert_called_once()

    @patch("scanner.ENABLE_ROLLOVER_SL_SHIELD", True)
    @patch("scanner.is_in_rollover_window", return_value=True)
    async def test_mid_price_emergency_close(self, mock_window):
        # Position is already shielded: original SL is 1.0980
        self.scanner._shielded_trades[777] = 1.0980

        open_positions = [{
            "ticket": 777,
            "symbol": "EURUSD",
            "type": "BUY",
            "price_open": 1.1000,
            "sl": 1.0950, # Disaster SL in MT5
            "tp": 1.1050,
            "price_current": 1.0975,
            "profit": -25.0,
        }]

        # Tick shows MID = (1.0978 + 1.0980) / 2 = 1.0979 <= 1.0980 (Original SL crossed!)
        tick_mock = MagicMock()
        tick_mock.bid = 1.0978
        tick_mock.ask = 1.0980
        self.mock_mt5_client.close_market_position.return_value = (True, "Position closed")

        with patch("MetaTrader5.symbol_info_tick", return_value=tick_mock):
            await self.scanner._handle_rollover_sl_shield(open_positions)

        self.mock_mt5_client.close_market_position.assert_called_once_with(
            777,
            comment="Rollover Shield: Mid hit SL"
        )
        self.mock_journal.unmark_trade_rollover_shielded.assert_called_once_with(777)
        self.assertNotIn(777, self.scanner._shielded_trades)

    @patch("scanner.ENABLE_ROLLOVER_SL_SHIELD", True)
    @patch("scanner.is_in_rollover_window", return_value=False)
    async def test_shield_restoration_after_spread_normalizes(self, mock_window):
        # Position is shielded, but rollover window has ended
        self.scanner._shielded_trades[888] = 1.0980

        open_positions = [{
            "ticket": 888,
            "symbol": "EURUSD",
            "type": "BUY",
            "price_open": 1.1000,
            "sl": 1.0950,
            "tp": 1.1050,
            "price_current": 1.1015,
            "profit": 15.0,
        }]

        # Spread normalized to 1.2 pips (threshold is 3.0 pips)
        self.mock_mt5_client.get_symbol_spread.return_value = 1.2
        self.mock_mt5_client.modify_position_sl.return_value = (True, "SL restored")

        await self.scanner._handle_rollover_sl_shield(open_positions)

        self.mock_mt5_client.modify_position_sl.assert_called_once_with(888, 1.0980)
        self.mock_journal.unmark_trade_rollover_shielded.assert_called_once_with(888)
        self.assertNotIn(888, self.scanner._shielded_trades)
        self.mock_telegram.send_message.assert_called_once()

    @patch("scanner.ENABLE_ROLLOVER_SL_SHIELD", True)
    @patch("scanner.is_in_rollover_window", return_value=False)
    @patch("scanner.ROLLOVER_MAX_SPREAD_GOLD_PIPS", 25.0)
    async def test_gold_spread_restoration_threshold(self, mock_window):
        # Position on Gold (XAUUSD)
        self.scanner._shielded_trades[999] = 2650.0

        open_positions = [{
            "ticket": 999,
            "symbol": "XAUUSD",
            "type": "BUY",
            "price_open": 2660.0,
            "sl": 2635.0,
            "tp": 2700.0,
            "price_current": 2665.0,
            "profit": 50.0,
        }]

        # Spread is 22 pips ($2.20) <= 25.0 pips allowed for Gold
        self.mock_mt5_client.get_symbol_spread.return_value = 22.0
        self.mock_mt5_client.modify_position_sl.return_value = (True, "SL restored")

        await self.scanner._handle_rollover_sl_shield(open_positions)

        self.mock_mt5_client.modify_position_sl.assert_called_once_with(999, 2650.0)
        self.assertNotIn(999, self.scanner._shielded_trades)

    @patch("scanner.ENABLE_ROLLOVER_SL_SHIELD", True)
    @patch("scanner.is_in_rollover_window", return_value=True)
    async def test_tick_caching_for_multiple_positions_same_symbol(self, mock_window):
        # Two positions on the same symbol EURUSD
        self.scanner._shielded_trades[101] = 1.0980
        self.scanner._shielded_trades[102] = 1.0985

        open_positions = [
            {"ticket": 101, "symbol": "EURUSD", "type": "BUY", "price_open": 1.1000, "sl": 1.0950, "price_current": 1.1005, "profit": 5.0},
            {"ticket": 102, "symbol": "EURUSD", "type": "BUY", "price_open": 1.1000, "sl": 1.0950, "price_current": 1.1005, "profit": 5.0},
        ]

        tick_mock = MagicMock()
        tick_mock.bid = 1.1004
        tick_mock.ask = 1.1006

        with patch("MetaTrader5.symbol_info_tick", return_value=tick_mock) as mock_tick:
            await self.scanner._handle_rollover_sl_shield(open_positions)
            # symbol_info_tick should be called only ONCE for EURUSD despite 2 positions
            mock_tick.assert_called_once_with("EURUSD")
