"""Regression checks for the browser-process deadline, before login exists."""

import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from invisible_playwright import InvisiblePlaywright

from browser_runtime import PAGE_CREATION_TIMEOUT_SECONDS, new_browser_page


class PageCreationTimeoutTests(unittest.TestCase):
    def test_only_page_creation_deadline_changes_and_connection_is_restored(self):
        context = Mock()
        connection = context._impl_obj._connection._transport._server.object.return_value.conn
        original_send = connection.send

        def create():
            connection.send("Browser.newPage", {"browserContextId": "test"}, timeout=30)
            connection.send("Page.navigate", {"url": "about:blank"}, session="test", timeout=10)
            connection.send("Browser.newPage", timeout=180)
            return "page"

        context.new_page.side_effect = create
        self.assertEqual(new_browser_page(context), "page")
        self.assertEqual(original_send.call_args_list[0].kwargs["timeout"], PAGE_CREATION_TIMEOUT_SECONDS)
        self.assertEqual(original_send.call_args_list[1].kwargs, {"session": "test", "timeout": 10})
        self.assertEqual(original_send.call_args_list[2].kwargs["timeout"], 180)
        self.assertIs(connection.send, original_send)
        context.new_page.assert_called_once_with()

    def test_failure_restores_connection_without_retrying_page_creation(self):
        context = Mock()
        connection = context._impl_obj._connection._transport._server.object.return_value.conn
        original_send = connection.send
        context.new_page.side_effect = RuntimeError("browser closed")
        with self.assertRaisesRegex(RuntimeError, "browser closed"):
            new_browser_page(context)
        self.assertIs(connection.send, original_send)
        context.new_page.assert_called_once_with()

    def test_incompatible_transport_fails_explicitly_before_creating_page(self):
        context = Mock(spec=["new_page"])
        with self.assertRaisesRegex(RuntimeError, "incompatible Invisible Playwright transport"):
            new_browser_page(context)
        context.new_page.assert_not_called()


class PageCreationProtocolTests(unittest.TestCase):
    def test_real_browser_accepts_reply_delayed_beyond_old_30_second_deadline(self):
        # Hold only the Browser.newPage reply, not the reader thread/events.
        # This exercises the real transport wait that set_default_timeout does
        # not control, without depending on an unpredictably overloaded host.
        with tempfile.TemporaryDirectory() as profile:
            with InvisiblePlaywright(profile_dir=profile, headless=True,
                                     locale="en-GB", timezone="Europe/London") as context:
                impl = context._impl_obj
                connection = impl._connection._transport._server.object(impl._guid).conn
                original_write, original_deliver = connection._write, connection._deliver
                page_commands, timers = [], []

                def write(message):
                    if message.get("method") == "Browser.newPage":
                        page_commands.append(message["id"])
                    original_write(message)

                def deliver(raw):
                    if json.loads(raw).get("id") in page_commands:
                        timer = threading.Timer(31, original_deliver, args=(raw,))
                        timer.daemon = True
                        timers.append(timer)
                        timer.start()
                    else:
                        original_deliver(raw)

                connection._write, connection._deliver = write, deliver
                try:
                    started = time.monotonic()
                    page = new_browser_page(context)
                    self.assertGreaterEqual(time.monotonic() - started, 31)
                    self.assertEqual(page.evaluate("1 + 1"), 2)
                    self.assertEqual(len(page_commands), 1)
                    page.close()
                finally:
                    connection._write, connection._deliver = original_write, original_deliver
                    for timer in timers:
                        timer.cancel()


if __name__ == "__main__":
    unittest.main()
