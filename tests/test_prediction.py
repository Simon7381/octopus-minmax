import io
import sys
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config
import config_manager
import ha_preview
import web_server
from prediction_engine import (HALF_HOUR, LONDON, PredictionEngine, PredictionError, RatesNotPublished,
                               day_bounds, day_slots, prediction_tariffs)
from prediction_preview import PredictionPreview
from query_service import QueryService


TARGET = date(2026, 10, 27)


def rate(start, end, value, payment=None):
    return {"valid_from": start.isoformat(), "valid_to": end.isoformat() if end else None,
            "value_inc_vat": value, "payment_method": payment}


class FixtureAPI:
    base_url = "https://api.example.invalid/v1"

    def __init__(self, target=TARGET):
        self.target = target
        self.requests = []
        self.missing_rate = False
        self.unpublished_final_hour = False
        self.gap = False
        self.bad_consumption = None
        self.paginate = False
        self.account = {"properties": [{"moved_out_at": None, "electricity_meter_points": [{
            "mpan": "12345", "is_export": False, "meters": [{"serial_number": "meter"}],
            "agreements": [{"tariff_code": "E-1R-AGILE-TEST-A", "valid_from": "2020-01-01T00:00:00Z",
                            "valid_to": None}],
        }]}]}

    def execute_rest_query(self, url, *, authenticated=False):
        self.requests.append((url, authenticated))
        parsed, params = urlsplit(url), parse_qs(urlsplit(url).query)
        path = parsed.path
        if "/accounts/" in path:
            assert authenticated
            return self.account
        if path == "/v1/products/":
            return {"results": [{"code": identifier.upper(), "display_name": display, "direction": "IMPORT",
                                 "available_from": "2020-01-01T00:00:00Z"}
                                for identifier, display in (("agile", "Agile Octopus"), ("cosy", "Cosy Octopus"))]}
        if "/consumption/" in path:
            assert authenticated
            start = datetime.fromisoformat(params["period_from"][0])
            end = datetime.fromisoformat(params["period_to"][0])
            week = (self.target - (start + timedelta(hours=1)).date()).days // 7
            readings = []
            while start < end:
                readings.append({"interval_start": start.isoformat(), "interval_end": (start + HALF_HOUR).isoformat(),
                                 "consumption": self.bad_consumption if self.bad_consumption is not None else week})
                start += HALF_HOUR
            if self.gap:
                readings.pop()
            if self.paginate:
                if params.get("page") == ["2"]:
                    return {"results": readings[20:]}
                return {"results": readings[:20], "next": url + "&page=2"}
            return {"results": readings}
        if path.endswith("/products/AGILE/") or path.endswith("/products/COSY/"):
            identifier = path.split("/")[-2]
            prefix = self.base_url + "/" + identifier
            return {"single_register_electricity_tariffs": {"_A": {"direct_debit_monthly": {"links": [
                {"rel": "standard_unit_rates", "href": prefix + "/units/"},
                {"rel": "standing_charges", "href": prefix + "/standing/"},
            ]}}}}
        start, end = day_bounds(self.target)
        if path.endswith("/standing/"):
            # Prove NON_DIRECT_DEBIT values are ignored.
            return {"results": [rate(start, end, 40), rate(start, end, 999, "NON_DIRECT_DEBIT")]}
        if path.endswith("/units/"):
            if "/AGILE/" in path:
                slots = day_slots(self.target)
                values = [rate(slot, slot + HALF_HOUR, -2 if index == 0 else 10)
                          for index, slot in enumerate(slots)]
                if self.unpublished_final_hour:
                    values = values[:-2]
                if self.missing_rate:
                    values.pop()
                return {"results": values}
            # Higher Cosy rate begins at a half-hour boundary. Half-open
            # interval matching must not accidentally use the old price.
            return {"results": [rate(start, start + HALF_HOUR, 5), rate(start + HALF_HOUR, end, 20)]}
        raise AssertionError("Unexpected fixture URL: " + url)


class PredictionEngineTests(unittest.TestCase):
    def test_four_weekday_slot_means_are_priced_identically_across_tariffs(self):
        api = FixtureAPI()
        result = PredictionEngine(api).predict("A-TEST", TARGET, "cosy,agile")
        self.assertEqual(result.source_dates, [TARGET - timedelta(weeks=week) for week in range(1, 5)])
        self.assertEqual(len(result.profile), 48)
        self.assertTrue(all(slot["kwh"] == 2.5 for slot in result.profile))
        by_id = {entry.tariff.id: entry.cost_breakdown for entry in result.comparisons}
        self.assertEqual(by_id["agile"].total_kwh, 120)
        self.assertEqual(by_id["agile"].total_cost, 2.5 * (-2 + 47 * 10) + 40)
        self.assertEqual(by_id["cosy"].total_cost, 2.5 * (5 + 47 * 20) + 40)
        payload = result.as_dict()
        self.assertEqual(payload["best_tariff"], "Agile Octopus")
        self.assertEqual(payload["slots"][0]["rates_pence"], {"agile": -2, "cosy": 5})
        self.assertEqual(payload["slots"][1]["rates_pence"]["cosy"], 20)
        self.assertIn("Estimated cheapest", result.summary())
        self.assertTrue(payload["is_full_day"])
        self.assertEqual(payload["period_to"], "2026-10-28T00:00:00+00:00")
        self.assertTrue(all("period_from=" in url for url, _ in api.requests if "/consumption/" in url))

    def test_each_half_hour_uses_its_own_history_instead_of_daily_total(self):
        api = FixtureAPI()
        original = api.execute_rest_query

        def query(url, **kwargs):
            response = original(url, **kwargs)
            if "/consumption/" in url:
                for index, reading in enumerate(response["results"]):
                    reading["consumption"] *= index + 1
            return response

        api.execute_rest_query = query
        result = PredictionEngine(api).predict("A-TEST", TARGET, "agile,cosy")
        self.assertEqual([slot["kwh"] for slot in result.profile], [2.5 * index for index in range(1, 49)])

    def test_missing_agile_slot_requests_retry_before_history_is_downloaded(self):
        api = FixtureAPI()
        api.missing_rate = True
        with self.assertRaises(RatesNotPublished):
            PredictionEngine(api).predict("A-TEST", TARGET, "agile,cosy")
        self.assertFalse(any("/consumption/" in url or "/COSY/" in url for url, _ in api.requests))

    def test_published_46_slot_day_compares_identical_periods_and_reports_coverage(self):
        api = FixtureAPI()
        api.unpublished_final_hour = True
        result = PredictionEngine(api).predict("A-TEST", TARGET, "agile,cosy")
        payload = result.as_dict()
        self.assertEqual(payload["slot_count"], 46)
        self.assertEqual(payload["day_slot_count"], 48)
        self.assertFalse(payload["is_full_day"])
        self.assertEqual(payload["period_from"], "2026-10-27T00:00:00+00:00")
        self.assertEqual(payload["period_to"], "2026-10-27T23:00:00+00:00")
        self.assertEqual(payload["total_kwh"], 115)
        by_id = {entry.tariff.id: entry.cost_breakdown for entry in result.comparisons}
        self.assertEqual(by_id["agile"].total_cost, 2.5 * (-2 + 45 * 10) + 40)
        self.assertEqual(by_id["cosy"].total_cost, 2.5 * (5 + 45 * 20) + 40)
        self.assertIn("Partial-day estimate: 46 of 48", result.summary())
        self.assertIn("final hour has no published Agile rates", result.summary())
        self.assertIn("one full daily standing charge", result.summary())

    def test_gaps_or_shorter_publication_are_not_accepted_as_complete(self):
        for unpublished_final_hour, removed in ((False, (0, 1)), (False, (20, 21)),
                                                (True, (20,)), (True, (44, 45))):
            with self.subTest(unpublished_final_hour=unpublished_final_hour, removed=removed):
                api = FixtureAPI()
                api.unpublished_final_hour = unpublished_final_hour
                original = api.execute_rest_query

                def query(url, **kwargs):
                    response = original(url, **kwargs)
                    if "/AGILE/units/" in url:
                        response["results"] = [entry for index, entry in enumerate(response["results"])
                                               if index not in removed]
                    return response

                api.execute_rest_query = query
                with self.assertRaisesRegex(RatesNotPublished, "of 46 half hours before 23:00"):
                    PredictionEngine(api).predict("A-TEST", TARGET, "agile,cosy")
                self.assertFalse(any("/consumption/" in url for url, _ in api.requests))

    def test_unpublished_final_hour_respects_dst_and_summer_time(self):
        for target, count in ((date(2026, 3, 29), 44), (date(2026, 10, 25), 48),
                              (date(2026, 7, 21), 46)):
            with self.subTest(target=target):
                api = FixtureAPI(target)
                api.unpublished_final_hour = True
                result = PredictionEngine(api).predict("A-TEST", target, "agile,cosy")
                self.assertEqual(len(result.profile), count)
                self.assertEqual(datetime.fromisoformat(result.as_dict()["period_to"]).hour, 23)
                self.assertEqual(result.as_dict()["day_slot_count"], count + 2)

    def test_other_tariffs_only_need_unit_rates_for_the_compared_period(self):
        api = FixtureAPI()
        api.unpublished_final_hour = True
        original = api.execute_rest_query

        def query(url, **kwargs):
            response = original(url, **kwargs)
            if "/COSY/units/" in url:
                response["results"][-1]["valid_to"] = day_slots(TARGET)[-2].isoformat()
            return response

        api.execute_rest_query = query
        result = PredictionEngine(api).predict("A-TEST", TARGET, "agile,cosy")
        self.assertEqual(len(result.profile), 46)

    def test_incomplete_history_is_not_treated_as_zero_usage(self):
        api = FixtureAPI()
        api.gap = True
        with self.assertRaisesRegex(PredictionError, "47 of 48"):
            PredictionEngine(api).predict("A-TEST", TARGET, "agile,cosy")

    def test_invalid_consumption_is_rejected(self):
        for value in (-1, float("nan"), float("inf"), "bad"):
            with self.subTest(value=value), self.assertRaises(PredictionError):
                api = FixtureAPI()
                api.bad_consumption = value
                PredictionEngine(api).predict("A-TEST", TARGET, "agile,cosy")

    def test_paginated_history_is_fully_retrieved(self):
        api = FixtureAPI()
        api.paginate = True
        result = PredictionEngine(api).predict("A-TEST", TARGET, "agile,cosy")
        self.assertEqual(result.as_dict()["total_kwh"], 120)
        self.assertEqual(sum("page=2" in url for url, _ in api.requests), 4)

    def test_all_prices_require_full_interval_coverage_and_half_open_boundaries(self):
        start, end = day_bounds(TARGET)
        rates = [rate(start, start + HALF_HOUR, 5), rate(start + HALF_HOUR, end, 20)]
        self.assertEqual(PredictionEngine._rate_at(rates, start + HALF_HOUR, start + 2 * HALF_HOUR), 20)
        self.assertIsNone(PredictionEngine._rate_at(rates, end, end + HALF_HOUR))
        self.assertIsNone(PredictionEngine._rate_at([rate(start, start + timedelta(minutes=15), 5)], start, start + HALF_HOUR))

    def test_dst_days_have_46_or_50_slots_and_local_source_slot_mapping(self):
        for target, count in ((date(2026, 3, 29), 46), (date(2026, 10, 25), 50),
                              (date(2026, 4, 5), 48), (date(2026, 11, 1), 48)):
            with self.subTest(target=target):
                result = PredictionEngine(FixtureAPI(target)).predict("A-TEST", target, "agile,cosy")
                self.assertEqual(len(result.profile), count)
                for slot in result.profile:
                    local_hour = slot["start"].astimezone(LONDON).hour
                    expected = 3 if target == date(2026, 4, 5) and local_hour == 1 else 2.5
                    self.assertEqual(slot["kwh"], expected)
                self.assertEqual(len({slot["start"] for slot in result.profile}), count)

    def test_repeated_historical_autumn_hour_has_equal_day_weight(self):
        target = date(2026, 11, 1)
        api = FixtureAPI(target)
        original = api.execute_rest_query

        def query(url, **kwargs):
            response = original(url, **kwargs)
            if "/consumption/" in url:
                for reading in response["results"]:
                    local = datetime.fromisoformat(reading["interval_start"]).astimezone(LONDON)
                    if local.fold == 1:
                        reading["consumption"] = 5
            return response

        api.execute_rest_query = query
        result = PredictionEngine(api).predict("A-TEST", target, "agile,cosy")
        for slot in result.profile:
            expected = 3 if slot["start"].astimezone(LONDON).hour == 1 else 2.5
            self.assertEqual(slot["kwh"], expected)

    def test_export_and_previous_properties_are_excluded_and_ambiguous_import_is_rejected(self):
        api = FixtureAPI()
        point = api.account["properties"][0]["electricity_meter_points"][0]
        api.account["properties"].append({"moved_out_at": "2021-01-01T00:00:00Z", "electricity_meter_points": [point]})
        api.account["properties"][0]["electricity_meter_points"].append({"is_export": True})
        PredictionEngine(api).predict("A-TEST", TARGET, "agile,cosy")
        api.account["properties"][0]["electricity_meter_points"].append(point)
        with self.assertRaisesRegex(PredictionError, "one identifiable"):
            PredictionEngine(api).predict("A-TEST", TARGET, "agile,cosy")

    def test_tariff_validation_requires_agile_and_is_independent_of_switching_config(self):
        for value in ("", "cosy", "agile,unknown"):
            with self.subTest(value=value), self.assertRaises(PredictionError):
                prediction_tariffs(value)
        self.assertEqual([tariff.id for tariff in prediction_tariffs(" COSY, Agile,agile ")], ["cosy", "agile"])
        self.assertTrue(config_manager.validate_config({"prediction_time": "25:00"}))
        self.assertTrue(config_manager.validate_config({"prediction_tariffs": "cosy"}))
        self.assertFalse(config_manager.validate_config({"prediction_time": "16:00", "prediction_tariffs": "agile,cosy"}))


class PredictionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.worker = PredictionPreview()
        settings = patch.dict(config.__dict__, {"ONE_OFF_RUN": False, "TEST_PLAYWRIGHT": False,
                                             "PREDICTION_TIME": "21:00", "PREDICTION_TARIFFS": "agile,cosy"})
        settings.start()
        self.addCleanup(settings.stop)

    def test_schedule_runs_once_at_uk_time_with_catch_up_and_does_not_overlap(self):
        with patch("prediction_preview.Thread") as thread:
            self.worker.tick(datetime(2026, 7, 20, 19, 59, tzinfo=UTC))
            thread.assert_not_called()
            self.worker.tick(datetime(2026, 7, 20, 20, 2, tzinfo=UTC))
            self.assertTrue(self.worker.status()["running"])
            self.assertEqual(self.worker.status()["target_date"], "2026-07-21")
            self.worker.tick(datetime(2026, 7, 20, 20, 30, tzinfo=UTC))
            self.assertFalse(self.worker.start())
            thread.return_value.start.assert_called_once()
            self.worker._state = "complete"
            self.worker.tick(datetime(2026, 7, 20, 22, tzinfo=UTC))
            thread.return_value.start.assert_called_once()

    def test_missing_rates_retry_after_exactly_30_minutes_for_original_date(self):
        now = datetime(2026, 10, 26, 21, tzinfo=UTC)
        with patch("prediction_preview.QueryService"), patch("prediction_preview.PredictionEngine") as engine, \
                patch("prediction_preview.NotificationService") as ns, patch("prediction_preview.datetime") as clock:
            engine.return_value.predict.side_effect = RatesNotPublished("Rates missing")
            clock.now.return_value = now
            self.worker._target_date = TARGET
            self.worker._run(TARGET, ("key", "url", "account", "agile,cosy"), scheduled=True)
        self.assertEqual(self.worker.status()["state"], "waiting")
        self.assertEqual(self.worker.status()["next_retry"], (now + timedelta(minutes=30)).isoformat())
        ns.return_value.send_notification.assert_not_called()
        with patch("prediction_preview.Thread") as thread:
            self.worker.tick(now + timedelta(minutes=29))
            thread.assert_not_called()
            self.worker.tick(now + timedelta(minutes=30))
            self.assertEqual(thread.call_args.kwargs["args"][0], TARGET)

    def test_waiting_target_does_not_roll_forward_at_midnight(self):
        self.worker._state = "waiting"
        self.worker._target_date = TARGET
        self.worker._scheduled_target_date = TARGET
        self.worker._next_retry = datetime(2026, 10, 27, 0, 10, tzinfo=UTC)
        with patch("prediction_preview.Thread") as thread:
            self.worker.tick(datetime(2026, 10, 27, 0, 10, tzinfo=UTC))
            self.assertEqual(thread.call_args.kwargs["args"][0], TARGET)

    def test_completed_manual_prediction_does_not_skip_the_scheduled_refresh(self):
        self.worker._state = "complete"
        self.worker._target_date = TARGET
        with patch("prediction_preview.Thread") as thread:
            self.worker.tick(datetime(2026, 10, 26, 21, tzinfo=UTC))
            thread.return_value.start.assert_called_once()
            self.assertEqual(thread.call_args.kwargs["args"][0], TARGET)

    def test_gui_shows_pending_rates_then_retry_sends_completed_notification(self):
        result = PredictionEngine(FixtureAPI()).predict("A-TEST", TARGET, "agile,cosy")
        settings = ("key", "url", "account", "agile,cosy")
        now = datetime(2026, 10, 26, 21, tzinfo=UTC)
        self.worker._target_date = TARGET
        with patch("prediction_preview.QueryService"), patch("prediction_preview.PredictionEngine") as engine, \
                patch("prediction_preview.NotificationService") as ns, \
                patch("prediction_preview.datetime") as clock, \
                patch.object(web_server, "prediction_preview", self.worker):
            engine.return_value.predict.side_effect = [
                RatesNotPublished("Agile rates are not yet fully available (20 of 48 half hours)."), result,
            ]
            clock.now.return_value = now
            self.worker._run(TARGET, settings, scheduled=True)
            client = web_server.app.test_client()
            headers = {"X-Ingress-Path": "/api/hassio_ingress/test"}
            status = client.get("/prediction-status", headers=headers).json
            self.assertEqual(status["state"], "waiting")
            self.assertIn("20 of 48", status["message"])
            self.assertIn(b"not yet fully available", client.get("/", headers=headers).data)
            ns.return_value.send_notification.assert_not_called()
            with patch("prediction_preview.Thread") as thread:
                thread.return_value.start.side_effect = lambda: self.worker._run(TARGET, settings, scheduled=True)
                self.worker.tick(now + timedelta(minutes=30))
            self.assertEqual(client.get("/prediction-status", headers=headers).json["state"], "complete")
            ns.return_value.send_notification.assert_called_once()
            call = ns.return_value.send_notification.call_args
            self.assertIn("Estimated cheapest: Agile Octopus", call.args[0])
            self.assertFalse(call.kwargs["batchable"])

    def test_manual_unavailable_result_never_starts_publication_retries(self):
        now = datetime(2026, 10, 26, 15, tzinfo=UTC)
        with patch("prediction_preview.QueryService"), patch("prediction_preview.PredictionEngine") as engine, \
                patch("prediction_preview.NotificationService") as ns:
            engine.return_value.predict.side_effect = RatesNotPublished("Rates not yet available (0 of 48 half hours).")
            self.worker._run(TARGET, ("key", "url", "account", "agile,cosy"))
        self.assertEqual(self.worker.status()["state"], "unavailable")
        self.assertIsNone(self.worker.status()["next_retry"])
        ns.return_value.send_notification.assert_not_called()
        with patch("prediction_preview.Thread") as thread:
            self.worker.tick(now + timedelta(minutes=30))
            self.worker.tick(now + timedelta(hours=1))
            thread.assert_not_called()
            self.worker.tick(now.replace(hour=21))
            thread.return_value.start.assert_called_once()
            self.assertTrue(thread.call_args.kwargs["args"][2])

    def test_manual_unavailable_check_preserves_existing_scheduled_retry(self):
        now = datetime(2026, 10, 26, 21, tzinfo=UTC)
        self.worker._scheduled_target_date = TARGET
        self.worker._next_retry = now + timedelta(minutes=30)
        self.worker._last_schedule_date = now.date()
        with patch("prediction_preview.QueryService"), patch("prediction_preview.PredictionEngine") as engine, \
                patch("prediction_preview.NotificationService"):
            engine.return_value.predict.side_effect = RatesNotPublished("Rates missing")
            self.worker._run(TARGET, ("key", "url", "account", "agile,cosy"))
        self.assertEqual(self.worker._next_retry, now + timedelta(minutes=30))
        self.assertIsNone(self.worker.status()["next_retry"])
        with patch("prediction_preview.Thread") as thread:
            self.worker.tick(now + timedelta(minutes=29))
            thread.assert_not_called()
            self.worker.tick(now + timedelta(minutes=30))
            self.assertEqual(thread.call_args.kwargs["args"][0], TARGET)
            self.assertTrue(thread.call_args.kwargs["args"][2])

    def test_one_off_and_browser_test_modes_do_not_start_daily_predictions(self):
        for mode in ("ONE_OFF_RUN", "TEST_PLAYWRIGHT"):
            with self.subTest(mode=mode), patch.object(config, mode, True), patch("prediction_preview.Thread") as thread:
                self.worker.tick(datetime(2026, 10, 26, 22, tzinfo=UTC))
                thread.assert_not_called()

    def test_success_is_reported_with_results_without_account_manager_or_browser(self):
        result = PredictionEngine(FixtureAPI()).predict("A-TEST", TARGET, "agile,cosy")
        with patch("prediction_preview.QueryService"), patch("prediction_preview.PredictionEngine") as engine, \
                patch("prediction_preview.NotificationService") as ns, \
                patch("account_manager.AccountManager") as account, patch("browser_switch.logged_in_page") as browser:
            engine.return_value.predict.return_value = result
            self.worker._run(TARGET, ("key", "url", "account", "agile,cosy"))
            account.assert_not_called()
            browser.assert_not_called()
        self.assertEqual(self.worker.status()["state"], "complete")
        self.assertEqual(self.worker.status()["result"]["total_kwh"], 120)
        self.assertIn("PREDICTION", ns.return_value.send_notification.call_args.args[0])
        self.assertFalse(ns.return_value.send_notification.call_args.kwargs["batchable"])

    def test_partial_day_success_reports_coverage_in_dashboard_and_notification(self):
        api = FixtureAPI()
        api.unpublished_final_hour = True
        result = PredictionEngine(api).predict("A-TEST", TARGET, "agile,cosy")
        with patch("prediction_preview.QueryService"), patch("prediction_preview.PredictionEngine") as engine, \
                patch("prediction_preview.NotificationService") as ns, \
                patch.object(web_server, "prediction_preview", self.worker):
            engine.return_value.predict.return_value = result
            self.worker._scheduled_target_date = TARGET
            self.worker._next_retry = datetime(2026, 10, 26, 21, 30, tzinfo=UTC)
            self.worker._run(TARGET, ("key", "url", "account", "agile,cosy"), scheduled=True)
            client = web_server.app.test_client()
            headers = {"X-Ingress-Path": "/api/hassio_ingress/test"}
            status = client.get("/prediction-status", headers=headers).json
            self.assertEqual(status["state"], "complete")
            self.assertIsNone(self.worker._next_retry)
            self.assertFalse(status["result"]["is_full_day"])
            self.assertIn("46 of 48", status["message"])
            self.assertIn(b"unpublished final hour", client.get("/", headers=headers).data)
            ns.return_value.send_notification.assert_called_once()
            self.assertIn("Partial-day estimate: 46 of 48", ns.return_value.send_notification.call_args.args[0])

    def test_failures_are_visible_and_raw_api_errors_are_not_exposed(self):
        for error, expected in ((RuntimeError("private-api-key"), "See Logs"),
                                (PredictionError("Incomplete consumption"), "Incomplete consumption")):
            with self.subTest(error=error), patch("prediction_preview.QueryService", side_effect=error), \
                    patch("prediction_preview.NotificationService") as ns:
                self.worker._run(TARGET, ("key", "url", "account", "agile,cosy"))
            self.assertEqual(self.worker.status()["state"], "failed")
            self.assertIn(expected, self.worker.status()["message"])
            self.assertNotIn("private-api-key", str(self.worker.status()) + str(ns.mock_calls))

    def test_thread_failure_allows_manual_retry(self):
        with patch("prediction_preview.Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("Thread unavailable")
            with self.assertRaises(RuntimeError):
                self.worker.start()
            self.assertEqual(self.worker.status()["state"], "failed")
            thread.return_value.start.side_effect = None
            self.assertTrue(self.worker.start())


class PredictionRoutesTests(unittest.TestCase):
    def setUp(self):
        self.client = web_server.app.test_client()
        self.headers = {"X-Ingress-Path": "/api/hassio_ingress/test"}

    def test_authenticated_post_and_token_are_required(self):
        with patch.object(web_server.prediction_preview, "start") as start:
            self.assertEqual(self.client.post("/prediction").status_code, 401)
            self.assertEqual(self.client.get("/prediction", headers=self.headers).status_code, 405)
            self.assertEqual(self.client.post("/prediction", headers=self.headers).status_code, 400)
            start.assert_not_called()
            self.assertEqual(self.client.get("/prediction-status").status_code, 401)

    def test_dashboard_ingress_and_manual_prediction_share_worker(self):
        with patch.object(web_server, "prediction_preview", PredictionPreview()), patch("prediction_preview.Thread") as thread:
            page = self.client.get("/", headers=self.headers)
            self.assertIn(b'action="prediction"', page.data)
            response = self.client.post("/prediction", headers=self.headers,
                                        data={"preview_token": web_server.preview_token})
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["Location"], ".")
            status = self.client.get("/prediction-status", headers=self.headers)
            self.assertTrue(status.json["running"])
            self.assertEqual(status.json["message"],
                             "Prediction running. Results will appear in the logs and configured notification channel.")
            self.assertEqual(status.headers["Cache-Control"], "no-store")
            self.assertIn(b'id="prediction-button" disabled', self.client.get("/", headers=self.headers).data)
            ha_preview.listen_for_preview_commands(Mock(), io.StringIO('{"command":"prediction_preview"}\n'),
                                                   web_server.prediction_preview)
            thread.return_value.start.assert_called_once()

    def test_runtime_settings_and_ha_command_use_prediction_tariffs(self):
        with patch.dict(config.__dict__, {}), patch("config_manager.setup_logging"):
            config_manager.update_config({"prediction_time": "16:00", "prediction_tariffs": "agile,cosy"})
            self.assertEqual(config_manager.get_config()["prediction_time"], "16:00")
            self.assertEqual(config_manager.get_config()["prediction_tariffs"], "agile,cosy")
        comparison, prediction = Mock(), Mock()
        ha_preview.listen_for_preview_commands(comparison, io.StringIO('{"command":"prediction_preview"}\n'), prediction)
        prediction.start.assert_called_once_with()
        comparison.start.assert_not_called()


class PredictionAuthenticationTests(unittest.TestCase):
    def test_only_private_rest_calls_receive_basic_auth_and_foreign_origins_are_rejected(self):
        with patch.object(QueryService, "_shared_token", "token"), patch("query_service.requests.get") as get:
            service = QueryService("private-key", "https://api.example.invalid/v1")
            service.execute_rest_query("https://api.example.invalid/v1/products/")
            self.assertNotIn("auth", get.call_args.kwargs)
            service.execute_rest_query("https://api.example.invalid/v1/accounts/A-TEST/", authenticated=True)
            self.assertEqual(get.call_args.kwargs["auth"], ("private-key", ""))
            get.reset_mock()
            with self.assertRaises(Exception):
                service.execute_rest_query("https://foreign.invalid/", authenticated=True)
            get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
