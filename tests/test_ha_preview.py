import io
import json
import os
import sys
from pathlib import Path
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ha_preview
import web_server
from comparison_preview import ComparisonPreview


COMMAND = json.dumps({"command": "comparison_preview"}) + "\n"


class HomeAssistantPreviewTests(unittest.TestCase):
    def test_only_supported_command_is_accepted_without_logging_raw_input(self):
        preview = Mock()
        invalid = ["private-secret", "null", "[]", '"comparison_preview"',
                   '{"command":"switch"}',
                   '{"command":"comparison_preview","secret":"private-secret"}']
        with self.assertLogs("octobot.ha_preview", level="INFO") as captured:
            ha_preview.listen_for_preview_commands(
                preview, io.StringIO("\n" + "\n".join(invalid) + "\n" + COMMAND))
        preview.start.assert_called_once_with()
        self.assertNotIn("private-secret", "\n".join(captured.output))

    def test_oversized_line_is_drained_before_next_command(self):
        preview = Mock()
        # A valid command hidden at the end of an oversized line is not run.
        stream = io.StringIO("x" * (ha_preview.MAX_COMMAND_LENGTH * 3) + COMMAND + COMMAND)
        with self.assertLogs("octobot.ha_preview", level="WARNING"):
            ha_preview.listen_for_preview_commands(preview, stream)
        preview.start.assert_called_once_with()

    def test_busy_and_failed_start_do_not_stop_listener(self):
        preview = Mock()
        preview.start.side_effect = [False, RuntimeError("private-secret"), True]
        with self.assertLogs("octobot.ha_preview", level="INFO") as captured:
            ha_preview.listen_for_preview_commands(preview, io.StringIO(COMMAND * 3))
        self.assertEqual(preview.start.call_count, 3)
        output = "\n".join(captured.output)
        self.assertIn("already running", output)
        self.assertIn("started a comparison preview", output)
        self.assertNotIn("private-secret", output)

    def test_closed_stdin_exits_without_starting_preview(self):
        preview = Mock()
        with patch.object(ha_preview.sys, "stdin", io.StringIO("")):
            thread = ha_preview.start_preview_listener(preview)
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(thread.daemon)
        preview.start.assert_not_called()

    def test_newline_delivers_command_without_waiting_for_stdin_to_close(self):
        received = Event()
        preview = Mock()
        preview.start.side_effect = lambda: received.set() or True
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "r", encoding="utf-8") as reader:
            thread = Thread(target=ha_preview.listen_for_preview_commands,
                            args=(preview, reader), daemon=True)
            thread.start()
            try:
                with os.fdopen(write_fd, "w", encoding="utf-8") as writer:
                    for _ in range(2):
                        received.clear()
                        writer.write(COMMAND)
                        writer.flush()
                        self.assertTrue(received.wait(2))
                    self.assertTrue(thread.is_alive())
            finally:
                thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(preview.start.call_count, 2)

    def test_ha_and_web_share_worker_progress_and_reject_overlapping_previews(self):
        preview = ComparisonPreview()
        started, release, finished = Event(), Event(), Event()
        original_finish = preview._finish

        def run():
            started.set()
            if not release.wait(5):
                raise TimeoutError("test did not release preview")
            return True

        def finish(success):
            original_finish(success)
            finished.set()

        with patch("comparison_preview.BotOrchestrator") as bot, \
                patch.object(web_server, "comparison_preview", preview), \
                patch.object(preview, "_finish", side_effect=finish):
            bot.return_value.run_comparison_preview.side_effect = run
            client = web_server.app.test_client()
            headers = {"X-Ingress-Path": "/api/hassio_ingress/test"}
            try:
                ha_preview.listen_for_preview_commands(preview, io.StringIO(COMMAND))
                self.assertTrue(started.wait(2))
                self.assertTrue(client.get("/preview-status", headers=headers).json["running"])
                self.assertIn(b'id="preview-button" disabled', client.get("/", headers=headers).data)
                client.post("/preview", headers=headers,
                            data={"preview_token": web_server.preview_token})
                ha_preview.listen_for_preview_commands(preview, io.StringIO(COMMAND))
                bot.return_value.run_comparison_preview.assert_called_once()
            finally:
                release.set()
                self.assertTrue(finished.wait(2))
            self.assertEqual(client.get("/preview-status", headers=headers).json["state"], "complete")
            # A completed HA preview also allows another WebUI preview.
            with patch("comparison_preview.Thread") as thread:
                client.post("/preview", headers=headers,
                            data={"preview_token": web_server.preview_token})
                thread.return_value.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
