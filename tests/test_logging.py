import io
import logging
import runpy
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import config
from diagnostics import error_summary, log_failure
from logger import setup_logging
from notification_service import NotificationService
from query_service import QueryService


class LoggingTests(unittest.TestCase):
    def test_debug_toggle_controls_file_and_console_without_duplicate_handlers(self):
        isolated = logging.getLogger("logging-test")
        console = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
                cleanup.callback(lambda: [handler.close() for handler in isolated.handlers])
                with patch("logger.logging.getLogger", return_value=isolated), patch("sys.stderr", console):
                    with patch.object(config, "DEBUG", False):
                        setup_logging(directory)
                        isolated.debug("hidden detail")
                        isolated.info("important event")
                    with patch.object(config, "DEBUG", True):
                        setup_logging(directory)
                        isolated.debug("enabled detail")
                    with patch.object(config, "DEBUG", False):
                        setup_logging(directory)
                        isolated.debug("disabled detail")
                        isolated.error("switch failed")
                    self.assertEqual(len(isolated.handlers), 2)
                    for handler in isolated.handlers:
                        handler.flush()
                    outputs = [console.getvalue(), (Path(directory) / "octobot.log").read_text()]
                    for output in outputs:
                        self.assertNotIn("hidden detail", output)
                        self.assertNotIn("disabled detail", output)
                        self.assertEqual(output.count("important event"), 1)
                        self.assertIn("enabled detail", output)
                        self.assertIn("switch failed", output)
                for handler in isolated.handlers:
                    handler.close()
        finally:
            for handler in isolated.handlers[:]:
                handler.close()
                isolated.removeHandler(handler)

    def test_main_initializes_logging_before_starting_threads(self):
        fake_logger = Mock()
        with patch("logger.setup_logging", return_value=fake_logger) as setup, \
                patch.dict(sys.modules, {"web_server": Mock(), "bot_orchestrator": Mock()}), \
                patch("threading.Thread") as thread:
            thread.return_value.start.side_effect = lambda: setup.assert_called_once()
            runpy.run_path(str(ROOT / "src" / "main.py"))
            self.assertEqual(thread.return_value.start.call_count, 2)
            fake_logger.info.assert_any_call(
                "Octobot %s starting; DEBUG=%s; log file: logs/octobot.log",
                config.BOT_VERSION, config.DEBUG,
            )

    def test_diagnostics_keep_stack_locations_without_raw_exception_secrets(self):
        log = logging.getLogger("octobot.diagnostics-test")
        with self.assertLogs(log, level="DEBUG") as captured:
            try:
                raise RuntimeError('Locator.count: Failed to find execution context with id = id-9 password=secret-value')
            except RuntimeError as exc:
                log_failure(log, "preparing agile signup", exc)
        output = "\n".join(captured.output)
        self.assertIn("preparing agile signup", output)
        self.assertIn("Locator.count", output)
        self.assertIn("execution context unavailable", output)
        self.assertIn("stack locations", output)
        self.assertNotIn("secret-value", output)
        self.assertNotIn("id-9", output)

    def test_token_and_account_payloads_are_not_logged_at_debug(self):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {"data": {"obtainKrakenToken": {"token": "private-token"}}}
        with patch.object(QueryService, "_shared_token", None), \
                patch("query_service.requests.post", return_value=response), \
                self.assertLogs("octobot.query_service", level="DEBUG") as captured:
            service = QueryService("private-api-key", "https://example.invalid")
            response.json.return_value = {"data": {"account": {"number": "private-account"}}}
            service.execute_gql_query('query { account(number: "private-account") { number } }')
        output = "\n".join(captured.output)
        for secret in ("private-token", "private-api-key", "private-account"):
            self.assertNotIn(secret, output)
        self.assertIn("HTTP 200", output)

    def test_browser_errors_keep_safe_operation_and_failure_details(self):
        cases = [
            ("BrowserContext.new_page: Timeout 30000ms exceeded. password=secret-value",
             "RuntimeError in BrowserContext.new_page (timed out after 30000 ms)"),
            ("BrowserContext.new_page: Target page, context or browser has been closed; token=secret-value",
             "RuntimeError in BrowserContext.new_page (browser or page closed/crashed)"),
            ("Failed to create new page https://example.invalid/secret-value",
             "RuntimeError (browser could not create a page)"),
        ]
        for message, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(error_summary(RuntimeError(message)), expected)

    def test_batched_notifications_still_log_important_events_immediately(self):
        with patch.object(config, "BATCH_NOTIFICATIONS", True), \
                patch.object(config, "NOTIFICATION_URLS", ""), \
                patch.object(NotificationService, "_get_apprise", return_value=Mock()) as apprise, \
                self.assertLogs("octobot.notification_service", level="INFO") as captured:
            service = NotificationService("", True)
            service.send_notification("Initiating Switch to Agile Octopus")
        self.assertIn("Initiating Switch to Agile Octopus", captured.output[0])
        apprise.return_value.notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
