"""Browser switch tests using invisible-playwright C++ patched Firefox against local HTML fixtures; no requests to Octopus are made."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if (ROOT / ".playwright-browsers").exists():
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".playwright-browsers")
    )

from invisible_playwright import InvisiblePlaywright

from browser_switch import (
    initiate_browser_switch,
    logged_in_page,
    prepare_signup,
    run_playwright_checks,
    wait_for_login_redirect,
)
from tariff import Tariff


def tariff(identifier, product="GO-FIX-12M-26-08-19"):
    return Tariff(identifier, identifier, identifier, "", "", True, product)


class SignupControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inv = InvisiblePlaywright()
        cls.browser = cls.inv.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.inv.__exit__(None, None, None)

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_default_timeout(1500)

    def tearDown(self):
        self.page.close()

    def load(self, html):
        self.page.set_content(
            html + '<button onclick="window.submitted = true">Switch Tariff</button>'
        )

    def test_go_checks_displayed_terms_without_selecting_a_variant(self):
        self.load(
            '<label><input type="checkbox">I accept the <a href="/smart/terms-and-conditions/GO-FIX-12M-26-08-19/">Terms &amp; Conditions</a></label>'
        )
        prepare_signup(self.page, tariff("go", "GO-VAR-TEST"))
        self.assertTrue(self.page.get_by_role("checkbox").is_checked())
        self.assertIsNone(self.page.evaluate("window.submitted"))

    def test_agile_needs_only_switch_button(self):
        self.load("")
        prepare_signup(self.page, tariff("agile"))

    def test_agile_checks_terms_when_present(self):
        self.load(
            '<label><input type="checkbox">I accept the Terms &amp; Conditions</label>'
        )
        prepare_signup(self.page, tariff("agile"))
        self.assertTrue(self.page.get_by_role("checkbox").is_checked())

    def test_cosy_selects_variable_radio_instead_of_fixed(self):
        self.load(
            '<label><input type="radio" name="plan" checked>Fixed</label><label><input type="radio" name="plan">Variable</label>'
        )
        prepare_signup(self.page, tariff("cosy"))
        self.assertTrue(self.page.get_by_role("radio", name="Variable").is_checked())
        self.assertFalse(self.page.get_by_role("radio", name="Fixed").is_checked())

    def test_cosy_selects_variable_button(self):
        self.load("<button onclick=\"window.plan = 'variable'\">Variable</button>")
        prepare_signup(self.page, tariff("cosy"))
        self.assertEqual(self.page.evaluate("window.plan"), "variable")

    def test_cosy_missing_variable_fails_before_submission(self):
        self.load('<label><input type="radio" checked>Fixed</label>')
        with self.assertRaisesRegex(RuntimeError, "no Variable option"):
            prepare_signup(self.page, tariff("cosy"))
        self.assertIsNone(self.page.evaluate("window.submitted"))

    def test_test_mode_checks_all_three_forms_without_submitting(self):
        visited = []

        def open_fixture(page, target, account_number):
            self.assertIsNone(page.evaluate("window.submitted"))
            visited.append(target.id)
            fixtures = {
                "go": '<label><input type="checkbox">I accept the Terms &amp; Conditions</label>',
                "agile": "",
                "cosy": '<label><input type="radio" name="plan" checked>Fixed</label><label><input type="radio" name="plan">Variable</label>',
            }
            self.load(fixtures[target.id])

        with (
            patch("browser_switch.logged_in_page") as login,
            patch("browser_switch.open_signup", side_effect=open_fixture),
        ):
            login.return_value.__enter__.return_value = self.page
            results = run_playwright_checks(
                "A-TEST", "user@example.invalid", "password"
            )
        self.assertEqual(visited, ["go", "agile", "cosy"])
        self.assertTrue(
            all(
                result["passed"] and result["switch_button_ready"] for result in results
            )
        )
        self.assertTrue(results[0]["terms_checked"])
        self.assertTrue(results[2]["variable_selected"])
        self.assertIsNone(self.page.evaluate("window.submitted"))


class BrowserLifecycleTests(unittest.TestCase):
    def test_visible_hcaptcha_stops_login_check(self):
        page = Mock()
        page.url = "https://auth.octopus.energy/login/"
        challenge = page.locator.return_value
        challenge.count.return_value = 2
        challenge.nth.return_value.is_visible.side_effect = [False, True]
        with self.assertRaisesRegex(RuntimeError, "interactive hCaptcha"):
            wait_for_login_redirect(page)
        page.wait_for_timeout.assert_not_called()

    def test_login_uses_auth_url_and_verifies_dashboard_account(self):
        with patch("browser_switch.InvisiblePlaywright") as start:
            browser = start.return_value.__enter__.return_value
            context = browser.new_context.return_value
            page = context.new_page.return_value
            page.url = "https://octopus.energy/dashboard/new/accounts/A-TEST/dashboard"
            with logged_in_page(
                "A-TEST", "user@example.invalid", "password"
            ) as yielded:
                self.assertIs(yielded, page)
            self.assertEqual(
                page.goto.call_args_list[0].args[0], "https://octopus.energy/dashboard/"
            )
            self.assertEqual(
                page.goto.call_args_list[1].args[0], "https://octopus.energy/dashboard/"
            )
            self.assertTrue(
                any(
                    call.args
                    and getattr(call.args[0], "pattern", "").startswith(
                        "^https://auth\\.octopus"
                    )
                    for call in page.wait_for_url.call_args_list
                )
            )
            context_options = context.call_args if hasattr(context, 'call_args') else None
            page.locator.assert_any_call("#id_auth-username")
            page.locator.assert_any_call("#id_auth-password")
            page.locator.assert_any_call("#submit-button")

    def test_login_rejects_dashboard_for_another_account(self):
        with patch("browser_switch.InvisiblePlaywright") as start:
            page = start.return_value.__enter__.return_value.new_context.return_value.new_page.return_value
            page.url = "https://octopus.energy/dashboard/new/accounts/A-OTHER/dashboard"
            page.content.return_value = "another account"
            with (
                self.assertRaisesRegex(
                    RuntimeError, "does not contain the configured account"
                ),
                logged_in_page("A-TEST", "user@example.invalid", "password"),
            ):
                pass

    def test_test_mode_continues_after_one_tariff_fails(self):
        with (
            patch("browser_switch.logged_in_page") as login,
            patch("browser_switch.open_signup"),
            patch(
                "browser_switch.prepare_signup",
                side_effect=[
                    RuntimeError("Failed"),
                    {"terms_checked": False, "variable_selected": False},
                    {"terms_checked": False, "variable_selected": True},
                ],
            ),
        ):
            results = run_playwright_checks(
                "A-TEST", "user@example.invalid", "password"
            )
            page = login.return_value.__enter__.return_value
            self.assertEqual(
                [result["passed"] for result in results], [False, True, True]
            )
            page.get_by_role.return_value.wait_for.assert_called_with(state="visible")

    def test_fixed_go_has_no_browser_route(self):
        with patch("browser_switch.InvisiblePlaywright") as launch:
            with self.assertRaisesRegex(ValueError, "not supported.*go-fix-12m"):
                initiate_browser_switch(
                    tariff("go-fix-12m"),
                    "A-TEST",
                    "user@example.invalid",
                    "password",
                    Mock(),
                )
            launch.assert_not_called()

    def test_missing_credentials_fail_before_launch(self):
        with patch("browser_switch.InvisiblePlaywright") as launch:
            with self.assertRaisesRegex(RuntimeError, "OCTOPUS_LOGIN_EMAIL"):
                initiate_browser_switch(tariff("agile"), "A-TEST", "", "", Mock())
            launch.assert_not_called()

    def test_enrolment_resolution_keeps_browser_alive_and_always_closes_it(self):
        for identifier, slug in [
            ("go", "go"),
            ("agile", "agile"),
            ("cosy", "cosy-octopus"),
        ]:
            with (
                self.subTest(tariff=identifier),
                patch("browser_switch.InvisiblePlaywright") as start,
                patch("browser_switch.prepare_signup"),
            ):
                browser = start.return_value.__enter__.return_value
                context = browser.new_context.return_value
                page = context.new_page.return_value
                page.url = (
                    "https://octopus.energy/dashboard/new/accounts/A-TEST/dashboard"
                )

                def confirm():
                    context.close.assert_not_called()
                    browser.close.assert_not_called()
                    raise RuntimeError("No enrolment")

                with self.assertRaisesRegex(RuntimeError, "No enrolment"):
                    initiate_browser_switch(
                        tariff(identifier),
                        "A-TEST",
                        "test@example.invalid",
                        "password",
                        confirm,
                    )
                self.assertEqual(
                    page.goto.call_args.args[0],
                    f"https://octopus.energy/smart/{slug}/sign-up/?accountNumber=A-TEST",
                )
                context.close.assert_called_once()
                browser.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
