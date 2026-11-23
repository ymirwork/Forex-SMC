import unittest
from unittest.mock import MagicMock, patch
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
        self.assertEqual(MT5Client._parse_currencies("EURUSD.stp"), ("EUR", "USD"))
        self.assertEqual(MT5Client._parse_currencies("EURUSD.c"), ("EUR", "USD"))

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

    @patch("mt5_client.mt5")
    def test_symbol_info_stops_level_attribute_safety(self, mock_mt5):
        # Position mock
        mock_pos = MagicMock()
        mock_pos.ticket = 12345
        mock_pos.symbol = "EURUSD"
        mock_pos.type = 0  # BUY
        mock_pos.price_open = 1.0800
        mock_pos.price_current = 1.0830
        mock_pos.tp = 1.0900
        mock_mt5.positions_get.return_value = [mock_pos]

        # SymbolInfo with modern MT5 trade_stops_level
        mock_sym = MagicMock()
        mock_sym.digits = 5
        mock_sym.point = 0.00001
        mock_sym.trade_stops_level = 10
        mock_sym.stops_level = 0
        mock_mt5.symbol_info.return_value = mock_sym

        mock_send_res = MagicMock()
        mock_send_res.retcode = 10009  # TRADE_RETCODE_DONE
        mock_mt5.order_send.return_value = mock_send_res
        mock_mt5.TRADE_RETCODE_DONE = 10009
        mock_mt5.ORDER_TYPE_BUY = 0

        client = MT5Client()
        client.is_connected = True
        client.get_initial_sl = MagicMock(return_value=1.0700)

        ok, msg = client.move_sl_to_breakeven(12345, offset_pips=1.0)
        self.assertTrue(ok)
        self.assertIn("przestawiony na Break-Even", msg)

        # Verify call to order_send with trade_stops_level safety
        req = mock_mt5.order_send.call_args[0][0]
        self.assertEqual(req["symbol"], "EURUSD")
        self.assertGreaterEqual(req["sl"], 1.0800)

        # Fallback when trade_stops_level attribute does not exist (legacy)
        del mock_sym.trade_stops_level
        mock_sym.stops_level = 15
        ok2, msg2 = client.move_sl_to_breakeven(12345, offset_pips=1.0)
        self.assertTrue(ok2)



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


class TestStopLossCalculation(unittest.TestCase):
    def test_stop_loss_modes(self):
        import smc_detector
        from smc_detector import calculate_stop_loss
        
        orig_mode = smc_detector.SL_CALCULATION_MODE
        orig_mult = smc_detector.SL_ATR_MULTIPLIER
        orig_hybrid = smc_detector.SL_HYBRID_MIN_ATR_MULT
        try:
            smc_detector.SL_ATR_MULTIPLIER = 1.5
            smc_detector.SL_HYBRID_MIN_ATR_MULT = 0.8
            entry = 1.1000
            atr = 0.0010  # 10 pips
            spread = 0.0001  # 1 pip

            # 1. STRUCTURE MODE
            smc_detector.SL_CALCULATION_MODE = "STRUCTURE"
            sl, risk, mode = calculate_stop_loss(entry=entry, structural_sl=1.0980, atr=atr, is_buy=True, spread_buffer=spread)
            self.assertEqual(mode, "STRUCTURE")
            self.assertAlmostEqual(sl, 1.0980)
            self.assertAlmostEqual(risk, 0.0020)

            # 2. ATR MODE (BUY): entry - (1.5 * 0.0010 + 0.0001) = 1.1000 - 0.0016 = 1.0984
            smc_detector.SL_CALCULATION_MODE = "ATR"
            sl, risk, mode = calculate_stop_loss(entry=entry, structural_sl=1.0980, atr=atr, is_buy=True, spread_buffer=spread)
            self.assertEqual(mode, "ATR")
            self.assertAlmostEqual(sl, 1.0984)
            self.assertAlmostEqual(risk, 0.0016)

            # 3. HYBRID MODE (BUY - Structural is wider than min ATR):
            # min ATR risk = (0.8 * 0.0010) + 0.0001 = 0.0009. Structural risk = 0.0020.
            # max(0.0020, 0.0009) = 0.0020. sl = 1.0980
            smc_detector.SL_CALCULATION_MODE = "HYBRID"
            sl, risk, mode = calculate_stop_loss(entry=entry, structural_sl=1.0980, atr=atr, is_buy=True, spread_buffer=spread)
            self.assertEqual(mode, "HYBRID")
            self.assertAlmostEqual(sl, 1.0980)
            self.assertAlmostEqual(risk, 0.0020)

            # 4. HYBRID MODE (BUY - Structural is tighter than min ATR):
            # structural_sl = 1.0995 (5 pips risk). min ATR risk = 9 pips (0.0009).
            # max(0.0005, 0.0009) = 0.0009. sl = 1.1000 - 0.0009 = 1.0991
            sl, risk, mode = calculate_stop_loss(entry=entry, structural_sl=1.0995, atr=atr, is_buy=True, spread_buffer=spread)
            self.assertEqual(mode, "HYBRID")
            self.assertAlmostEqual(sl, 1.0991)
            self.assertAlmostEqual(risk, 0.0009)

            # 5. HYBRID MODE (SELL - Structural is tighter than min ATR):
            # entry = 1.1000. structural_sl = 1.1005 (5 pips).
            # sl = 1.1000 + 0.0009 = 1.1009
            sl, risk, mode = calculate_stop_loss(entry=entry, structural_sl=1.1005, atr=atr, is_buy=False, spread_buffer=spread)
            self.assertEqual(mode, "HYBRID")
            self.assertAlmostEqual(sl, 1.1009)
            self.assertAlmostEqual(risk, 0.0009)

            # 6. Invalid risk (too tight <= 5 points)
            sl, risk, mode = calculate_stop_loss(entry=entry, structural_sl=1.09999, atr=0.00001, is_buy=True, spread_buffer=0.0)
            self.assertIsNone(sl)
            self.assertIsNone(risk)
        finally:
            smc_detector.SL_CALCULATION_MODE = orig_mode
            smc_detector.SL_ATR_MULTIPLIER = orig_mult
            smc_detector.SL_HYBRID_MIN_ATR_MULT = orig_hybrid


class TestAuthorizationSecurity(unittest.TestCase):
    def test_unauthorized_channel_member_callback_rejected(self):
        from unittest.mock import MagicMock
        from telegram_bot import TelegramBotManager

        # Trader configures their personal user ID (123456) and a broadcast channel (-100999999)
        bot = TelegramBotManager(None, token="dummy", alert_channel=-100999999, allowed_ids=[123456, -100999999])

        # Authorized user clicking button
        update_auth = MagicMock()
        update_auth.callback_query = MagicMock()
        update_auth.effective_user.id = 123456
        update_auth.effective_chat.id = -100999999
        update_auth.callback_query.message.chat.id = -100999999
        self.assertTrue(bot._is_authorized(update_auth))

        # Random member in the channel clicking button (user ID 999999 not in allowed_chat_ids)
        update_unauth = MagicMock()
        update_unauth.callback_query = MagicMock()
        update_unauth.effective_user.id = 999999
        update_unauth.effective_chat.id = -100999999
        update_unauth.callback_query.message.chat.id = -100999999
        self.assertFalse(bot._is_authorized(update_unauth))


class TestPropFirmRiskMin(unittest.TestCase):
    @patch("MetaTrader5.symbol_info")
    @patch("MetaTrader5.account_info")
    @patch.object(MT5Client, "ensure_connection", return_value=True)
    def test_edge_filter_reduced_risk_preserved_in_prop_mode(self, mock_conn, mock_acc, mock_sym):
        from config import PROP_RISK_PER_TRADE_PERCENT
        sym_mock = MagicMock()
        sym_mock.digits = 5
        sym_mock.point = 0.00001
        sym_mock.trade_tick_value = 1.0
        sym_mock.trade_tick_size = 0.00001
        sym_mock.volume_min = 0.01
        sym_mock.volume_max = 100.0
        sym_mock.volume_step = 0.01
        mock_sym.return_value = sym_mock

        acc_mock = MagicMock()
        acc_mock.balance = 10000.0
        acc_mock.currency = "USD"
        mock_acc.return_value = acc_mock

        client = MT5Client()
        lot_full = client.calculate_lot_size("EURUSD", PROP_RISK_PER_TRADE_PERCENT, 1.07800, 1.08000)
        lot_halved = client.calculate_lot_size("EURUSD", 0.25, 1.07800, 1.08000)
        self.assertLess(lot_halved, lot_full)
        self.assertAlmostEqual(lot_halved, lot_full * 0.5, delta=0.02)


class TestPyramidMinLotRiskProtection(unittest.TestCase):
    @patch("MetaTrader5.symbol_info")
    @patch("MetaTrader5.account_info")
    @patch.object(MT5Client, "ensure_connection", return_value=True)
    def test_pyramid_rejects_when_min_lot_exceeds_locked_risk(self, mock_conn, mock_acc, mock_sym):
        sym_mock = MagicMock()
        sym_mock.digits = 5
        sym_mock.point = 0.00001
        sym_mock.trade_tick_value = 1.0
        sym_mock.trade_tick_size = 0.00001
        sym_mock.volume_min = 0.01
        sym_mock.volume_max = 100.0
        sym_mock.volume_step = 0.01
        mock_sym.return_value = sym_mock

        acc_mock = MagicMock()
        acc_mock.balance = 1000.0
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
                    "volume": 0.01,
                    "sl": 1.08010,
                }
            ],
        }
        client = MT5Client()
        lot, locked, allowed, reason = client.calculate_pyramid_position_size(
            symbol="EURUSD",
            entry_price=1.08200,
            sl_price=1.08010,
            setup_grade="A+",
            cluster_status=cluster_status,
        )
        self.assertEqual(lot, 0.0)
        self.assertIn("przekraczając dozwolone ryzyko", reason)


class TestAsianRangeMondayFallback(unittest.TestCase):
    def test_monday_fallback_to_friday(self):
        fri_bars = pd.date_range("2026-11-13 00:00", "2026-11-13 06:45", freq="15min")
        mon_bars = pd.date_range("2026-11-16 08:00", "2026-11-16 09:30", freq="15min")
        times = list(fri_bars) + list(mon_bars)

        highs = [1.0950 if i == 5 else 1.0930 for i in range(len(times))]
        lows = [1.0910 if i == 10 else 1.0920 for i in range(len(times))]
        closes = [1.0925] * len(times)

        df = pd.DataFrame({"time": times, "high": highs, "low": lows, "close": closes})

        with patch("session_manager.get_broker_utc_offset", return_value=0):
            # Monday at 09:30 (today's Asian session candles are absent from df, falls back to Friday)
            asia_h, asia_l = SessionManager.calculate_asian_range(df)
            self.assertEqual(asia_h, 1.0950)
            self.assertEqual(asia_l, 1.0910)


class TestTelegramCallbacksAndOrderExecution(unittest.IsolatedAsyncioTestCase):
    async def test_callback_dismiss_mutes_symbol(self):
        from telegram_bot import TelegramBotManager
        from smc_detector import TradeSetup
        mt5_mock = MagicMock()
        journal_mock = MagicMock()
        bot = TelegramBotManager(mt5_mock, token="dummy", alert_channel=-100123, allowed_ids=[123456], journal=journal_mock)

        setup = MagicMock(spec=TradeSetup)
        setup.symbol = "EURUSD"
        setup.direction = "BUY"
        bot.pending_setups["setup_1"] = setup
        bot.pending_setups_time["setup_1"] = 1000

        update = MagicMock()
        update.effective_user.id = 123456
        update.effective_chat.id = -100123
        update.callback_query.data = "dismiss:setup_1"
        update.callback_query.message.caption = "Old caption"
        update.callback_query.answer = unittest.mock.AsyncMock()
        update.callback_query.edit_message_caption = unittest.mock.AsyncMock()

        await bot.handle_callback(update, MagicMock())

        self.assertTrue(bot.is_symbol_muted("EURUSD", "BUY"))
        self.assertNotIn("setup_1", bot.pending_setups)

    async def test_callback_limit_order_logs_as_pending(self):
        from telegram_bot import TelegramBotManager
        from smc_detector import TradeSetup
        mt5_mock = MagicMock()
        mt5_mock.execute_limit_order.return_value = (True, "OK", 777001, 1.0850)
        mt5_mock.calculate_lot_size.return_value = 0.1
        mt5_mock.can_open_new_trade.return_value = (True, "OK")

        journal_mock = MagicMock()
        bot = TelegramBotManager(mt5_mock, token="dummy", alert_channel=-100123, allowed_ids=[123456], journal=journal_mock)

        setup = MagicMock(spec=TradeSetup)
        setup.symbol = "EURUSD"
        setup.direction = "BUY"
        setup.entry_price = 1.0850
        setup.sl_price = 1.0820
        setup.tp1_price = 1.0910
        setup.confluence_score = 80
        setup.setup_type = "M15 CHoCH"
        setup.strategy_name = "SMC"
        setup.is_pyramid = False
        bot.pending_setups["setup_2"] = setup
        bot.pending_setups_time["setup_2"] = 1000

        update = MagicMock()
        update.effective_user.id = 123456
        update.effective_chat.id = -100123
        update.callback_query.data = "limit:setup_2"
        update.callback_query.message.caption = "Old caption"
        update.callback_query.answer = unittest.mock.AsyncMock()
        update.callback_query.edit_message_caption = unittest.mock.AsyncMock()

        with patch.object(bot.news_filter, "check_news_async", unittest.mock.AsyncMock(return_value=(False, None, None))):
            await bot.handle_callback(update, MagicMock())

        mt5_mock.execute_limit_order.assert_called_once()
        journal_mock.add_trade.assert_called_once()
        _, kwargs = journal_mock.add_trade.call_args
        self.assertEqual(kwargs.get("status"), "PENDING")
        self.assertEqual(kwargs.get("mt5_ticket"), 777001)


class TestScannerCandleCaching(unittest.TestCase):
    def test_fetch_cached_ohlcv_reuses_cache_within_ttl(self):
        from scanner import SMCScanner
        mt5_mock = MagicMock()
        dummy_df = pd.DataFrame({"close": [1.0800, 1.0850]})
        mt5_mock.fetch_ohlcv.return_value = dummy_df

        scanner = SMCScanner(mt5_client=mt5_mock)
        df1 = scanner._fetch_cached_ohlcv("EURUSD", "D1", 10, max_age_seconds=1800.0)
        df2 = scanner._fetch_cached_ohlcv("EURUSD", "D1", 10, max_age_seconds=1800.0)

        self.assertIs(df1, df2)
        mt5_mock.fetch_ohlcv.assert_called_once_with("EURUSD", "D1", 10)

    def test_fetch_cached_ohlcv_refetches_after_ttl(self):
        import time
        from scanner import SMCScanner
        mt5_mock = MagicMock()
        dummy_df1 = pd.DataFrame({"close": [1.0800]})
        dummy_df2 = pd.DataFrame({"close": [1.0850]})
        mt5_mock.fetch_ohlcv.side_effect = [dummy_df1, dummy_df2]

        scanner = SMCScanner(mt5_client=mt5_mock)
        df1 = scanner._fetch_cached_ohlcv("EURUSD", "D1", 10, max_age_seconds=0.01)
        time.sleep(0.02)
        df2 = scanner._fetch_cached_ohlcv("EURUSD", "D1", 10, max_age_seconds=0.01)

        self.assertEqual(mt5_mock.fetch_ohlcv.call_count, 2)
        self.assertIsNot(df1, df2)


class TestNotionTicketExactMatch(unittest.IsolatedAsyncioTestCase):
    async def test_find_page_id_exact_token_matching(self):
        from notion_journal import NotionJournal
        journal = NotionJournal()
        journal.api_key = "dummy"
        journal.database_id = "dummy_db"
        journal._db_properties = {"trade id": ("Name", "title")}

        mock_response_substring = MagicMock()
        mock_response_substring.status_code = 200
        mock_response_substring.json.return_value = {
            "results": [
                {
                    "id": "page_wrong",
                    "properties": {
                        "Name": {
                            "type": "title",
                            "title": [{"plain_text": "#105626168 EURUSD"}],
                        }
                    },
                }
            ]
        }

        with patch("httpx.AsyncClient.post", new_callable=unittest.mock.AsyncMock) as mock_post:
            mock_post.return_value = mock_response_substring
            res = await journal._find_page_id_by_ticket(105)
            self.assertIsNone(res)

        mock_response_exact = MagicMock()
        mock_response_exact.status_code = 200
        mock_response_exact.json.return_value = {
            "results": [
                {
                    "id": "page_correct",
                    "properties": {
                        "Name": {
                            "type": "title",
                            "title": [{"plain_text": "#105 EURUSD"}],
                        }
                    },
                }
            ]
        }

        with patch("httpx.AsyncClient.post", new_callable=unittest.mock.AsyncMock) as mock_post:
            mock_post.return_value = mock_response_exact
            res = await journal._find_page_id_by_ticket(105)
            self.assertEqual(res, "page_correct")


class TestSyncJournalDirectionFallback(unittest.IsolatedAsyncioTestCase):
    @patch("sync_journal.ENABLE_NOTION", False)
    @patch("sync_journal.mt5.history_deals_get")
    @patch("sync_journal.mt5.history_orders_get")
    @patch("sync_journal.mt5.positions_get", return_value=[])
    async def test_direction_falls_back_to_comment_when_entry_deal_missing(
        self, mock_pos, mock_orders, mock_deals
    ):
        from sync_journal import sync_all
        from trade_journal import TradeJournal

        mt5_mock = MagicMock()
        mt5_mock.connect.return_value = True
        mt5_mock.get_open_positions.return_value = []
        mt5_mock.get_position_mfe.return_value = 1.5

        exit_deal = MagicMock()
        exit_deal.entry = 1  # DEAL_ENTRY_OUT
        exit_deal.position_id = 888123
        exit_deal.ticket = 999123
        exit_deal.symbol = "EURUSD.pro"
        exit_deal.profit = 150.0
        exit_deal.price = 1.0800
        exit_deal.reason = 5
        exit_deal.comment = "TP hit [SELL]"
        exit_deal.time = 1700000000

        mock_deals.side_effect = [
            [exit_deal],  # for general history_deals_get(from_date, to_date)
            [exit_deal],  # for history_deals_get(position=ticket)
        ]
        mock_orders.return_value = []

        journal_mock = MagicMock(spec=TradeJournal)
        journal_mock.get_all_trades.return_value = []

        await sync_all(client=mt5_mock, journal=journal_mock)

        journal_mock.add_trade.assert_called_once()
        _, kwargs = journal_mock.add_trade.call_args
        self.assertEqual(kwargs.get("symbol"), "EURUSD")  # clean_symbol was applied!
        self.assertEqual(kwargs.get("direction"), "SELL")  # Fell back to comment!


class TestTelegramHtmlFormatting(unittest.TestCase):
    def test_md_to_telegram_html_basic(self):
        from telegram_bot import md_to_telegram_html

        sample = "🔔 **SMC SETUP: EURJPY**\n🎯 **Ocena:** `90%` 🔥 [Grade A+ Setup]"
        html_out = md_to_telegram_html(sample)
        self.assertIn("<b>SMC SETUP: EURJPY</b>", html_out)
        self.assertIn("<b>Ocena:</b> <code>90%</code>", html_out)
        self.assertNotIn("**", html_out)
        self.assertNotIn("`", html_out)
        self.assertIn("[Grade A+ Setup]", html_out)

    def test_md_to_telegram_html_special_chars_and_regimes(self):
        from telegram_bot import md_to_telegram_html

        sample = "🧠 **Reżim:** `[STRONG_TREND]` (Fibo >= 61.8% & < 78.6%)"
        html_out = md_to_telegram_html(sample)
        self.assertIn("<code>[STRONG_TREND]</code>", html_out)
        self.assertIn("&gt;=", html_out)
        self.assertIn("&lt;", html_out)
        self.assertIn("&amp;", html_out)
        self.assertNotIn("<i>", html_out)  # Underscore inside code shouldn't become italic

    def test_truncate_telegram_html(self):
        from telegram_bot import truncate_telegram_html

        long_html = "<b>Header</b>\n" + "\n".join([f"• Line {i}: <code>Val {i}</code> and <b>bold {i}</b>" for i in range(20)])
        trunc = truncate_telegram_html(long_html, max_rendered_len=150)
        # Should close all tags properly
        self.assertTrue(trunc.count("<b>") == trunc.count("</b>"))
        self.assertTrue(trunc.count("<code>") == trunc.count("</code>"))


    def test_md_to_telegram_html_links_with_underscores(self):
        from telegram_bot import md_to_telegram_html

        sample = "Check [API Docs](https://example.com/_api_/endpoint_v1) for details."
        html_out = md_to_telegram_html(sample)
        self.assertIn('<a href="https://example.com/_api_/endpoint_v1">API Docs</a>', html_out)
        self.assertNotIn("<i>", html_out)

    def test_safe_edit_message_truncation_and_remove_keyboard(self):
        import asyncio
        from telegram_bot import TelegramBotManager

        mock_query = MagicMock()
        mock_msg = MagicMock()
        mock_msg.caption = "Original setup caption"
        mock_query.message = mock_msg

        # Long text over 1200 characters
        long_text = "<b>Result:</b>\n" + ("• Detailed analysis line.\n" * 50)
        self.assertGreater(len(long_text), 1100)

        mock_query.edit_message_caption = unittest.mock.AsyncMock()

        asyncio.run(TelegramBotManager._safe_edit_message(mock_query, text=long_text))
        mock_query.edit_message_caption.assert_called_once()
        _, kwargs = mock_query.edit_message_caption.call_args
        self.assertLessEqual(len(kwargs["caption"]), 1024)
        self.assertIsNone(kwargs["reply_markup"])

    def test_group_callback_rejected_without_user_id(self):
        from telegram_bot import TelegramBotManager

        client_mock = MagicMock()
        mgr = TelegramBotManager(mt5_client=client_mock, allowed_ids=[-100123456789])

        update_mock = MagicMock()
        update_mock.callback_query = MagicMock()
        update_mock.effective_user.id = 888999  # User ID not in allowed positive IDs
        update_mock.effective_chat.id = -100123456789  # Group ID in allowed

        self.assertFalse(mgr._is_authorized(update_mock))


class TestAuditAndReviewFixes(unittest.TestCase):
    @patch("mt5_client.mt5")
    def test_lot_size_over_risk_rejection(self, mock_mt5):
        # Account: $600 balance, risk: 0.5% ($3.00), tolerance: 1.35x ($4.05 max tolerated)
        acc_mock = MagicMock()
        acc_mock.balance = 600.0
        acc_mock.currency = "USD"
        mock_mt5.account_info.return_value = acc_mock

        # Gold: min volume = 0.01 lot, tick size = 0.01, tick val = 1.0
        sym_mock = MagicMock()
        sym_mock.digits = 2
        sym_mock.trade_tick_size = 0.01
        sym_mock.trade_tick_value = 1.0
        sym_mock.trade_tick_value_loss = 1.0
        sym_mock.volume_min = 0.01
        sym_mock.volume_max = 100.0
        sym_mock.volume_step = 0.01
        mock_mt5.symbol_info.return_value = sym_mock

        client = MT5Client()
        client.is_connected = True
        client.ensure_connection = MagicMock(return_value=True)
        client.resolve_symbol = MagicMock(return_value="XAUUSD")

        # SL distance = $30.00 (300 pips on Gold). Loss at 0.01 lot = $30.00!
        # Target risk is $3.00, tolerated is $4.05. Loss ($30.00) > $4.05 -> MUST return 0.0 lot!
        lot = client.calculate_lot_size("XAUUSD", risk_percent=0.5, sl_price=2620.0, entry_price=2650.0)
        self.assertEqual(lot, 0.0)

    def test_gold_min_sl_distance_consistently_30_dollars(self):
        from smc_detector import get_min_sl_distance

        # Broker with 2 digits (point_scale = 0.01)
        dist_2digit = get_min_sl_distance("XAUUSD", point_scale=0.01)
        self.assertEqual(dist_2digit, 30.0)

        # Broker with 3 digits (point_scale = 0.001) - previously bugged to 0.30!
        dist_3digit = get_min_sl_distance("XAUUSD", point_scale=0.001)
        self.assertEqual(dist_3digit, 30.0)

    def test_parse_currencies_unified(self):
        from config import parse_currencies

        self.assertEqual(parse_currencies("EURUSD"), ("EUR", "USD"))
        self.assertEqual(parse_currencies("GBPUSD.pro"), ("GBP", "USD"))
        self.assertEqual(parse_currencies("XAUUSD"), ("XAU", "USD"))
        self.assertEqual(parse_currencies("XAUUSD.raw"), ("XAU", "USD"))
        self.assertEqual(parse_currencies("EURUSDm"), ("EUR", "USD"))


if __name__ == "__main__":
    unittest.main()
