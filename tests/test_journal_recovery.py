import os
import shutil
import sqlite3
import tempfile
import unittest

from trade_journal import TradeJournal


class TestTradeJournalRecovery(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_trades.db")
        self.journal = TradeJournal(self.db_path)

    def tearDown(self):
        self.journal.checkpoint()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_healthy_database_operations(self):
        tid = self.journal.add_trade(
            symbol="EURUSD",
            direction="BUY",
            entry_price=1.0850,
            sl_price=1.0800,
            tp_price=1.0950,
            mt5_ticket=12345,
        )
        self.assertGreater(tid, 0)

        open_trades = self.journal.get_open_trades()
        self.assertEqual(len(open_trades), 1)
        self.assertEqual(open_trades[0]["symbol"], "EURUSD")
        self.assertEqual(open_trades[0]["mt5_ticket"], 12345)

        # Close trade
        ok = self.journal.close_trade_by_ticket(12345, realized_r=2.0, status="WIN")
        self.assertTrue(ok)

        open_trades_after = self.journal.get_open_trades()
        self.assertEqual(len(open_trades_after), 0)

        closed = self.journal.get_closed_trades()
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["status"], "WIN")

    def test_check_and_repair_on_healthy_db(self):
        self.journal.add_trade(
            symbol="GBPUSD",
            direction="SELL",
            entry_price=1.2700,
            sl_price=1.2750,
            tp_price=1.2600,
            mt5_ticket=67890,
        )
        # Should return True and keep the trade
        result = self.journal.check_and_repair()
        self.assertTrue(result)
        trades = self.journal.get_all_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["symbol"], "GBPUSD")

    def test_auto_recovery_from_corrupted_file(self):
        # 1. Add some trades
        self.journal.add_trade(
            symbol="USDJPY",
            direction="BUY",
            entry_price=150.00,
            sl_price=149.50,
            tp_price=151.00,
            mt5_ticket=11111,
        )
        self.journal.checkpoint()

        # 2. Corrupt the database file by writing garbage into it
        with open(self.db_path, "r+b") as f:
            f.seek(100)  # past header into page data
            f.write(b"CORRUPTED_GARBAGE_DATA_1234567890" * 20)
            f.flush()

        # 3. Create a new TradeJournal instance pointing to the corrupted file
        # It must self-heal via check_and_repair() in __init__
        new_journal = TradeJournal(self.db_path)
        self.assertTrue(os.path.exists(self.db_path))

        # 4. It should be fully usable and not raise DatabaseError
        open_trades = new_journal.get_open_trades()
        self.assertIsInstance(open_trades, list)

        # 5. Adding new trade should work flawlessly
        new_tid = new_journal.add_trade(
            symbol="AUDUSD",
            direction="SELL",
            entry_price=0.6500,
            sl_price=0.6550,
            tp_price=0.6400,
            mt5_ticket=22222,
        )
        self.assertGreater(new_tid, 0)
        self.assertEqual(len(new_journal.get_open_trades()), 1)

        # 6. Corrupt backup file should have been created
        corrupt_files = [f for f in os.listdir(self.temp_dir) if "corrupt_" in f]
        self.assertGreater(len(corrupt_files), 0)

    def test_runtime_decorator_recovery(self):
        # 1. Add trade
        self.journal.add_trade(
            symbol="NZDUSD",
            direction="BUY",
            entry_price=0.6000,
            sl_price=0.5950,
            tp_price=0.6100,
            mt5_ticket=33333,
        )
        self.journal.checkpoint()

        # 2. Corrupt the database file while the instance is alive
        with open(self.db_path, "r+b") as f:
            f.seek(100)
            f.write(b"EXTREME_CORRUPTION_DATA" * 50)
            f.flush()

        # 3. Calling get_open_trades() triggers @_auto_recover_on_corruption
        # and should self-heal without raising an unhandled DatabaseError
        trades = self.journal.get_open_trades()
        self.assertIsInstance(trades, list)

        # 4. Check stats works cleanly
        stats = self.journal.get_summary_stats()
        self.assertIn("winrate", stats)


if __name__ == "__main__":
    unittest.main()
