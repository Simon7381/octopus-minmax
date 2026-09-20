"""Browser switch tests using invisible-playwright C++ patched Firefox against local HTML fixtures; no requests to Octopus are made."""

import os
import sys
import tempfile
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
from invisible_playwright._pw.sync_api import Error as InvisiblePlaywrightError
from playwright.sync_api import Error

from browser_switch import (
    BROWSER_SEED,
    DESKTOP_HARDWARE_PINS,
    DESKTOP_VIEWPORT,
    CREDENTIAL_MASK_SELECTOR,
    fill_login_field,
    login_and_verify_account,
    initiate_browser_switch,
    logged_in_page,
    prewarm_browser_login,
    prepare_signup,
    run_playwright_checks,
    save_failure_screenshot,
    wait_for_login_redirect,
)
from tariff import Tariff


def tariff(identifier, product="GO-FIX-12M-26-08-19"):
    return Tariff(identifier, identifier, identifier, "", "", True, product)


class SignupControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inv = InvisiblePlaywright(headless=True)
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

    def test_login_entry_waits_for_editable_fields_and_replaces_partial_values(self):
        self.page.set_content('''
            <input id="id_auth-username" type="text" value="partial" disabled>
            <input id="id_auth-password" type="password" value="old" disabled>
            <script>
              window.inputValues = [];
              document.addEventListener('input', event => window.inputValues.push(event.target.value));
              setTimeout(() => document.querySelectorAll('input').forEach(
                input => input.disabled = false), 500);
            </script>
        ''')
        email = "test+login@example.invalid"
        password = "Synthetic-only-£-long-password"
        fill_login_field(self.page, "#id_auth-username", email, "Email")
        fill_login_field(self.page, "#id_auth-password", password, "Password")
        self.assertEqual(self.page.locator("#id_auth-username").input_value(), email)
        self.assertEqual(self.page.locator("#id_auth-password").input_value(), password)
        self.assertEqual(set(self.page.evaluate("window.inputValues")), {email, password})
        # Include text-type usernames and a password revealed by the eye button.
        self.page.locator("#id_auth-password").evaluate("el => el.type = 'text'")
        self.assertEqual(self.page.locator(CREDENTIAL_MASK_SELECTOR).count(), 2)

    def test_login_entry_replaces_value_after_one_partial_input(self):
        self.page.set_content('''
            <input id="id_auth-username" type="text">
            <script>
              window.inputs = 0;
              document.querySelector('input').addEventListener('input', event => {
                if (++window.inputs === 1) event.target.value = 'partial';
              });
            </script>
        ''')
        fill_login_field(self.page, "#id_auth-username", "test@example.invalid", "Email")
        self.assertEqual(self.page.locator("#id_auth-username").input_value(), "test@example.invalid")
        self.assertGreaterEqual(self.page.evaluate("window.inputs"), 2)

    def test_login_entry_stops_after_two_incomplete_inputs_without_leaking_value(self):
        self.page.set_content('''
            <input id="id_auth-username" oninput="this.value = 'partial'">
        ''')
        with self.assertRaisesRegex(RuntimeError, "Email entry was incomplete") as raised:
            fill_login_field(self.page, "#id_auth-username", "private@example.invalid", "Email")
        self.assertNotIn("private@example.invalid", str(raised.exception))

    def test_screenshot_hides_credentials_and_restores_fields_even_on_failure(self):
        self.page.set_content('<input id="id_auth-username" value="synthetic@example.invalid">')
        field = self.page.locator("#id_auth-username")
        original_screenshot = self.page.screenshot
        for fails in (False, True):
            with self.subTest(fails=fails), tempfile.TemporaryDirectory() as directory:
                def capture(**kwargs):
                    self.assertFalse(field.is_visible())
                    if fails:
                        raise InvisiblePlaywrightError("Synthetic screenshot failure")
                    return original_screenshot(**kwargs)

                with patch("browser_switch.Path", return_value=Path(directory)), \
                        patch.object(self.page, "screenshot", side_effect=capture):
                    save_failure_screenshot(self.page, "test")
                self.assertTrue(field.is_visible())
                self.assertEqual(field.input_value(), "synthetic@example.invalid")
                self.assertEqual((Path(directory) / "playwright-test-failure.png").exists(), not fails)

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


class BrowserHardwareTests(unittest.TestCase):
    def test_persistent_browser_reports_desktop_profile_across_restarts(self):
        # Use the production launch path, isolated from real cookies and Octopus.
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            os.chdir(directory)
            try:
                fingerprints = []
                with patch("browser_switch.login_and_verify_account"):
                    for _ in range(2):
                        with logged_in_page("A-TEST", "test@example.invalid", "test") as page:
                            fingerprints.append(page.evaluate("""() => {
                                const gl = document.createElement('canvas').getContext('webgl');
                                const info = gl.getExtension('WEBGL_debug_renderer_info');
                                return {
                                    viewport: [innerWidth, innerHeight],
                                    screen: [screen.width, screen.height],
                                    available: [screen.availWidth, screen.availHeight],
                                    dpr: devicePixelRatio,
                                    concurrency: navigator.hardwareConcurrency,
                                    vendor: gl.getParameter(info.UNMASKED_VENDOR_WEBGL),
                                    renderer: gl.getParameter(info.UNMASKED_RENDERER_WEBGL),
                                    locale: navigator.language,
                                    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                                };
                            }"""))
                self.assertEqual(fingerprints[0], fingerprints[1])
                actual = fingerprints[0]
                self.assertEqual(actual["viewport"], [1902, 1398])
                self.assertEqual(actual["screen"], [3840, 1600])
                self.assertEqual(actual["available"], [3840, 1552])
                self.assertEqual(actual["dpr"], 1.0)
                self.assertEqual(actual["concurrency"], 24)
                self.assertEqual(actual["vendor"], DESKTOP_HARDWARE_PINS["gpu.vendor"])
                # Firefox sanitizes the ANGLE string before exposing it to pages.
                self.assertIn("ANGLE (AMD, Radeon R9 200 Series", actual["renderer"])
                self.assertEqual(actual["locale"], "en-GB")
                self.assertEqual(actual["timezone"], "Europe/London")
            finally:
                os.chdir(previous)


class BrowserLifecycleTests(unittest.TestCase):
    def test_field_readiness_failure_does_not_expose_assertion_contents(self):
        page = Mock()
        with patch("browser_switch.expect") as editable:
            editable.return_value.to_be_editable.side_effect = AssertionError("DOM contains private@example.invalid")
            with self.assertRaisesRegex(RuntimeError, "Email field did not become editable") as raised:
                fill_login_field(page, "#id_auth-username", "private@example.invalid", "Email")
        self.assertNotIn("private@example.invalid", str(raised.exception))
        page.keyboard.insert_text.assert_not_called()

    def test_page_creation_failure_logs_stage_and_closes_browser(self):
        with patch("browser_switch.InvisiblePlaywright") as start:
            context = Mock(spec=["new_page", "pages", "close"])
            context.pages = []
            context.new_page.side_effect = InvisiblePlaywrightError(
                "BrowserContext.new_page: Timeout 30000ms exceeded. password=secret-value"
            )
            start.return_value.__enter__.return_value = context
            with self.assertLogs("octobot.browser_switch", level="DEBUG") as captured:
                with self.assertRaises(InvisiblePlaywrightError):
                    with logged_in_page("A-TEST", "email", "password"):
                        self.fail("Page creation failed")
            output = "\n".join(captured.output)
            self.assertIn("creating website browser page", output)
            self.assertIn("BrowserContext.new_page (timed out after 30000 ms)", output)
            self.assertIn("Browser runtime: invisible-playwright=", output)
            self.assertNotIn("secret-value", output)
            context.close.assert_not_called()
            start.return_value.__exit__.assert_called_once()

    def test_prewarm_verifies_account_and_closes_persistent_context(self):
        with patch("browser_switch.InvisiblePlaywright") as start:
            context = Mock(spec=["new_page", "pages", "close"])
            page = Mock()
            context.pages = [page]
            start.return_value.__enter__.return_value = context
            page.url = "https://octopus.energy/dashboard/"
            page.content.return_value = "Account A-12345678"
            page.locator.return_value.count.return_value = 0

            account = prewarm_browser_login("A-12345678", "email", "password")

            self.assertEqual(account, "A-12345678")
            start.assert_called_once_with(
                profile_dir=Path("logs/browser_profile"), headless=True,
                seed=BROWSER_SEED, pin=DESKTOP_HARDWARE_PINS,
                locale="en-GB", timezone="Europe/London",
            )
            page.set_viewport_size.assert_called_once_with(DESKTOP_VIEWPORT)
            page.content.assert_called_once()
            page.locator.return_value.fill.assert_not_called()
            page.get_by_role.assert_not_called()
            context.close.assert_not_called()
            start.return_value.__exit__.assert_called_once()

    def test_prewarm_rejects_missing_credentials_before_launch(self):
        with patch("browser_switch.InvisiblePlaywright") as start:
            for args in [("", "email", "password"), ("A-TEST", "", "password"), ("A-TEST", "email", "")]:
                with self.subTest(args=args), self.assertRaisesRegex(RuntimeError, "Browser login requires"):
                    prewarm_browser_login(*args)
            start.assert_not_called()

    def test_login_inspection_error_is_not_treated_as_authenticated(self):
        with patch("browser_switch.InvisiblePlaywright") as start, \
                patch("browser_switch.save_failure_screenshot") as screenshot, \
                self.assertLogs("octobot.browser_switch", level="DEBUG") as captured:
            page = start.return_value.__enter__.return_value.new_context.return_value.new_page.return_value
            page.url = "https://octopus.energy/dashboard/"
            page.locator.return_value.count.side_effect = InvisiblePlaywrightError(
                "Locator.count: Failed to find execution context with id = id-9 private-value"
            )
            with self.assertRaisesRegex(RuntimeError, "checking whether login is required.*Locator.count"):
                with logged_in_page("A-TEST", "email", "password"):
                    self.fail("Failed page inspection must not yield an authenticated page")
            screenshot.assert_called_once_with(page, "login")
        self.assertNotIn("private-value", "\n".join(captured.output))

    def test_context_error_reports_stage_and_does_not_submit_or_leak_call_log(self):
        with patch("browser_switch.logged_in_page") as login, \
                patch("browser_switch.open_signup"), \
                patch("browser_switch.prepare_signup", side_effect=InvisiblePlaywrightError(
                    "Locator.count: Failed to find execution context with id = id-9 password=private-password"
                )), \
                patch("browser_switch.save_failure_screenshot") as screenshot, \
                self.assertLogs("octobot.browser_switch", level="DEBUG") as captured:
            resolve = Mock()
            with self.assertRaisesRegex(RuntimeError, "preparing agile signup controls.*Locator.count") as raised:
                initiate_browser_switch(tariff("agile"), "A-TEST", "email", "password", resolve)
            page = login.return_value.__enter__.return_value
            page.get_by_role.assert_not_called()
            resolve.assert_not_called()
            screenshot.assert_called_once_with(page, "agile")
        self.assertNotIn("private-password", str(raised.exception))
        self.assertNotIn("private-password", "\n".join(captured.output))

    def test_submission_error_is_not_retried_and_warns_to_check_account(self):
        with patch("browser_switch.logged_in_page") as login, \
                patch("browser_switch.open_signup"), \
                patch("browser_switch.prepare_signup"), \
                patch("browser_switch.save_failure_screenshot"):
            page = login.return_value.__enter__.return_value
            page.get_by_role.return_value.click.side_effect = Error("execution context was destroyed")
            with self.assertRaisesRegex(RuntimeError, "check your account before retrying"):
                initiate_browser_switch(tariff("agile"), "A-TEST", "email", "password", Mock())
            page.get_by_role.return_value.click.assert_called_once()

    def test_visible_hcaptcha_stops_login_check(self):
        page = Mock()
        page.url = "https://auth.octopus.energy/login/"
        challenge = page.locator.return_value
        challenge.count.return_value = 2
        challenge.nth.return_value.is_visible.side_effect = [False, True]
        with self.assertRaisesRegex(RuntimeError, "interactive hCaptcha"):
            wait_for_login_redirect(page)
        page.wait_for_timeout.assert_not_called()

    def test_login_redirect_tolerates_document_replacement_during_challenge_check(self):
        page = Mock()
        page.url = "https://auth.octopus.energy/login/"
        page.locator.return_value.count.side_effect = InvisiblePlaywrightError(
            "Locator.count: Failed to find execution context with id = old-document"
        )
        page.wait_for_timeout.side_effect = lambda _: setattr(page, "url", "https://octopus.energy/dashboard/")
        wait_for_login_redirect(page)
        page.wait_for_timeout.assert_called_once_with(250)
        page.locator.return_value.click.assert_not_called()

    def test_login_redirect_does_not_swallow_closed_browser_errors(self):
        page = Mock()
        page.url = "https://auth.octopus.energy/login/"
        page.locator.return_value.count.side_effect = InvisiblePlaywrightError("Browser has been closed")
        with self.assertRaises(InvisiblePlaywrightError):
            wait_for_login_redirect(page)
        page.wait_for_timeout.assert_not_called()

    def test_login_uses_auth_url_and_verifies_dashboard_account(self):
        with patch("browser_switch.InvisiblePlaywright") as start, \
                patch("browser_switch.fill_login_field") as fill_field:
            browser = start.return_value.__enter__.return_value
            context = browser.new_context.return_value
            page = context.new_page.return_value
            page.url = "https://auth.octopus.energy/login/"
            page.content.return_value = "dashboard for A-TEST"

            def handle_click():
                page.url = "https://octopus.energy/dashboard/accounts/A-TEST/dashboard"

            page.locator.return_value.click.side_effect = handle_click
            page.locator.return_value.count.return_value = 0
            page.locator.return_value.input_value.side_effect = ["user@example.invalid", "password"]
            with logged_in_page(
                "A-TEST", "user@example.invalid", "password"
            ) as yielded:
                self.assertIs(yielded, page)
            page.set_viewport_size.assert_called_once_with(DESKTOP_VIEWPORT)
            self.assertEqual(
                page.goto.call_args_list[0].args[0], "https://octopus.energy/dashboard/"
            )
            # Let the successful redirect finish without reloading the dashboard.
            page.goto.assert_called_once_with("https://octopus.energy/dashboard/")
            self.assertTrue(
                any(
                    call.args
                    and getattr(call.args[0], "pattern", "").startswith(
                        "^https://auth\\.octopus"
                    )
                    for call in page.wait_for_url.call_args_list
                )
            )
            page.locator.assert_any_call("#id_auth-username")
            page.locator.assert_any_call("#id_auth-password")
            page.locator.assert_any_call("#submit-button")
            self.assertEqual(fill_field.call_count, 2)

    def test_changed_login_field_prevents_submission(self):
        page = Mock()
        page.url = "https://auth.octopus.energy/login/"
        page.locator.return_value.input_value.return_value = "partial"
        with patch("browser_switch.fill_login_field"), \
                patch("browser_switch.save_failure_screenshot"), \
                self.assertRaisesRegex(RuntimeError, "Login fields changed before submission"):
            login_and_verify_account(page, "A-TEST", "email", "password")
        page.locator.return_value.click.assert_not_called()

    def test_account_verification_waits_for_dashboard_data(self):
        page = Mock()
        page.url = "https://octopus.energy/dashboard/"
        page.locator.return_value.count.return_value = 0
        page.content.side_effect = ["Loading dashboard", "Account A-TEST"]
        login_and_verify_account(page, "A-TEST", "email", "password")
        page.wait_for_function.assert_called_once()
        self.assertEqual(page.wait_for_function.call_args.kwargs["arg"], "A-TEST")
        page.locator.return_value.click.assert_not_called()

    def test_login_reuses_existing_authenticated_session(self):
        with patch("browser_switch.InvisiblePlaywright") as start:
            browser = start.return_value.__enter__.return_value
            context = browser.new_context.return_value
            page = context.new_page.return_value
            page.url = "https://octopus.energy/dashboard/accounts/A-TEST/dashboard"
            page.content.return_value = "authenticated dashboard for A-TEST"
            with logged_in_page(
                "A-TEST", "user@example.invalid", "password"
            ) as yielded:
                self.assertIs(yielded, page)
            self.assertEqual(len(page.goto.call_args_list), 1)
            self.assertEqual(
                page.goto.call_args_list[0].args[0], "https://octopus.energy/dashboard/"
            )

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
                browser.close.assert_not_called()
                start.return_value.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
