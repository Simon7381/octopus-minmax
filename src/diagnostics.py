"""Useful exception diagnostics without dumping browser call logs or API payloads."""

import re
import traceback


def error_summary(exc: Exception) -> str:
    summary = type(exc).__name__
    message = str(exc)
    # Playwright messages can include filled credentials, URLs and page contents.
    # Only extract an operation name and classify known errors; never log the body.
    operation = re.match(r"^([A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*):", message)
    if operation:
        summary += f" in {operation[1]}"
    if any(marker in message.lower() for marker in (
        "failed to find execution context", "execution context was destroyed",
        "cannot find context with specified id",
    )):
        summary += " (browser execution context unavailable; page may be navigating)"
    timeout = re.search(r"\btimeout\s+(\d{1,9})\s*ms\s+exceeded\b", message, re.IGNORECASE)
    if timeout:
        summary += f" (timed out after {timeout[1]} ms)"
    elif "timeout" in type(exc).__name__.lower() or "timed out" in message.lower():
        summary += " (browser operation timed out)"
    if any(marker in message.lower() for marker in (
        "target page, context or browser has been closed", "browser has been closed",
        "page has been closed", "page crashed", "browser closed",
    )):
        summary += " (browser or page closed/crashed)"
    if "failed to create new page" in message.lower():
        summary += " (browser could not create a page)"
    return summary


def log_failure(logger, stage: str, exc: Exception) -> None:
    logger.error("%s: %s", stage, error_summary(exc))
    # Stack locations identify the failed call without exposing exception text,
    # source lines, local variables, tokens or credentials.
    frames = traceback.extract_tb(exc.__traceback__)
    logger.debug("%s stack locations: %s", stage, " -> ".join(
        f"{frame.name}:{frame.lineno}" for frame in frames
    ))
