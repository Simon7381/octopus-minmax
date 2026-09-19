import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import config
from apprise import AppriseAttachment
from notification_service import DISCORD_CHAR_LIMIT, NotificationService


class NotificationAttachmentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        previous = Path.cwd()
        os.chdir(directory.name)
        self.addCleanup(os.chdir, previous)
        self.logs = Path("logs")
        self.logs.mkdir()
        for name in ("octobot.log", "login.png", "stage.PNG", "ignore.txt"):
            (self.logs / name).write_bytes(b"synthetic diagnostic")
        profile = self.logs / "browser_profile"
        profile.mkdir()
        (profile / "private.png").write_bytes(b"profile data")
        (self.logs / "directory.png").mkdir()
        self.enterContext(patch.object(config, "DEBUG", True))
        self.enterContext(patch.object(config, "BATCH_NOTIFICATIONS", False))
        self.enterContext(patch.object(config, "NOTIFICATION_URLS", ""))
        self.apprise = Mock()
        self.apprise.notify.return_value = True
        self.enterContext(patch.object(NotificationService, "_get_apprise", return_value=self.apprise))
        self.service = NotificationService("", False)

    def test_debug_error_attaches_log_and_top_level_pngs(self):
        self.assertTrue(self.service.send_notification("Browser login failed", is_error=True, batchable=False))
        attachments = self.apprise.notify.call_args.kwargs["attach"]
        self.assertEqual({Path(p).name for p in attachments}, {"octobot.log", "login.png", "stage.PNG"})
        # Validate the real Apprise attachment parser using synthetic local files.
        parsed = AppriseAttachment(attachments)
        self.assertEqual(len(parsed), 3)
        self.assertTrue(all(attachment for attachment in parsed))

    def test_debug_disabled_error_and_normal_messages_are_text_only(self):
        with patch.object(config, "DEBUG", False):
            self.service.send_notification("Error", is_error=True)
        self.service.send_notification("Login succeeded")
        for call in self.apprise.notify.call_args_list:
            self.assertNotIn("attach", call.kwargs)

    def test_missing_diagnostics_do_not_block_notification(self):
        with patch("notification_service.Path.iterdir", side_effect=FileNotFoundError):
            self.assertTrue(self.service.send_notification("Error", is_error=True))
        self.assertNotIn("attach", self.apprise.notify.call_args.kwargs)

    def test_unreadable_file_is_skipped(self):
        real_open = Path.open

        def open_readable(path, *args, **kwargs):
            if path.name == "octobot.log":
                raise PermissionError("Synthetic permission failure")
            return real_open(path, *args, **kwargs)

        with patch.object(Path, "open", open_readable):
            self.assertTrue(self.service.send_notification("Error", is_error=True))
        self.assertEqual(
            {Path(p).name for p in self.apprise.notify.call_args.kwargs["attach"]},
            {"login.png", "stage.PNG"},
        )

    def test_long_error_attaches_once_and_reports_any_delivery_failure(self):
        message = "x" * (DISCORD_CHAR_LIMIT * 2 + 1)
        self.apprise.notify.side_effect = [False, True, True]
        self.assertFalse(self.service.send_notification(message, title="Error", is_error=True))
        calls = self.apprise.notify.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual("".join(call.kwargs["body"][6:-4] for call in calls), message)
        self.assertTrue(all("attach" not in call.kwargs for call in calls[:-1]))
        self.assertIn("attach", calls[-1].kwargs)
        self.assertEqual(calls[-1].kwargs["title"], "Error")

    def test_batch_collects_latest_files_retains_on_failure_then_resets(self):
        with patch.object(config, "BATCH_NOTIFICATIONS", True):
            self.service.send_notification("Error", is_error=True)
            self.service.send_notification("Another error", is_error=True)
            self.apprise.notify.assert_not_called()
            (self.logs / "later.png").write_bytes(b"later screenshot")
            self.apprise.notify.side_effect = [False, True, True]
            self.assertFalse(self.service.send_batch_notification())
            self.assertTrue(self.service.send_batch_notification())
            for call in self.apprise.notify.call_args_list:
                self.assertEqual(len(call.kwargs["attach"]), 4)
            self.service.send_notification("Normal batch")
            self.assertTrue(self.service.send_batch_notification())
            self.assertNotIn("attach", self.apprise.notify.call_args.kwargs)

    def test_turning_debug_off_before_batch_delivery_suppresses_attachments(self):
        with patch.object(config, "BATCH_NOTIFICATIONS", True):
            self.service.send_notification("Error", is_error=True)
            with patch.object(config, "DEBUG", False):
                self.assertTrue(self.service.send_batch_notification())
        self.assertNotIn("attach", self.apprise.notify.call_args.kwargs)

    def test_errors_queued_without_debug_do_not_gain_attachments(self):
        with patch.object(config, "BATCH_NOTIFICATIONS", True):
            with patch.object(config, "DEBUG", False):
                self.service.send_notification("Error", is_error=True)
            self.assertTrue(self.service.send_batch_notification())
        self.assertNotIn("attach", self.apprise.notify.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
