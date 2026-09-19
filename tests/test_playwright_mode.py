import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import config
from bot_orchestrator import BotOrchestrator


class PlaywrightModeTests(unittest.TestCase):
    def test_mode_prevents_comparison_scheduling_and_notifications(self):
        for fails in (False, True):
            with self.subTest(browser_fails=fails), patch.dict(config.__dict__, {
                    "TEST_PLAYWRIGHT": True, "DRY_RUN": False, "ONE_OFF_RUN": False}), \
                    patch("bot_orchestrator.run_playwright_checks") as check, \
                    patch("bot_orchestrator.prewarm_browser_login") as prewarm, \
                    patch("bot_orchestrator.NotificationService") as notifications, \
                    patch.object(BotOrchestrator, "_run_tariff_compare") as compare, \
                    patch("bot_orchestrator.time.sleep") as sleep:
                check.return_value = [{"passed": True}] * 3
                if fails:
                    check.side_effect = RuntimeError("Login failed")
                BotOrchestrator().start()
                check.assert_called_once()
                compare.assert_not_called()
                notifications.assert_not_called()
                sleep.assert_not_called()
                prewarm.assert_not_called()


class StartupLoginTests(unittest.TestCase):
    def run_startup(self, *, fails=False, email="email", one_off=False):
        events = []
        with patch.dict(config.__dict__, {
                "TEST_PLAYWRIGHT": False, "ONE_OFF_RUN": one_off,
                "ONE_OFF_EXECUTED": False, "EXECUTION_TIME": "never",
                "ACC_NUMBER": "A-12345678", "OCTOPUS_LOGIN_EMAIL": email,
                "OCTOPUS_LOGIN_PASSWD": "private-password", "BATCH_NOTIFICATIONS": True,
        }), patch("bot_orchestrator.NotificationService") as notifications, \
                patch("bot_orchestrator.prewarm_browser_login") as prewarm, \
                patch.object(BotOrchestrator, "_run_tariff_compare", side_effect=lambda: events.append("compare")), \
                patch("bot_orchestrator.time.sleep", side_effect=InterruptedError("stop loop")):
            ns = notifications.return_value
            ns.send_notification.side_effect = lambda message, **kwargs: events.append(message)

            def login(*args):
                events.append("login")
                if fails:
                    raise RuntimeError("private-password A-12345678")
                return "A-12345678"

            prewarm.side_effect = login
            with self.assertRaises(InterruptedError):
                BotOrchestrator().start()
            if email:
                prewarm.assert_called_once_with("A-12345678", "email", "private-password")
            else:
                prewarm.assert_not_called()
            for call in ns.send_notification.call_args_list[:3]:
                self.assertFalse(call.kwargs["batchable"])
        self.assertNotIn("A-12345678", "\n".join(events))
        self.assertNotIn("private-password", "\n".join(events))
        return events

    def test_startup_warms_login_after_mode_notification(self):
        events = self.run_startup()
        self.assertIn("Scheduled mode", events[0])
        self.assertIn("for dashboard", events[0])
        self.assertIn("Browser login in progress", events[1])
        self.assertEqual(events[2], "login")
        self.assertIn("Verified account *******678", events[3])

    def test_login_failure_reports_error_and_continues_one_off_comparison(self):
        events = self.run_startup(fails=True, one_off=True)
        self.assertIn("Browser login failed", events[3])
        self.assertEqual(events[-1], "compare")
        self.assertFalse(any("Browser login complete" in event for event in events))

    def test_missing_credentials_skip_login(self):
        events = self.run_startup(email="")
        self.assertIn("Browser login skipped", events[1])

    def test_one_off_comparison_runs_after_login(self):
        events = self.run_startup(one_off=True)
        self.assertIn("Browser login complete", events[3])
        self.assertEqual(events[-1], "compare")


if __name__ == "__main__":
    unittest.main()
