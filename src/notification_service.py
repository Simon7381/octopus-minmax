import logging
from datetime import datetime
from pathlib import Path

from apprise import Apprise

import config

logger = logging.getLogger("octobot.notification_service")
DISCORD_CHAR_LIMIT = 1900  # Technically it's 2000 but the "title" and backticks are part of it so this is safer in case the formatting changes


class NotificationService:
    def __init__(self, notification_urls: str, batch_enabled: bool):
        self.notification_urls = notification_urls
        self.batch_notifications: list[str] = []
        self._batch_has_debug_errors = False
        self.batch_enabled = batch_enabled
        self._apprise: Apprise | None = None

    def _refresh_from_config(self) -> None:
        if self.notification_urls != config.NOTIFICATION_URLS:
            self.notification_urls = config.NOTIFICATION_URLS
            self._apprise = None
        if self.batch_enabled != config.BATCH_NOTIFICATIONS:
            self.batch_enabled = config.BATCH_NOTIFICATIONS

    def _get_apprise(self) -> Apprise | None:
        if self._apprise is None:
            self._apprise = Apprise()
            for url in self.notification_urls.split(","):
                self._apprise.add(url.strip())

        return self._apprise

    def _diagnostic_attachments(self) -> list[str]:
        """Collect readable diagnostics without traversing the browser profile."""
        if not config.DEBUG:
            return []
        attachments = []
        try:
            candidates = sorted(Path("logs").iterdir())
        except FileNotFoundError:
            return []
        except OSError:
            logger.warning("Could not list diagnostic attachments in logs")
            return []
        for path in candidates:
            if path.name != "octobot.log" and path.suffix.lower() != ".png":
                continue
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                with path.open("rb"):
                    pass
                attachments.append(str(path.absolute()))
            except OSError:
                logger.warning("Could not read diagnostic attachment: %s", path.name)
        return attachments

    def send_notification(
        self,
        message: str,
        title: str = "",
        is_error: bool = False,
        batchable: bool = True,
    ) -> bool:
        """Sends a notification using Apprise.

        Args:
            message (str): The message to send.
            title (str, optional): The title of the notification.
            is_error (bool, optional): Format as an error and attach available logs/PNGs
                when DEBUG is enabled. Defaults to False.
            batchable (bool, optional): Whether the message can be batched.
        """
        self._refresh_from_config()
        logger.log(logging.ERROR if is_error else logging.INFO, "%s%s",
                   f"{title}: " if title else "", message)
        apprise = self._get_apprise()

        messages = [message]
        if is_error:
            messages = [
                f"```py\n{message[offset:offset + DISCORD_CHAR_LIMIT]}\n```"
                for offset in range(0, max(1, len(message)), DISCORD_CHAR_LIMIT)
            ]

        if self.batch_enabled and batchable:
            self.batch_notifications.extend(messages)
            self._batch_has_debug_errors |= is_error and config.DEBUG
            logger.debug(
                f"Added message to batch. Current batch size: {len(self.batch_notifications)}"
            )
            return True
        else:
            if not apprise:
                logger.warning(
                    "No notification services configured. Check config.NOTIFICATION_URLS."
                )
                return False
            attachments = self._diagnostic_attachments() if is_error else []
            success = True
            for index, part in enumerate(messages):
                # Upload diagnostics once, alongside the final chunk of an error.
                last_part = index == len(messages) - 1
                options = {"attach": attachments} if last_part and attachments else {}
                delivered = apprise.notify(
                    body=part, title=title if last_part else "", **options
                )
                success = bool(delivered) and success
            if not success:
                logger.error(f"Failed to send notification: {title}")
            else:
                logger.debug("Notification delivered successfully.")
            return success

    def send_batch_notification(self) -> bool:
        self._refresh_from_config()
        if not self.batch_notifications:
            logger.debug("No notifications in batch to send")
            return True

        apprise = self._get_apprise()

        now = datetime.now()
        title = now.strftime(
            f"Octopus MinMax Results - %a %d %b {config.EXECUTION_TIME if not config.ONE_OFF_RUN else now.strftime('%H:%M:%S')}"
        )
        body = "\n".join(self.batch_notifications)

        if not apprise:
            logger.warning(
                "Cannot send batch - no notification services configured. Check config.NOTIFICATION_URLS."
            )
            logger.info(body)
            return False
        attachments = self._diagnostic_attachments() if self._batch_has_debug_errors else []
        options = {"attach": attachments} if attachments else {}
        success = apprise.notify(body=body, title=title, **options)
        if success:
            logger.info(
                f"Sent batch notification with {len(self.batch_notifications)} messages"
            )
            self.batch_notifications.clear()
            self._batch_has_debug_errors = False
        else:
            logger.error("Failed to send batch notification")

        return success
