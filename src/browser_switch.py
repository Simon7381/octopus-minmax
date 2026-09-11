"""Website fallback for tariff initiation; agreement acceptance stays in the API."""

import logging
import re
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from tariff import TARIFFS, Tariff

logger = logging.getLogger("octobot.browser_switch")


@dataclass(frozen=True)
class SignupFlow:
    slug: str
    option: str | None = None
    require_terms: bool = False


SIGNUP_FLOWS = {
    "go": SignupFlow("go", require_terms=True),
    "agile": SignupFlow("agile"),
    "cosy": SignupFlow("cosy-octopus", option="Variable"),
}


def save_failure_screenshot(page, name: str) -> None:
    try:
        screenshot = Path("logs") / f"playwright-{name}-failure.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(
            path=str(screenshot),
            full_page=True,
            mask=[page.locator('input[type="password"], input[type="email"]')],
        )
        logger.info("Playwright failure screenshot saved to %s", screenshot)
    except Exception:
        logger.warning("Could not capture Playwright failure screenshot.")


def wait_for_login_redirect(page, timeout_seconds: int = 60) -> None:
    """Wait for authentication, reporting an interactive challenge explicitly."""
    deadline = time.monotonic() + timeout_seconds
    challenge = page.locator('iframe[src*="hcaptcha.com"], iframe[title*="hCaptcha"]')
    while time.monotonic() < deadline:
        if re.match(r"^https://octopus\.energy(?:/|$)", page.url):
            return
        if any(challenge.nth(index).is_visible() for index in range(challenge.count())):
            raise RuntimeError(
                "Octopus login requires an interactive hCaptcha; headless Playwright cannot continue"
            )
        page.wait_for_timeout(250)
    raise RuntimeError(
        "Octopus login did not redirect to octopus.energy within 60 seconds"
    )


def prepare_signup(page, tariff: Tariff) -> dict:
    """Select the requested variant and accept any displayed signup terms."""
    flow = SIGNUP_FLOWS[tariff.id]
    checks = {"terms_checked": False, "variable_selected": False}
    page.get_by_role(
        "button", name=re.compile(r"^Switch Tariff$", re.IGNORECASE)
    ).wait_for()
    if flow.option:
        option_name = re.compile(rf"\b{flow.option}\b", re.IGNORECASE)
        radio = page.get_by_role("radio", name=option_name)
        button = page.get_by_role("button", name=option_name)
        label = page.get_by_text(flow.option, exact=True)
        if radio.count():
            radio.check()
        elif button.count():
            button.click()
        elif label.count():
            label.click()
        elif tariff.id == "cosy":
            raise RuntimeError(
                "Cosy signup has no Variable option; switch was not submitted"
            )
        checks["variable_selected"] = True

    terms = page.get_by_role(
        "checkbox", name=re.compile(r"I accept.*Terms", re.IGNORECASE)
    )
    if flow.require_terms:
        terms.wait_for()
        terms.check()
        checks["terms_checked"] = terms.is_checked()
    elif terms.count():
        terms.check()
        checks["terms_checked"] = terms.is_checked()
    return checks


@contextmanager
def logged_in_page(account_number: str, email: str, password: str):
    """Share the same login and browser lifecycle for checks and real switches."""
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                locale="en-GB",
                timezone_id="Europe/London",
            )
            try:
                page = context.new_page()
                page.set_default_timeout(60_000)
                step = "opening dashboard login entry"
                try:
                    page.goto("https://octopus.energy/dashboard/")
                    step = "waiting for Octopus authentication page"
                    page.wait_for_url(
                        re.compile(r"^https://auth\.octopus\.energy/login(?:/|\?|$)")
                    )
                    step = "entering email"
                    page.locator("#id_auth-username").fill(email)
                    step = "entering password"
                    page.locator("#id_auth-password").fill(password)
                    step = "submitting login"
                    page.locator("#submit-button").click()
                    step = "waiting for login redirect"
                    wait_for_login_redirect(page)
                    step = "opening dashboard"
                    page.goto("https://octopus.energy/dashboard/")
                    page.wait_for_load_state("domcontentloaded")
                    step = "verifying dashboard account"
                    page.wait_for_url(
                        re.compile(r"^https://octopus\.energy/dashboard(?:/|$)")
                    )
                    if (
                        account_number.lower() not in page.url.lower()
                        and account_number.lower() not in page.content().lower()
                    ):
                        raise RuntimeError(
                            "authenticated dashboard does not contain the configured account"
                        )
                except Error as exc:
                    # Playwright call logs may contain the filled password.
                    save_failure_screenshot(page, "login")
                    raise RuntimeError(
                        f"Octopus website login failed while {step}: {type(exc).__name__}"
                    ) from None
                except RuntimeError:
                    save_failure_screenshot(page, "login")
                    raise
                logger.info(
                    "Logged in to Octopus website and verified configured account."
                )
                yield page
            finally:
                context.close()
        finally:
            browser.close()


def open_signup(page, tariff: Tariff, account_number: str) -> None:
    slug = SIGNUP_FLOWS[tariff.id].slug
    page.goto(
        f"https://octopus.energy/smart/{slug}/sign-up/?{urlencode({'accountNumber': account_number})}"
    )


def run_playwright_checks(account_number: str, email: str, password: str) -> list:
    """Check all signup forms, using a trial click that never submits a switch."""
    if not email or not password or not account_number:
        raise RuntimeError(
            "TEST_PLAYWRIGHT requires ACC_NUMBER, OCTOPUS_LOGIN_EMAIL and OCTOPUS_LOGIN_PASSWD"
        )
    results = []
    with logged_in_page(account_number, email, password) as page:
        for identifier in SIGNUP_FLOWS:
            tariff = next(t for t in TARIFFS if t.id == identifier)
            logger.info("TEST_PLAYWRIGHT: checking %s signup.", identifier)
            try:
                open_signup(page, tariff, account_number)
                checks = prepare_signup(page, tariff)
                # Checks visibility, enabled state and actionability without clicking.
                page.get_by_role(
                    "button", name=re.compile(r"^Switch Tariff$", re.IGNORECASE)
                ).click(trial=True)
                result = {
                    "tariff": identifier,
                    "passed": True,
                    **checks,
                    "switch_button_ready": True,
                }
                logger.info(
                    "TEST_PLAYWRIGHT PASS %s: terms=%s, variable=%s, Switch Tariff button ready; not clicked.",
                    identifier,
                    "checked" if checks["terms_checked"] else "not displayed",
                    "selected" if checks["variable_selected"] else "not required",
                )
            except Exception as exc:
                # Report types only: browser errors can contain account details.
                result = {
                    "tariff": identifier,
                    "passed": False,
                    "error": type(exc).__name__,
                }
                logger.error(
                    "TEST_PLAYWRIGHT FAIL %s: %s; no switch submitted.",
                    identifier,
                    type(exc).__name__,
                )
                save_failure_screenshot(page, identifier)
            results.append(result)
    return results


def initiate_browser_switch(
    tariff: Tariff,
    account_number: str,
    email: str,
    password: str,
    wait_for_enrolment: Callable[[], str],
) -> str:
    if tariff.id not in SIGNUP_FLOWS:
        raise ValueError(f"Website fallback is not supported for tariff '{tariff.id}'")
    if not email or not password:
        raise RuntimeError(
            "Website fallback requires OCTOPUS_LOGIN_EMAIL and OCTOPUS_LOGIN_PASSWD"
        )
    if not account_number:
        raise ValueError("Website fallback requires ACC_NUMBER")

    # Lazy import keeps API-only installations usable without a browser runtime.
    from playwright.sync_api import Error

    with logged_in_page(account_number, email, password) as page:
        stage = "tariff selection"
        try:
            open_signup(page, tariff, account_number)
            prepare_signup(page, tariff)
            stage = "tariff submission (check your account before retrying)"
            page.get_by_role(
                "button", name=re.compile(r"^Switch Tariff$", re.IGNORECASE)
            ).click()
            return wait_for_enrolment()
        except Error as exc:
            raise RuntimeError(
                f"Octopus website failed during {stage}: {type(exc).__name__}"
            ) from None
