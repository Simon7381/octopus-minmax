"""Firefox tests against local HTML fixtures; no requests to Octopus are made."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if (ROOT / ".playwright-browsers").exists():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".playwright-browsers"))

from playwright.sync_api import sync_playwright
from browser_switch import initiate_browser_switch, prepare_signup
from tariff import Tariff


def tariff(identifier, product="GO-FIX-12M-26-08-19"):
    return Tariff(identifier, identifier, identifier, "", "", True, product)


class SignupControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.firefox.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_default_timeout(1500)

    def tearDown(self):
        self.page.close()

    def load(self, html):
        self.page.set_content(html + '<button onclick="window.submitted = true">Switch Tariff</button>')

    def test_go_checks_displayed_terms_without_selecting_a_variant(self):
        self.load('<label><input type="checkbox">I accept the <a href="/smart/terms-and-conditions/GO-FIX-12M-26-08-19/">Terms &amp; Conditions</a></label>')
        prepare_signup(self.page, tariff("go", "GO-VAR-TEST"))
        self.assertTrue(self.page.get_by_role("checkbox").is_checked())
        self.assertIsNone(self.page.evaluate("window.submitted"))

    def test_agile_needs_only_switch_button(self):
        self.load("")
        prepare_signup(self.page, tariff("agile"))

    def test_agile_checks_terms_when_present(self):
        self.load('<label><input type="checkbox">I accept the Terms &amp; Conditions</label>')
        prepare_signup(self.page, tariff("agile"))
        self.assertTrue(self.page.get_by_role("checkbox").is_checked())

    def test_cosy_selects_variable_radio_instead_of_fixed(self):
        self.load('<label><input type="radio" name="plan" checked>Fixed</label><label><input type="radio" name="plan">Variable</label>')
        prepare_signup(self.page, tariff("cosy"))
        self.assertTrue(self.page.get_by_role("radio", name="Variable").is_checked())
        self.assertFalse(self.page.get_by_role("radio", name="Fixed").is_checked())

    def test_cosy_selects_variable_button(self):
        self.load('<button onclick="window.plan = \'variable\'">Variable</button>')
        prepare_signup(self.page, tariff("cosy"))
        self.assertEqual(self.page.evaluate("window.plan"), "variable")

    def test_cosy_missing_variable_fails_before_submission(self):
        self.load('<label><input type="radio" checked>Fixed</label>')
        with self.assertRaisesRegex(RuntimeError, "no Variable option"):
            prepare_signup(self.page, tariff("cosy"))
        self.assertIsNone(self.page.evaluate("window.submitted"))


class BrowserLifecycleTests(unittest.TestCase):
    def test_fixed_go_has_no_browser_route(self):
        with patch("playwright.sync_api.sync_playwright") as launch:
            with self.assertRaisesRegex(ValueError, "not supported.*go-fix-12m"):
                initiate_browser_switch(tariff("go-fix-12m"), "A-TEST", "user@example.invalid", "password", Mock())
            launch.assert_not_called()

    def test_missing_credentials_fail_before_launch(self):
        with patch("playwright.sync_api.sync_playwright") as launch:
            with self.assertRaisesRegex(RuntimeError, "OCTOPUS_LOGIN_EMAIL"):
                initiate_browser_switch(tariff("agile"), "A-TEST", "", "", Mock())
            launch.assert_not_called()

    def test_enrolment_resolution_keeps_browser_alive_and_always_closes_it(self):
        for identifier, slug in [("go", "go"), ("agile", "agile"), ("cosy", "cosy-octopus")]:
            with self.subTest(tariff=identifier), patch("playwright.sync_api.sync_playwright") as start, patch("browser_switch.prepare_signup"):
                browser = start.return_value.__enter__.return_value.firefox.launch.return_value
                context = browser.new_context.return_value
                page = context.new_page.return_value

                def confirm():
                    context.close.assert_not_called()
                    browser.close.assert_not_called()
                    raise RuntimeError("No enrolment")

                with self.assertRaisesRegex(RuntimeError, "No enrolment"):
                    initiate_browser_switch(tariff(identifier), "A-TEST", "test@example.invalid", "password", confirm)
                self.assertEqual(page.goto.call_args.args[0], f"https://octopus.energy/smart/{slug}/sign-up/?accountNumber=A-TEST")
                context.close.assert_called_once()
                browser.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
