"""
Unit tests for the AI Strategy & Trade Auditor module (ai_auditor.py)
and its Telegram bot integration.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from ai_auditor import (
    ALLOWED_CONFIG_KEYS,
    AIAuditAggregator,
    apply_recommendations,
    deterministic_fallback_report,
    extract_recommendations_json,
    filter_trades_by_range,
    format_audit_prompt,
    query_gemini_api,
    query_gemini_api_async,
    run_audit,
    save_audit_report,
)
from trade_journal import TradeJournal


class TestAIAuditor(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_auditor.db")
        self.journal = TradeJournal(self.db_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_filter_trades_by_range(self):
        now = datetime.now()
        t1 = {"id": 1, "timestamp": (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")}
        t2 = {"id": 2, "timestamp": (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")}
        t3 = {"id": 3, "timestamp": (now - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")}
        all_trades = [t1, t2, t3]

        # 'all'
        self.assertEqual(len(filter_trades_by_range(all_trades, "all")), 3)

        # '7d' (should only include t1)
        res_7d = filter_trades_by_range(all_trades, "7d")
        self.assertEqual(len(res_7d), 1)
        self.assertEqual(res_7d[0]["id"], 1)

        # '30d' (should include t1 and t2)
        res_30d = filter_trades_by_range(all_trades, "30d")
        self.assertEqual(len(res_30d), 2)

        # '2' (last 2 trades)
        res_count = filter_trades_by_range(all_trades, "2")
        self.assertEqual(len(res_count), 2)
        self.assertEqual(res_count[0]["id"], 2)
        self.assertEqual(res_count[1]["id"], 3)

    def test_audit_aggregator_metrics_and_diagnostics(self):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. Winning trade (+2.0R)
        self.journal.add_trade(
            symbol="EURUSD",
            direction="BUY",
            entry_price=1.1000,
            sl_price=1.0950,
            tp_price=1.1100,
            status="WIN",
            strategy="SMC",
            session_name="London",
            realized_r=2.0,
            max_r=2.2,
            mae_r=0.2,
            confluence_score=85,
            grade="A+",
            regime="STRONG_TREND",
        )

        # 2. Losing trade (-1.0R)
        self.journal.add_trade(
            symbol="GBPUSD",
            direction="SELL",
            entry_price=1.3000,
            sl_price=1.3050,
            tp_price=1.2900,
            status="LOSS",
            strategy="Zumiko KillMLM",
            session_name="NY AM",
            realized_r=-1.0,
            max_r=0.2,
            mae_r=1.0,
            confluence_score=70,
            grade="A",
            regime="COMPRESSION",
        )

        # 3. Premature BE trade (max_r was 1.5R, closed at 0.0R)
        self.journal.add_trade(
            symbol="XAUUSD",
            direction="BUY",
            entry_price=2600.0,
            sl_price=2590.0,
            tp_price=2630.0,
            status="BE",
            strategy="SMC",
            session_name="London",
            realized_r=0.0,
            max_r=1.6,
            mae_r=0.1,
            confluence_score=90,
            grade="A+",
            regime="STRONG_TREND",
        )

        aggregator = AIAuditAggregator(self.journal, range_spec="all")
        metrics = aggregator.collect_metrics()

        self.assertEqual(metrics["total_trades"], 3)
        self.assertEqual(metrics["wins"], 1)
        self.assertEqual(metrics["losses"], 1)
        self.assertEqual(metrics["bes"], 1)
        self.assertAlmostEqual(metrics["winrate"], 33.3, places=1)
        self.assertAlmostEqual(metrics["net_r"], 1.0, places=2)
        self.assertAlmostEqual(metrics["ev_per_trade"], 0.333, places=2)
        self.assertEqual(metrics["profit_factor"], 2.0)

        # Diagnostics: premature BE
        diag = metrics["execution_diagnostics"]
        self.assertEqual(diag["premature_be_count"], 1)
        self.assertEqual(len(diag["worst_losses"]), 1)
        self.assertEqual(diag["worst_losses"][0]["symbol"], "GBPUSD")
        self.assertEqual(len(diag["top_winners"]), 1)
        self.assertEqual(diag["top_winners"][0]["symbol"], "EURUSD")

    def test_format_audit_prompt(self):
        aggregator = AIAuditAggregator(self.journal, range_spec="30d")
        metrics = aggregator.collect_metrics()
        prompt = format_audit_prompt(metrics)

        self.assertIn("Senior Quant & SMC Risk Manager", prompt)
        self.assertIn("METRYKI OGÓLNE", prompt)
        self.assertIn("ROZBICIE WG SESJI", prompt)
        self.assertIn("DIAGNOSTYKA EGZEKUCJI", prompt)
        self.assertIn("TWOJE ZADANIE ANALITYCZNE", prompt)
        self.assertIn("```json", prompt)

    def test_deterministic_fallback_report(self):
        aggregator = AIAuditAggregator(self.journal, range_spec="30d")
        metrics = aggregator.collect_metrics()
        report = deterministic_fallback_report(metrics)

        self.assertIn("STATYSTYCZNY AUDYT STRATEGII (DETERMINISTIC FALLBACK)", report)
        self.assertIn("WYKRYTE WYCIEKI KAPITAŁU", report)
        self.assertIn("REKOMENDACJE", report)

    def test_extract_recommendations_json(self):
        sample_response = (
            "Oto analiza wyników egzekucji.\n\n"
            "### REKOMENDACJE:\n"
            "```json\n"
            "{\n"
            '  "summary": "Stabilne wyniki SMC, Zumiko wymaga optymalizacji.",\n'
            '  "capital_leaks": ["NY AM", "USDJPY"],\n'
            '  "strategy_critique": "Wysokie oceny A+ są filtrowane zbyt restrykcyjnie.",\n'
            '  "recommended_config": {\n'
            '    "AUTO_BE_R_TRIGGER": 1.30,\n'
            '    "MIN_CONFLUENCE_SCORE": 75\n'
            "  },\n"
            '  "action_items": ["Zwiększ próg BE do 1.3R", "Wyłącz NY AM"]\n'
            "}\n"
            "```\n"
            "Koniec raportu."
        )

        extracted = extract_recommendations_json(sample_response)
        self.assertIsNotNone(extracted)
        self.assertEqual(extracted["recommended_config"]["AUTO_BE_R_TRIGGER"], 1.30)
        self.assertEqual(extracted["recommended_config"]["MIN_CONFLUENCE_SCORE"], 75)
        self.assertEqual(len(extracted["capital_leaks"]), 2)

    def test_save_audit_report(self):
        sample_text = (
            "# Audit Report\n\n"
            "```json\n"
            '{"summary": "Test", "recommended_config": {"AUTO_BE_R_TRIGGER": 1.25}}\n'
            "```"
        )
        md_path, json_path, rec_data = save_audit_report(
            sample_text, output_dir=self.tmp_dir.name, prefix="test_audit"
        )

        self.assertTrue(os.path.exists(md_path))
        self.assertTrue(os.path.exists(json_path))
        self.assertIsNotNone(rec_data)
        self.assertEqual(rec_data["recommended_config"]["AUTO_BE_R_TRIGGER"], 1.25)

    def test_apply_recommendations_and_backup(self):
        # Create a mock .env
        env_file = os.path.join(self.tmp_dir.name, ".env.test")
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(
                "# Komentarz wstępny\n"
                "AUTO_BE_R_TRIGGER=0.85 # Stary próg BE\n"
                "MIN_CONFLUENCE_SCORE=70\n"
                "INNY_KLUCZ=abc\n"
            )

        # Create a mock recommendations.json
        rec_file = os.path.join(self.tmp_dir.name, "recommendations.json")
        rec_data = {
            "recommended_config": {
                "AUTO_BE_R_TRIGGER": 1.30,
                "MIN_CONFLUENCE_SCORE": 80,
                "MALICIOUS_KEY_NOT_ALLOWED": "hacked",  # should be ignored!
            }
        }
        with open(rec_file, "w", encoding="utf-8") as f:
            json.dump(rec_data, f)

        # 1. Dry run test
        ok_dry, msgs_dry = apply_recommendations(rec_file, env_file, dry_run=True)
        self.assertTrue(ok_dry)
        self.assertTrue(any("DRY-RUN" in m for m in msgs_dry))

        # 2. Live apply test
        ok, msgs = apply_recommendations(rec_file, env_file, dry_run=False)
        self.assertTrue(ok)

        # Verify .env content
        with open(env_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        content = "".join(lines)
        self.assertIn("AUTO_BE_R_TRIGGER=1.3", content)
        self.assertIn("MIN_CONFLUENCE_SCORE=80", content)
        self.assertNotIn("MALICIOUS_KEY_NOT_ALLOWED", content)

        # Verify backup was created
        backups = [f for f in os.listdir(self.tmp_dir.name) if f.startswith(".env.test.backup_")]
        self.assertEqual(len(backups), 1)

    @patch("httpx.Client.post")
    def test_query_gemini_api_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {"content": {"parts": [{"text": "Raport AI z sukcesem."}]}}
            ]
        }
        mock_post.return_value = mock_resp

        ok, text = query_gemini_api("Test prompt", api_key="dummy_key", model_name="gemini-1.5-pro")
        self.assertTrue(ok)
        self.assertEqual(text, "Raport AI z sukcesem.")

    @patch("httpx.Client.post")
    def test_query_gemini_api_async_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {"content": {"parts": [{"text": "Raport AI async z sukcesem."}]}}
            ]
        }
        mock_post.return_value = mock_resp

        ok, text = asyncio.run(query_gemini_api_async("Test prompt", api_key="dummy_key", model_name="gemini-flash-lite-latest"))
        self.assertTrue(ok)
        self.assertEqual(text, "Raport AI async z sukcesem.")


class TestTelegramAIAuditIntegration(unittest.IsolatedAsyncioTestCase):
    @patch("ai_auditor.run_audit")
    async def test_ai_audit_cmd_dispatches_cleanly(self, mock_run_audit):
        from telegram_bot import TelegramBotManager

        mock_run_audit.return_value = (
            "Raport z audytu AI.\n\nPodsumowanie wycieków kapitału.",
            {"winrate": 50.0},
            {"recommended_config": {"AUTO_BE_R_TRIGGER": 1.25}},
        )

        mock_mt5 = MagicMock()
        manager = TelegramBotManager(mt5_client=mock_mt5)
        manager.token = "dummy_token"
        manager.allowed_chat_ids = [12345]

        update = MagicMock()
        update.effective_user.id = 12345
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.args = ["30d"]

        await manager.ai_audit_cmd(update, context)

        # Assert initial progress reply was sent
        self.assertGreaterEqual(update.message.reply_text.call_count, 2)
        initial_call = update.message.reply_text.call_args_list[0][0][0]
        self.assertIn("Rozpoczynam audyt AI strategii", initial_call)

        # Assert report was sent
        report_call = update.message.reply_text.call_args_list[1][0][0]
        self.assertIn("Raport z audytu AI", report_call)

        # Assert recommendation call was sent with inline Apply/Reject buttons
        rec_call = update.message.reply_text.call_args_list[2]
        rec_text = rec_call[0][0]
        self.assertIn("ZAPROPONOWANE PARAMETRY DO WDROŻENIA", rec_text)
        self.assertIn("AUTO_BE_R_TRIGGER", rec_text)
        markup = rec_call[1].get("reply_markup")
        self.assertIsNotNone(markup)
        callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertEqual(callbacks, ["audit_apply", "audit_reject"])

    @patch("config.reload_runtime_config")
    @patch("ai_auditor.apply_recommendations")
    async def test_handle_callback_audit_apply_success(self, mock_apply, mock_reload):
        from telegram_bot import TelegramBotManager

        mock_apply.return_value = (
            True,
            [
                "📦 Utworzono kopię zapasową: .env.backup_20261127",
                "✅ Zaktualizowano: AUTO_BE_R_TRIGGER = 2.0",
            ],
        )

        manager = TelegramBotManager(mt5_client=MagicMock())
        manager.allowed_chat_ids = [12345]

        update = MagicMock()
        update.effective_user.id = 12345
        update.callback_query.data = "audit_apply"
        update.callback_query.answer = AsyncMock()
        update.callback_query.message.caption = None
        update.callback_query.message.text = "💡 ZAPROPONOWANE PARAMETRY DO WDROŻENIA:\n• AUTO_BE_R_TRIGGER: 2.0"
        update.callback_query.edit_message_text = AsyncMock()

        await manager.handle_callback(update, MagicMock())

        mock_apply.assert_called_once_with()
        mock_reload.assert_called_once()
        update.callback_query.answer.assert_awaited_once()
        edited_text = update.callback_query.edit_message_text.call_args[1]["text"]
        self.assertIn("Zmiany zostały automatycznie wdrożone na serwerze", edited_text)
        self.assertIn("przeładowane w pamięci bota", edited_text)
        self.assertIn("AUTO_BE_R_TRIGGER = 2.0", edited_text)
        self.assertIsNone(update.callback_query.edit_message_text.call_args[1]["reply_markup"])

    @patch("ai_auditor.apply_recommendations")
    async def test_handle_callback_audit_apply_failure_shows_hint(self, mock_apply):
        from telegram_bot import TelegramBotManager

        mock_apply.return_value = (False, ["Plik rekomendacji 'docs/audits/recommendations.json' nie istnieje."])

        manager = TelegramBotManager(mt5_client=MagicMock())
        manager.allowed_chat_ids = [12345]

        update = MagicMock()
        update.effective_user.id = 12345
        update.callback_query.data = "audit_apply"
        update.callback_query.answer = AsyncMock()
        update.callback_query.message.caption = None
        update.callback_query.message.text = "💡 ZAPROPONOWANE PARAMETRY DO WDROŻENIA:"
        update.callback_query.edit_message_text = AsyncMock()

        await manager.handle_callback(update, MagicMock())

        edited_text = update.callback_query.edit_message_text.call_args[1]["text"]
        self.assertIn("Nie udało się wdrożyć zmian", edited_text)
        self.assertIn("/ai_audit", edited_text)
        self.assertIsNone(update.callback_query.edit_message_text.call_args[1]["reply_markup"])

    def test_reload_runtime_config_updates_imported_constants(self):
        import config
        import ai_auditor

        orig_env = os.environ.get("AUTO_BE_R_TRIGGER")
        orig_val = config.AUTO_BE_R_TRIGGER
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                env_file = os.path.join(tmpdir, ".env")
                with open(env_file, "w", encoding="utf-8") as f:
                    f.write("AUTO_BE_R_TRIGGER=3.25\n")

                config.reload_runtime_config(keys=["AUTO_BE_R_TRIGGER"], dotenv_path=env_file)
                self.assertEqual(config.AUTO_BE_R_TRIGGER, 3.25)
                self.assertEqual(ai_auditor.AUTO_BE_R_TRIGGER, 3.25)
        finally:
            if orig_env is None:
                os.environ.pop("AUTO_BE_R_TRIGGER", None)
            else:
                os.environ["AUTO_BE_R_TRIGGER"] = orig_env
            config.AUTO_BE_R_TRIGGER = orig_val
            ai_auditor.AUTO_BE_R_TRIGGER = orig_val

    async def test_handle_callback_audit_reject(self):
        from telegram_bot import TelegramBotManager

        manager = TelegramBotManager(mt5_client=MagicMock())
        manager.allowed_chat_ids = [12345]

        update = MagicMock()
        update.effective_user.id = 12345
        update.callback_query.data = "audit_reject"
        update.callback_query.answer = AsyncMock()
        update.callback_query.message.caption = None
        update.callback_query.message.text = "💡 ZAPROPONOWANE PARAMETRY DO WDROŻENIA:"
        update.callback_query.edit_message_text = AsyncMock()

        await manager.handle_callback(update, MagicMock())

        update.callback_query.answer.assert_awaited_once()
        edited_text = update.callback_query.edit_message_text.call_args[1]["text"]
        self.assertIn("Zmiany odrzucone przez użytkownika", edited_text)
        self.assertIsNone(update.callback_query.edit_message_text.call_args[1]["reply_markup"])

    async def test_send_chunked_html_handles_large_unbroken_text(self):
        from telegram_bot import TelegramBotManager

        mock_mt5 = MagicMock()
        manager = TelegramBotManager(mt5_client=mock_mt5)
        mock_target = MagicMock()
        mock_target.reply_text = AsyncMock()

        # 8500 characters of unbroken text without \n\n
        long_text = "Linia audytu " * 650
        self.assertGreater(len(long_text), 8000)

        await manager._send_chunked_html(mock_target, long_text)

        # Ensure all sent chunks are safely under Telegram's 4096 limit
        self.assertGreaterEqual(mock_target.reply_text.call_count, 2)
        for call_item in mock_target.reply_text.call_args_list:
            chunk_sent = call_item[0][0]
            self.assertLessEqual(len(chunk_sent), 4000)


if __name__ == "__main__":
    unittest.main()
