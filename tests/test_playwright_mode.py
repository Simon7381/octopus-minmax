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


if __name__ == "__main__":
    unittest.main()
