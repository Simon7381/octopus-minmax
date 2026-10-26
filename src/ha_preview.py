"""Receive Home Assistant Supervisor button commands over add-on stdin."""

import json
import logging
import sys
from threading import Thread

from diagnostics import log_failure

logger = logging.getLogger("octobot.ha_preview")
PREVIEW_COMMAND = {"command": "comparison_preview"}
MAX_COMMAND_LENGTH = 4096


def listen_for_preview_commands(preview, stream, prediction=None):
    """Accept only the comparison-only command; never execute arbitrary input."""
    logger.info("Home Assistant comparison preview command listener ready.")
    try:
        # Supervisor appends a newline to each app_stdin/addon_stdin payload.
        while line := stream.readline(MAX_COMMAND_LENGTH + 1):
            if len(line) > MAX_COMMAND_LENGTH:
                # Drain the rest of this one command, retaining the next line.
                while not line.endswith("\n"):
                    line = stream.readline(MAX_COMMAND_LENGTH + 1)
                    if not line:
                        break
                logger.warning("Ignored oversized Home Assistant preview command.")
                continue
            if not line.strip():
                continue
            try:
                command = json.loads(line)
            except (ValueError, RecursionError):
                logger.warning("Ignored invalid Home Assistant preview command.")
                continue
            is_prediction = command == {"command": "prediction_preview"} and prediction is not None
            if command != PREVIEW_COMMAND and not is_prediction:
                logger.warning("Ignored unsupported Home Assistant preview command.")
                continue
            try:
                worker = prediction if is_prediction else preview
                label = "prediction" if is_prediction else "comparison preview"
                if worker.start():
                    logger.info("Home Assistant button started a %s; no tariff changes.", label)
                else:
                    logger.info("Home Assistant button: a %s is already running.", label)
            except Exception as exc:
                log_failure(logger, "Home Assistant could not start comparison preview", exc)
    except (OSError, ValueError) as exc:
        log_failure(logger, "Home Assistant preview input closed", exc)
    logger.debug("Home Assistant preview command listener stopped.")


def start_preview_listener(preview, prediction=None):
    """A closed stdin outside HA exits quietly without affecting the bot."""
    thread = Thread(target=listen_for_preview_commands, args=(preview, sys.stdin, prediction),
                    daemon=True, name="HomeAssistantPreview")
    thread.start()
    return thread
