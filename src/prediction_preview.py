"""Shared manual/scheduled prediction worker with non-blocking publication retries."""

import logging
from datetime import UTC, datetime, timedelta
from threading import Lock, Thread

import config
from diagnostics import log_failure
from notification_service import NotificationService
from prediction_engine import LONDON, PredictionEngine, PredictionError, RatesNotPublished, day_bounds
from query_service import QueryService

logger = logging.getLogger("octobot.prediction_preview")
RETRY_DELAY = timedelta(minutes=30)


class PredictionPreview:
    def __init__(self):
        self._lock = Lock()
        self._state = "idle"
        self._message = "Ready to predict tomorrow using the four most recent complete matching weekdays."
        self._result = None
        self._target_date = None
        self._scheduled_target_date = None
        self._next_retry = None
        self._last_schedule_date = None

    def status(self):
        with self._lock:
            return {"running": self._state == "running", "state": self._state,
                    "message": self._message, "result": self._result,
                    "target_date": self._target_date.isoformat() if self._target_date else None,
                    "next_retry": self._next_retry.isoformat()
                    if self._next_retry and self._state == "waiting" else None}

    def start(self, target_date=None, *, scheduled=False):
        target_date = target_date or (datetime.now(LONDON).date() + timedelta(days=1))
        with self._lock:
            if self._state == "running":
                return False
            self._state = "running"
            self._target_date = target_date
            if scheduled:
                self._scheduled_target_date = target_date
                self._next_retry = None
            self._result = None
            self._message = "Prediction running. Results will appear in the logs and configured notification channel."
        # Each run uses a private service and tariff objects. No account manager,
        # browser login or switching path is involved.
        settings = (config.API_KEY, config.BASE_URL, config.ACC_NUMBER, config.PREDICTION_TARIFFS)
        try:
            Thread(target=self._run, args=(target_date, settings, scheduled),
                   name="PredictionPreview", daemon=True).start()
        except Exception:
            with self._lock:
                self._state = "failed"
                self._message = "Could not start prediction. See Logs; you can try again."
            raise
        return True

    def _run(self, target_date, settings, scheduled=False):
        notifications = NotificationService(config.NOTIFICATION_URLS, config.BATCH_NOTIFICATIONS)
        try:
            api_key, base_url, account_number, tariff_ids = settings
            result = PredictionEngine(QueryService(api_key, base_url)).predict(account_number, target_date, tariff_ids)
            payload, summary = result.as_dict(), result.summary()
        except RatesNotPublished as exc:
            retry = datetime.now(UTC) + RETRY_DELAY if scheduled else None
            message = f"PREDICTION for {target_date}: {exc} "
            message += (f"Retrying at {retry.astimezone(LONDON):%H:%M %Z} (every 30 minutes)."
                        if scheduled else "One-time check complete. Try again later or wait for the scheduled prediction.")
            with self._lock:
                self._state, self._message = "waiting" if scheduled else "unavailable", message
                if scheduled:
                    self._scheduled_target_date, self._next_retry = target_date, retry
            logger.info("%s", message)
        except Exception as exc:
            log_failure(logger, "PREDICTION failed", exc)
            message = (f"Prediction failed: {exc}" if isinstance(exc, PredictionError)
                       else "Prediction failed. See Logs for diagnostics; you can try again.")
            with self._lock:
                self._state, self._message = "failed", message
            notifications.send_notification(message, title="Octobot prediction", is_error=True, batchable=False)
        else:
            with self._lock:
                self._state = "complete"
                self._result = payload
                if scheduled and self._scheduled_target_date == target_date:
                    self._scheduled_target_date, self._next_retry = None, None
                self._message = "Prediction complete. View the results in Logs or your configured notification channel."
                if payload["incomplete_source_days"]:
                    self._message += (" Searched older matching weekdays because some source days were incomplete. "
                                      "Source days: " + ", ".join(payload["source_dates"]) + ".")
                if payload["uses_partial_history"] or payload["history_sample_min"] < 4:
                    self._message += (f" Limited history: {payload['history_sample_min']}–{payload['history_sample_max']} "
                                      "matching days per half hour; used available readings within three months.")
                if not payload["is_full_day"]:
                    self._message += (f" Partial-day comparison: {payload['slot_count']} of {payload['day_slot_count']} "
                                      "half hours (00:00–23:00 Europe/London). The unpublished final hour is "
                                      "excluded from every tariff; includes one full daily standing charge.")
            notifications.send_notification(summary, title="Octobot prediction", batchable=False)

    def tick(self, now=None):
        """Called every 30 seconds independently of the tariff-switch scheduler."""
        now = now or datetime.now(UTC)
        local = now.astimezone(LONDON)
        with self._lock:
            if self._state == "running":
                return
            target = self._scheduled_target_date
            if target and now >= day_bounds(target)[1]:
                self._scheduled_target_date, self._next_retry = None, None
                if self._state == "waiting":
                    self._state = "expired"
                    self._message = f"Prediction for {target} expired without a complete set of Agile rates."
                target = None
            scheduled = (not config.TEST_PLAYWRIGHT and not config.ONE_OFF_RUN
                         and local.strftime("%H:%M") >= config.PREDICTION_TIME
                         and self._last_schedule_date != local.date())
            retry_due = self._next_retry is not None and now >= self._next_retry
            if scheduled:
                self._last_schedule_date = local.date()
                tomorrow = local.date() + timedelta(days=1)
                if target == tomorrow and self._next_retry is not None:
                    # Preserve the existing scheduled retry cadence, even when
                    # a one-time GUI check has since updated the visible status.
                    if not retry_due:
                        return
                else:
                    target = tomorrow
            elif not retry_due:
                return
        self.start(target, scheduled=True)


def start_prediction_scheduler(prediction):
    def run():
        import time
        while True:
            try:
                prediction.tick()
            except Exception as exc:
                log_failure(logger, "Prediction scheduler failed", exc)
            time.sleep(30)

    thread = Thread(target=run, name="PredictionScheduler", daemon=True)
    thread.start()
    return thread
