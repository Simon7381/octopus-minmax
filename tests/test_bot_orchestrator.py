import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config
from bot_orchestrator import BotOrchestrator


class LoginRecoveryTests(unittest.TestCase):
    def setUp(self):
        settings = patch.dict(config.__dict__, {
            "TEST_PLAYWRIGHT": False, "ONE_OFF_RUN": False,
            "EXECUTION_TIME": "12:00", "DRY_RUN": False,
            "ACC_NUMBER": "A-12345678", "OCTOPUS_LOGIN_EMAIL": "email",
            "OCTOPUS_LOGIN_PASSWD": "private-password", "BATCH_NOTIFICATIONS": True,
        })
        settings.start()
        self.addCleanup(settings.stop)
        self.bot = BotOrchestrator()
        self.bot.notification_service = Mock()
        self.bot.account_manager = Mock()
        self.bot.query_service = Mock()
        self.target = SimpleNamespace(product_code="AGILE-TEST", display_name="Agile")

    def test_scheduled_login_uses_delay_and_comparison_survives_failure(self):
        for fails in (False, True):
            for elapsed in (20, 120):
                with self.subTest(fails=fails, elapsed=elapsed):
                    bot = BotOrchestrator()
                    events = []

                    def login(*args):
                        events.append("login")
                        if fails and events.count("login") == 2:
                            raise RuntimeError("login unavailable")
                        return "A-12345678"

                    def sleep(seconds):
                        events.append(("sleep", seconds))
                        if seconds == 30:
                            raise InterruptedError("stop loop")

                    with patch("bot_orchestrator.NotificationService") as notifications, \
                            patch("bot_orchestrator.datetime") as clock, \
                            patch("bot_orchestrator.random.randint", return_value=90), \
                            patch("bot_orchestrator.time.monotonic", side_effect=[100, 100 + elapsed]), \
                            patch("bot_orchestrator.time.sleep", side_effect=sleep), \
                            patch("bot_orchestrator.prewarm_browser_login", side_effect=login), \
                            patch.object(bot, "_run_tariff_compare", side_effect=lambda: events.append("compare")):
                        clock.now.return_value = datetime(2026, 9, 26, 12, 0)
                        notifications.return_value.send_notification.side_effect = (
                            lambda message, **kwargs: events.append("scheduled")
                            if "Initiating comparison" in message else None
                        )
                        with self.assertRaises(InterruptedError):
                            bot.start()

                    expected = ["login", "scheduled", ("sleep", 9), "login"]
                    if elapsed < 90:
                        expected.append(("sleep", 90 - elapsed))
                    expected.extend(["compare", ("sleep", 30)])
                    self.assertEqual(events, expected)
                    self.assertEqual(bot._browser_login_failed, fails)

    def fail_initial_login(self):
        with patch("bot_orchestrator.prewarm_browser_login", side_effect=RuntimeError("unavailable")):
            self.bot._prewarm_browser_login()
        self.assertTrue(self.bot._browser_login_failed)

    def compare(self, *, should_switch=True):
        results = SimpleNamespace(
            should_switch=should_switch, cheapest_tariff=self.target,
            current_tariff_comparison=SimpleNamespace(
                tariff=self.target, cost_breakdown=SimpleNamespace(total_cost_pounds=1)
            ),
        )
        with patch.object(self.bot, "_initialize"), \
                patch.object(self.bot, "_format_comparison_summary", return_value="Comparison summary"), \
                patch("bot_orchestrator.ComparisonEngine") as engine:
            engine.return_value.compare_tariffs.return_value = results
            self.bot._run_tariff_compare()
            engine.return_value.compare_tariffs.assert_called_once()
        self.bot.notification_service.send_notification.assert_any_call(message="Comparison summary")

    def test_failed_login_retries_once_then_switches(self):
        self.fail_initial_login()
        with patch("bot_orchestrator.prewarm_browser_login", return_value="A-12345678") as login, \
                patch("bot_orchestrator.time.sleep"):
            self.compare()
        login.assert_called_once_with("A-12345678", "email", "private-password")
        self.bot.account_manager.initiate_tariff_switch.assert_called_once_with("AGILE-TEST")
        self.assertFalse(self.bot._browser_login_failed)
        self.bot.notification_service.send_notification.assert_any_call(
            "Browser login is unavailable. Retrying login once before switching.",
            is_error=True, batchable=False,
        )

    def test_final_login_failure_preserves_comparison_and_prevents_switch(self):
        self.fail_initial_login()
        with patch("bot_orchestrator.prewarm_browser_login", side_effect=RuntimeError("private-password")) as login:
            self.compare()
        login.assert_called_once()
        self.bot.account_manager.initiate_tariff_switch.assert_not_called()
        self.bot.account_manager.accept_new_agreement.assert_not_called()
        self.assertTrue(self.bot._browser_login_failed)
        notifications = self.bot.notification_service.send_notification.call_args_list
        self.assertTrue(any(
            "failed after the final retry" in call.kwargs.get("message", "")
            and call.kwargs.get("is_error") for call in notifications
        ))
        self.assertNotIn("private-password", str(notifications))
        self.bot.notification_service.send_batch_notification.assert_called_once()

    def test_no_switch_and_dry_run_do_not_retry_failed_login(self):
        for dry_run, should_switch in ((False, False), (True, True)):
            with self.subTest(dry_run=dry_run), patch.object(config, "DRY_RUN", dry_run):
                self.fail_initial_login()
                with patch("bot_orchestrator.prewarm_browser_login") as login:
                    self.compare(should_switch=should_switch)
                login.assert_not_called()
                self.bot.account_manager.initiate_tariff_switch.assert_not_called()

    def test_later_successful_check_clears_failure(self):
        self.fail_initial_login()
        with patch("bot_orchestrator.prewarm_browser_login", return_value="A-12345678"):
            self.bot._prewarm_browser_login()
        with patch("bot_orchestrator.prewarm_browser_login") as login, \
                patch("bot_orchestrator.time.sleep"):
            self.compare()
        login.assert_not_called()
        self.bot.account_manager.initiate_tariff_switch.assert_called_once_with("AGILE-TEST")

    def test_missing_credentials_allow_comparison_but_block_switch(self):
        with patch.object(config, "OCTOPUS_LOGIN_EMAIL", ""), \
                patch("bot_orchestrator.prewarm_browser_login") as login:
            self.bot._prewarm_browser_login()
            login.assert_not_called()
            login.side_effect = RuntimeError("Browser login requires credentials")
            self.compare()
            login.assert_called_once()
        self.bot.account_manager.initiate_tariff_switch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
