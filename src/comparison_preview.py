"""One background comparison preview at a time, independent of scheduling."""

import logging
from threading import Lock, Thread

from bot_orchestrator import BotOrchestrator
from diagnostics import log_failure

logger = logging.getLogger("octobot.comparison_preview")


class ComparisonPreview:
    def __init__(self):
        self._lock = Lock()
        self._state = "idle"
        self._message = "Ready to compare today's available consumption."

    def status(self):
        with self._lock:
            return {"running": self._state == "running", "state": self._state,
                    "message": self._message}

    def start(self):
        with self._lock:
            if self._state == "running":
                return False
            self._state = "running"
            self._message = "Preview running. Results will appear in the logs and configured notification channel."
        try:
            Thread(target=self._run, name="ComparisonPreview", daemon=True).start()
        except Exception:
            self._finish(False)
            raise
        return True

    def _run(self):
        success = False
        try:
            success = BotOrchestrator().run_comparison_preview()
        except Exception as exc:
            log_failure(logger, "PREVIEW worker failed", exc)
        finally:
            self._finish(success)

    def _finish(self, success):
        with self._lock:
            self._state = "complete" if success else "failed"
            self._message = (
                "Preview complete. View the results in Logs or your configured notification channel."
                if success else "Preview failed. See Logs for diagnostics; you can try again."
            )
