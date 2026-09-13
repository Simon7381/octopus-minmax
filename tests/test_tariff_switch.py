import sys
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from account_manager import AccountManager
from query_service import GQLAuthorizationError, QueryService
from tariff import Tariff


def enrolment(identifier, product="AGILE-TEST", status="IN_PROGRESS"):
    return {"id": identifier, "product": {"code": product}, "status": status}


class TariffSwitchTests(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.tariff = Tariff("agile", "Agile", "Agile", "agile", "agile", True, "AGILE-TEST")
        self.manager = AccountManager(self.service, [self.tariff])
        self.manager.mpan = "12345"
        self.manager.config = SimpleNamespace(ACC_NUMBER="A-TEST", OCTOPUS_LOGIN_EMAIL="test@example.invalid",
                                              OCTOPUS_LOGIN_PASSWD="test-password")

    def test_api_success_does_not_open_browser(self):
        self.service.execute_gql_query.return_value = {"startOnboardingProcess": {"productEnrolment": {"id": "api-id"}}}
        with patch("account_manager.initiate_browser_switch") as browser:
            self.assertEqual(self.manager.initiate_tariff_switch("AGILE-TEST"), "api-id")
            browser.assert_not_called()

    def test_fixed_go_remains_available_via_api_only(self):
        self.tariff.id = "go-fix-12m"
        self.tariff.product_code = "GO-FIX-TEST"
        self.service.execute_gql_query.side_effect = [
            {"startOnboardingProcess": {"productEnrolment": {"id": "fixed-api-id"}}},
            GQLAuthorizationError(),
        ]
        with patch("account_manager.initiate_browser_switch") as browser:
            self.assertEqual(self.manager.initiate_tariff_switch("GO-FIX-TEST"), "fixed-api-id")
            with self.assertRaisesRegex(ValueError, "GraphQL switching only"):
                self.manager.initiate_tariff_switch("GO-FIX-TEST")
            browser.assert_not_called()
        self.assertEqual(self.service.execute_gql_query.call_count, 2)

    def test_failure_polls_exact_product_and_ignores_old_completed_entries(self):
        before = [enrolment("old", status="COMPLETED")]
        after = before + [enrolment("wrong", "COSY-TEST"), enrolment("new")]
        self.service.execute_gql_query.side_effect = [GQLAuthorizationError(), {"productEnrolments": before},
                                                      {"productEnrolments": before}, {"productEnrolments": after}]
        with patch("account_manager.initiate_browser_switch", side_effect=lambda *args: args[-1]()) as browser, \
                patch("account_manager.time.sleep") as sleep:
            self.assertEqual(self.manager.initiate_tariff_switch("AGILE-TEST"), "new")
            browser.assert_called_once()
            sleep.assert_called_once_with(10)

    def test_missing_api_id_reuses_matching_pending_enrolment(self):
        self.service.execute_gql_query.side_effect = [{"startOnboardingProcess": None},
                                                      {"productEnrolments": [enrolment("existing")]}]
        with patch("account_manager.initiate_browser_switch") as browser:
            self.assertEqual(self.manager.initiate_tariff_switch("AGILE-TEST"), "existing")
            browser.assert_not_called()

    def test_unavailable_enrolments_prevent_browser_submission(self):
        self.service.execute_gql_query.side_effect = [RuntimeError(), {"productEnrolments": None}]
        with patch("account_manager.initiate_browser_switch") as browser:
            with self.assertRaisesRegex(RuntimeError, "enrolments are unavailable"):
                self.manager.initiate_tariff_switch("AGILE-TEST")
            browser.assert_not_called()

    def test_ambiguous_enrolments_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Multiple matching"):
            self.manager._matching_enrolment([enrolment("one"), enrolment("two")], "AGILE-TEST")

    def test_poll_timeout_does_not_return_an_unrelated_enrolment(self):
        self.service.execute_gql_query.side_effect = [RuntimeError(), {"productEnrolments": []}] + [
            {"productEnrolments": [enrolment("wrong", "COSY-TEST")]}] * 13
        with patch("account_manager.initiate_browser_switch", side_effect=lambda *args: args[-1]()) as browser, \
                patch("account_manager.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "no new enrolment for the exact"):
                self.manager.initiate_tariff_switch("AGILE-TEST")
            browser.assert_called_once()

    def test_browser_auto_acceptance_is_not_accepted_twice(self):
        self.service.execute_gql_query.side_effect = [RuntimeError(), {"productEnrolments": []},
                                                      {"productEnrolments": [enrolment("done", status="COMPLETED")]}]
        with patch("account_manager.initiate_browser_switch", side_effect=lambda *args: args[-1]()):
            identifier = self.manager.initiate_tariff_switch("AGILE-TEST")
        self.service.execute_gql_query.reset_mock()
        self.assertEqual(self.manager.accept_new_agreement("AGILE-TEST", identifier), "already accepted on website")
        self.service.execute_gql_query.assert_not_called()

    def test_normal_agreement_acceptance_uses_api(self):
        self.service.execute_gql_query.side_effect = [{"termsAndConditionsForProduct": {"version": "2.3"}},
                                                      {"acceptTermsAndConditions": {"acceptedVersion": "2.3"}}]
        self.assertEqual(self.manager.accept_new_agreement("AGILE-TEST", "new"), "2.3")
        query = self.service.execute_gql_query.call_args.args[0]
        self.assertIn('enrolmentId: "new"', query)
        self.assertIn('versionMajor: 2', query)

    def test_completed_post_enrolment_skips_acceptance_even_if_status_lags(self):
        entry = enrolment("done")
        entry["stages"] = [{"name": "post-enrolment", "status": "COMPLETED"}]
        self.service.execute_gql_query.side_effect = [RuntimeError(), {"productEnrolments": []},
                                                      {"productEnrolments": [entry]}]
        with patch("account_manager.initiate_browser_switch", side_effect=lambda *args: args[-1]()):
            identifier = self.manager.initiate_tariff_switch("AGILE-TEST")
        self.assertEqual(self.manager.accept_new_agreement("AGILE-TEST", identifier), "already accepted on website")

    def test_verification_requires_target_product_and_import_meter(self):
        agreement = {"validFrom": date.today().isoformat(), "tariff": {"productCode": "COSY-TEST"},
                     "meterPoint": {"direction": "IMPORT", "mpan": "12345"}}
        self.service.execute_gql_query.return_value = {"account": {"electricityAgreements": [agreement]}}
        self.assertFalse(self.manager.verify_new_agreement_status("AGILE-TEST"))
        agreement["tariff"]["productCode"] = "AGILE-TEST"
        self.assertTrue(self.manager.verify_new_agreement_status("AGILE-TEST"))
        agreement["meterPoint"]["direction"] = "EXPORT"
        self.assertFalse(self.manager.verify_new_agreement_status("AGILE-TEST"))


class QueryRetryTests(unittest.TestCase):
    def setUp(self):
        with patch.object(QueryService, "_shared_token", "token"):
            self.service = QueryService("key", "https://example.invalid")

    def test_permission_denial_is_not_retried(self):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {"errors": [{"extensions": {"errorCode": "KT-CT-1111"}}]}
        with patch("query_service.requests.post", return_value=response) as post, patch("query_service.time.sleep") as sleep:
            with self.assertRaises(GQLAuthorizationError):
                self.service.execute_gql_query("mutation { startOnboardingProcess }")
            post.assert_called_once()
            sleep.assert_not_called()

    def test_expired_token_still_refreshes(self):
        expired = Mock(ok=True, status_code=200)
        expired.json.return_value = {"errors": [{"extensions": {"errorCode": "KT-CT-1124"}}]}
        success = Mock(ok=True, status_code=200)
        success.json.return_value = {"data": {"ok": True}}
        with patch("query_service.requests.post", side_effect=[expired, success]), \
                patch.object(self.service, "_get_token", return_value="new-token") as refresh, \
                patch.object(QueryService, "_shared_token", "old-token"):
            self.assertEqual(self.service.execute_gql_query("query { ok }"), {"ok": True})
            refresh.assert_called_once()

    def test_network_error_can_retry_before_response_exists(self):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {"data": {"ok": True}}
        with patch("query_service.requests.post", side_effect=[ConnectionError(), response]), patch("query_service.time.sleep"):
            self.assertEqual(self.service.execute_gql_query("query { ok }"), {"ok": True})


if __name__ == "__main__":
    unittest.main()
