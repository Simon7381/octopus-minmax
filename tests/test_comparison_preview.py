import sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config
import web_server
from bot_orchestrator import BotOrchestrator
from comparison_engine import ComparisonResult, CostBreakdown, TariffComparison
from comparison_preview import ComparisonPreview
from tariff import TARIFFS


class ComparisonPreviewTests(unittest.TestCase):
    def test_preview_cannot_switch_even_with_live_mode_and_large_savings(self):
        bot = BotOrchestrator()
        current, cheaper = TARIFFS[0], TARIFFS[2]
        result = ComparisonResult(
            TariffComparison(current, CostBreakdown(200, 50, 250, 10)),
            [TariffComparison(cheaper, CostBreakdown(100, 40, 140, 10))],
            cheaper, 110,
        )
        with patch.dict(config.__dict__, {"DRY_RUN": False, "ONE_OFF_EXECUTED": True,
                                        "TARIFFS": "go,agile", "BATCH_NOTIFICATIONS": True}), \
                patch("bot_orchestrator.QueryService"), \
                patch("bot_orchestrator.AccountManager") as account, \
                patch("bot_orchestrator.ComparisonEngine") as engine, \
                patch("bot_orchestrator.NotificationService") as notifications, \
                patch("bot_orchestrator.prewarm_browser_login") as login, \
                patch.object(bot, "_execute_switch") as switch:
            engine.return_value.compare_tariffs.return_value = result
            with self.assertLogs("octobot.bot_orchestrator", level="INFO") as logs:
                self.assertTrue(bot.run_comparison_preview())
            self.assertIn("PREVIEW", str(logs.output))
            self.assertFalse(config.DRY_RUN)
            self.assertTrue(config.ONE_OFF_EXECUTED)
            self.assertIsNone(bot.last_execution_datetime)
            account.get_instance.assert_not_called()
            account.return_value.fetch_current_account_info.assert_called_once()
            account.return_value.initiate_tariff_switch.assert_not_called()
            account.return_value.accept_new_agreement.assert_not_called()
            login.assert_not_called()
            switch.assert_not_called()
            for tariff in bot.tariffs:
                self.assertIsNot(tariff, next(t for t in TARIFFS if t.id == tariff.id))
            call = notifications.return_value.send_notification.call_args
            self.assertFalse(call.kwargs["batchable"])
            self.assertIn("PREVIEW", call.args[0])
            self.assertIn("£2.50", call.args[0])
            self.assertIn("£1.40", call.args[0])
            self.assertIn("£1.10", call.args[0])
            self.assertNotIn("Initiating Switch", call.args[0])

    def test_preview_failure_is_reported_without_exposing_raw_api_error(self):
        with patch("bot_orchestrator.QueryService", side_effect=RuntimeError("private-api-key")), \
                patch("bot_orchestrator.NotificationService") as notifications, \
                self.assertLogs("octobot.bot_orchestrator", level="ERROR") as logs:
            self.assertFalse(BotOrchestrator().run_comparison_preview())
        self.assertNotIn("private-api-key", str(logs.output))
        call = notifications.return_value.send_notification.call_args
        self.assertTrue(call.kwargs["is_error"])
        self.assertFalse(call.kwargs["batchable"])
        self.assertIn("PREVIEW failed", call.args[0])
        self.assertNotIn("private-api-key", str(call))

    def test_preview_handles_no_eligible_tariff(self):
        bot = BotOrchestrator()
        with patch("bot_orchestrator.QueryService"), patch("bot_orchestrator.AccountManager"), \
                patch("bot_orchestrator.NotificationService") as notifications, \
                patch("bot_orchestrator.ComparisonEngine") as engine, \
                patch.object(bot, "_format_comparison_summary", return_value="Costs"):
            engine.return_value.compare_tariffs.return_value = SimpleNamespace(cheapest_tariff=None)
            self.assertTrue(bot.run_comparison_preview())
            self.assertIn("No eligible switchable tariff", notifications.return_value.send_notification.call_args.args[0])


class PreviewWorkerTests(unittest.TestCase):
    def test_duplicate_clicks_do_not_overlap_and_completion_allows_another_preview(self):
        preview = ComparisonPreview()
        started, release, finished = Event(), Event(), Event()
        original_finish = preview._finish

        def run():
            started.set()
            if not release.wait(5):
                raise TimeoutError("test did not release worker")
            return True

        def finish(success):
            original_finish(success)
            finished.set()

        with patch("comparison_preview.BotOrchestrator") as bot, \
                patch.object(preview, "_finish", side_effect=finish):
            bot.return_value.run_comparison_preview.side_effect = run
            try:
                self.assertTrue(preview.start())
                self.assertTrue(started.wait(2))
                self.assertFalse(preview.start())
                self.assertTrue(preview.status()["running"])
            finally:
                release.set()
                self.assertTrue(finished.wait(2))
            self.assertEqual(preview.status()["state"], "complete")
            bot.return_value.run_comparison_preview.assert_called_once()
            with patch("comparison_preview.Thread"):
                self.assertTrue(preview.start())

    def test_worker_exception_and_thread_start_failure_allow_retry(self):
        preview = ComparisonPreview()
        with patch("comparison_preview.BotOrchestrator", side_effect=RuntimeError("unavailable")):
            preview._run()
        self.assertEqual(preview.status()["state"], "failed")
        with patch("comparison_preview.Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("cannot start")
            with self.assertRaises(RuntimeError):
                preview.start()
        self.assertFalse(preview.status()["running"])
        with patch("comparison_preview.Thread"):
            self.assertTrue(preview.start())


class PreviewRouteTests(unittest.TestCase):
    def setUp(self):
        self.settings = patch.dict(config.__dict__, {"WEB_USERNAME": "test", "WEB_PASSWORD": "test"})
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.client = web_server.app.test_client()
        self.auth = ("test", "test")

    def test_post_requires_auth_and_dashboard_token_and_get_does_not_start(self):
        with patch.object(web_server.comparison_preview, "start") as start:
            self.assertEqual(self.client.post("/preview").status_code, 401)
            self.assertEqual(self.client.get("/preview", auth=self.auth).status_code, 405)
            for value in ("", "wrong", "£"):
                self.assertEqual(self.client.post("/preview", data={"preview_token": value}, auth=self.auth).status_code, 400)
            start.assert_not_called()
            self.assertEqual(self.client.get("/preview-status").status_code, 401)

    def test_ingress_form_and_redirect_keep_relative_paths(self):
        headers = {"X-Ingress-Path": "/api/hassio_ingress/test"}
        page = self.client.get("/", headers=headers)
        self.assertIn(b'action="preview"', page.data)
        self.assertIn(web_server.preview_token.encode(), page.data)
        self.assertIn(b"Run comparison preview", page.data)
        with patch.object(web_server.comparison_preview, "start", return_value=True) as start:
            response = self.client.post("/preview", data={"preview_token": web_server.preview_token}, headers=headers)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["Location"], ".")
            start.assert_called_once()

    def test_running_state_disables_button_and_status_is_not_cached(self):
        state = {"running": True, "state": "running", "message": "Preview running."}
        with patch.object(web_server.comparison_preview, "status", return_value=state), \
                patch.object(web_server.comparison_preview, "start", return_value=False):
            page = self.client.get("/", auth=self.auth)
            self.assertIn(b'id="preview-button" disabled', page.data)
            response = self.client.get("/preview-status", auth=self.auth)
            self.assertEqual(response.json, state)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.client.post("/preview", data={"preview_token": web_server.preview_token}, auth=self.auth)
            response = self.client.get("/", auth=self.auth)
            self.assertIn(b"already running", response.data)


if __name__ == "__main__":
    unittest.main()
