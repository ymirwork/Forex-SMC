import asyncio
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from config import clean_symbol
from mt5_client import MT5Client
from trade_journal import TradeJournal
from smc_detector import TradeSetup


class TestPyramidCluster(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_cluster.db")
        self.journal = TradeJournal(self.db_path)
        self.client = MT5Client()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_clean_symbol(self):
        self.assertEqual(clean_symbol("EURUSD.pro"), "EURUSD")
        self.assertEqual(clean_symbol("XAUUSD.raw"), "XAUUSD")
        self.assertEqual(clean_symbol("GBPUSD_PRO"), "GBPUSD")
        self.assertEqual(clean_symbol("EURUSDm"), "EURUSD")
        self.assertEqual(clean_symbol("USDCAD.stp"), "USDCAD")
        self.assertEqual(clean_symbol("USDJPY"), "USDJPY")

    def test_database_pyramid_level_and_pending_transition(self):
        # 1. Add pending limit order for pyramid L2
        trade_id = self.journal.add_trade(
            symbol="EURUSD",
            direction="BUY",
            entry_price=1.08500,
            sl_price=1.08300,
            tp_price=1.09000,
            status="PENDING",
            mt5_ticket=200001,
            parent_ticket=100001,
            is_pyramid=1,
            pyramid_level=2,
            strategy="SMC",
            setup_type="[DOKŁADKA TRENDOWA L2] Fibo OTE",
        )
        self.assertGreater(trade_id, 0)

        # 2. Verify pending retrieval
        pending = self.journal.get_pending_trades()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["mt5_ticket"], 200001)
        self.assertEqual(pending[0]["pyramid_level"], 2)
        self.assertEqual(pending[0]["status"], "PENDING")

        # 3. Simulate limit order filled in MT5
        ok = self.journal.mark_trade_as_open(mt5_ticket=200001, fill_price=1.08495)
        self.assertTrue(ok)

        # 4. Verify transition to OPEN
        open_trades = self.journal.get_open_trades()
        self.assertEqual(len(open_trades), 1)
        self.assertEqual(open_trades[0]["mt5_ticket"], 200001)
        self.assertEqual(open_trades[0]["status"], "OPEN")
        self.assertEqual(open_trades[0]["entry_price"], 1.08495)

        # 5. Retrieve cluster trades
        cluster = self.journal.get_pyramid_cluster_trades("EURUSD", parent_ticket=100001)
        self.assertEqual(len(cluster), 1)
        self.assertEqual(cluster[0]["parent_ticket"], 100001)

    @patch("MetaTrader5.orders_get")
    @patch.object(MT5Client, "ensure_connection", return_value=True)
    def test_pyramid_cluster_eligibility_cascade(self, mock_conn, mock_orders):
        mock_orders.return_value = []

        # Case 1: No open positions -> not eligible
        with patch.object(self.client, "get_open_positions", return_value=[]):
            res = self.client.get_pyramid_cluster_status("EURUSD")
            self.assertFalse(res["is_eligible"])
            self.assertEqual(res["count"], 0)

        # Case 2: Base trade at +0.5R (less than PYRAMID_MIN_PROFIT_R=1.0)
        pos_l1 = {
            "ticket": 101,
            "symbol": "EURUSD",
            "type": "BUY",
            "price_open": 1.08000,
            "price_current": 1.08100,  # +10 pips
            "sl": 1.07800,             # risk = 20 pips -> +0.5R
            "tp": 1.09000,
            "profit": 50.0,
            "volume": 0.5,
            "time": 1000,
        }
        with patch.object(self.client, "get_open_positions", return_value=[pos_l1]), \
             patch.object(self.client, "get_initial_sl", return_value=1.07800):
            res = self.client.get_pyramid_cluster_status("EURUSD")
            self.assertFalse(res["is_eligible"])
            self.assertIn("wymagane", res.get("reason", ""))

        # Case 3: Base trade at +1.5R, SL at Break-Even -> Eligible for L2!
        pos_l1_be = {
            "ticket": 101,
            "symbol": "EURUSD",
            "type": "BUY",
            "price_open": 1.08000,
            "price_current": 1.08300,  # +30 pips -> +1.5R
            "sl": 1.08010,             # at BE + 1 pip
            "tp": 1.09000,
            "profit": 150.0,
            "volume": 0.5,
            "time": 1000,
        }
        with patch.object(self.client, "get_open_positions", return_value=[pos_l1_be]), \
             patch.object(self.client, "get_initial_sl", return_value=1.07800):
            res = self.client.get_pyramid_cluster_status("EURUSD")
            self.assertTrue(res["is_eligible"])
            self.assertEqual(res["next_level"], 2)
            self.assertEqual(res["base_ticket"], 101)
            self.assertEqual(res["master_tp"], 1.09000)

        # Case 4: Eligible base trade, BUT pending limit order exists -> blocked!
        mock_pending = MagicMock()
        mock_pending.ticket = 202
        mock_pending.symbol = "EURUSD"
        mock_orders.return_value = [mock_pending]

        with patch.object(self.client, "get_open_positions", return_value=[pos_l1_be]), \
             patch.object(self.client, "get_initial_sl", return_value=1.07800):
            res = self.client.get_pyramid_cluster_status("EURUSD")
            self.assertFalse(res["is_eligible"])
            self.assertTrue(res.get("has_pending", False))
            self.assertEqual(res["pending_ticket"], 202)

        mock_orders.return_value = []

        # Case 5: L1 at +2.5R (BE), L2 at +1.2R (BE) -> Eligible for L3!
        pos_l2_be = {
            "ticket": 102,
            "symbol": "EURUSD",
            "type": "BUY",
            "price_open": 1.08200,
            "price_current": 1.08450,
            "sl": 1.08210,             # at BE
            "tp": 1.09000,
            "profit": 125.0,
            "volume": 0.25,
            "time": 2000,
        }
        with patch.object(self.client, "get_open_positions", return_value=[pos_l1_be, pos_l2_be]), \
             patch.object(self.client, "get_initial_sl", side_effect=[1.07800, 1.08000]):
            res = self.client.get_pyramid_cluster_status("EURUSD")
            self.assertTrue(res["is_eligible"])
            self.assertEqual(res["next_level"], 3)
            self.assertEqual(len(res["cluster_tickets"]), 2)

    @patch("MetaTrader5.symbol_info")
    @patch("MetaTrader5.account_info")
    @patch.object(MT5Client, "ensure_connection", return_value=True)
    def test_risk_neutral_lot_sizing(self, mock_conn, mock_acc, mock_sym):
        # Broker symbol mock
        sym_mock = MagicMock()
        sym_mock.digits = 5
        sym_mock.point = 0.00001
        sym_mock.trade_tick_value = 1.0
        sym_mock.trade_tick_size = 0.00001
        sym_mock.volume_min = 0.01
        sym_mock.volume_max = 100.0
        sym_mock.volume_step = 0.01
        mock_sym.return_value = sym_mock

        # Account mock: $10,000 balance
        acc_mock = MagicMock()
        acc_mock.balance = 10000.0
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
                    "volume": 1.0,
                    "sl": 1.08010,
                }
            ],
        }

        # Sub-test A: new_sl is BELOW base entry (1.07900 <= 1.08000)
        # Should be REJECTED because it does not protect base entry!
        lot, locked, allowed, reason = self.client.calculate_pyramid_position_size(
            symbol="EURUSD",
            entry_price=1.08200,
            sl_price=1.07900,
            setup_grade="A+",
            cluster_status=cluster_status,
        )
        self.assertEqual(lot, 0.0)
        self.assertIn("nie chroni", reason)

        # Sub-test B: new_sl is ABOVE base entry (1.08150 > 1.08000)
        # Trailing L1 to 1.08150 locks in 15 pips * 1.0 lot * $10/pip = $150
        # Max risk allowed = $150 * 0.75 = $112.50
        # Standard risk = $10,000 * 0.5% = $50. Decay L2 (0.5x) * Grade A+ (1.0x) = $25
        # Allowed risk = min($112.50, $25) = $25.00
        # Entry = 1.08350, SL = 1.08150 (dist = 20 pips = $200/lot)
        # Lot = $25 / $200 = 0.125 -> rounded to 0.13 or 0.12 lot
        lot, locked, allowed, reason = self.client.calculate_pyramid_position_size(
            symbol="EURUSD",
            entry_price=1.08350,
            sl_price=1.08150,
            setup_grade="A+",
            cluster_status=cluster_status,
        )
        self.assertEqual(reason, "OK")
        self.assertGreater(locked, 100.0)
        self.assertGreater(lot, 0.0)
        self.assertLessEqual(lot, 1.0)

        # Sub-test C: Grade B should receive half the risk of Grade A+
        lot_b, _, allowed_b, _ = self.client.calculate_pyramid_position_size(
            symbol="EURUSD",
            entry_price=1.08350,
            sl_price=1.08150,
            setup_grade="B",
            cluster_status=cluster_status,
        )
        self.assertAlmostEqual(allowed_b, allowed * 0.5, delta=1.0)
        self.assertLess(lot_b, lot)

    @patch("MetaTrader5.order_send")
    @patch("MetaTrader5.symbol_info")
    @patch.object(MT5Client, "ensure_connection", return_value=True)
    def test_cluster_stop_loss_synchronization(self, mock_conn, mock_sym, mock_order):
        sym_mock = MagicMock()
        sym_mock.digits = 5
        sym_mock.point = 0.00001
        sym_mock.trade_stops_level = 5
        mock_sym.return_value = sym_mock

        order_res = MagicMock()
        order_res.retcode = 10009  # TRADE_RETCODE_DONE
        mock_order.return_value = order_res

        pos_l1 = {
            "ticket": 101,
            "symbol": "EURUSD",
            "type": "BUY",
            "price_open": 1.08000,
            "price_current": 1.08500,
            "sl": 1.08010,  # Current SL is BE
            "tp": 1.09000,
        }

        # Case 1: Target SL is 1.08200 (improves from 1.08010)
        with patch.object(self.client, "get_open_positions", return_value=[pos_l1]):
            results = self.client.sync_cluster_stop_loss("EURUSD", new_sl=1.08200)
            self.assertEqual(len(results), 1)
            ticket, ok, msg = results[0]
            self.assertEqual(ticket, 101)
            self.assertTrue(ok)
            mock_order.assert_called_once()

        mock_order.reset_mock()

        # Case 2: Target SL is 1.07900 (worse than current SL 1.08010) -> Should NOT update
        with patch.object(self.client, "get_open_positions", return_value=[pos_l1]):
            results = self.client.sync_cluster_stop_loss("EURUSD", new_sl=1.07900)
            self.assertEqual(len(results), 1)
            _, ok, msg = results[0]
            self.assertTrue(ok)
            self.assertIn("lepszym poziomie", msg)
            mock_order.assert_not_called()

        # Case 3: Target SL is too close to market price (< stops_level + 2)
        pos_near_market = {
            "ticket": 101,
            "symbol": "EURUSD",
            "type": "BUY",
            "price_open": 1.08000,
            "price_current": 1.08203,
            "sl": 1.08010,
            "tp": 1.09000,
        }
        with patch.object(self.client, "get_open_positions", return_value=[pos_near_market]):
            results = self.client.sync_cluster_stop_loss("EURUSD", new_sl=1.08200)
            _, ok, msg = results[0]
            self.assertFalse(ok)
            self.assertIn("Zbyt blisko", msg)
            mock_order.assert_not_called()

    @patch("MetaTrader5.symbol_info_tick", return_value=None)
    def test_session_manager_broker_offset(self, mock_tick):
        from session_manager import get_broker_utc_offset, SessionManager
        offset = get_broker_utc_offset()
        self.assertIsInstance(offset, int)
        self.assertTrue(-12 <= offset <= 14)

        session = SessionManager.get_current_session()
        self.assertIn("session_name", session)
        self.assertIn("is_killzone", session)
        self.assertIn("time_utc", session)

    @patch("MetaTrader5.positions_get")
    def test_scanner_pending_fill_and_sync(self, mock_positions):
        # 1. Add pending pyramid scale-in to journal
        self.journal.add_trade(
            symbol="EURUSD",
            direction="BUY",
            entry_price=1.08400,
            sl_price=1.08200,
            tp_price=1.09000,
            status="PENDING",
            mt5_ticket=300001,
            parent_ticket=100001,
            is_pyramid=1,
            pyramid_level=2,
        )

        from scanner import SMCScanner
        scanner = SMCScanner(
            mt5_client=self.client,
            journal=self.journal,
        )

        # Mock MT5 positions showing ticket 300001 is now an OPEN position (filled at 1.08405)
        mock_pos = {
            "ticket": 300001,
            "symbol": "EURUSD",
            "type": "BUY",
            "price_open": 1.08405,
            "sl": 1.08200,
            "tp": 1.09000,
            "price_current": 1.08450,
            "profit": 15.0,
            "volume": 0.2,
            "time": 1000,
        }
        mock_positions.return_value = [mock_pos]

        with patch.object(self.client, "sync_cluster_stop_loss", return_value=[(100001, True, "OK")]) as mock_sync, \
             patch.object(self.client, "get_open_positions", return_value=[mock_pos]), \
             patch.object(self.client, "get_closed_deal_info", return_value=None), \
             patch.object(self.client, "ensure_connection", return_value=True), \
             patch.object(scanner.smc_detector, "get_latest_m15_swing", return_value=(0.0, 0.0, 0.0001)), \
             patch.object(self.client, "get_position_mfe", return_value=0.5):
            asyncio.run(scanner.monitor_open_trades())
            # Verify trade transitioned to OPEN
            trade = self.journal.get_trade_by_ticket(300001)
            self.assertEqual(trade["status"], "OPEN")
            self.assertEqual(trade["entry_price"], 1.08405)
            # Verify cluster Stop Loss synchronization was triggered
            mock_sync.assert_called_once_with(symbol="EURUSD", new_sl=1.08200)


if __name__ == "__main__":
    unittest.main()
