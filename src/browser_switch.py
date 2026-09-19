"""Website fallback for tariff initiation; agreement acceptance stays in the API."""

import logging
import re
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from invisible_playwright import InvisiblePlaywright
# Invisible Playwright uses its own client and exception hierarchy.
from invisible_playwright._pw.sync_api import Error as InvisiblePlaywrightError
from playwright.sync_api import Error

from diagnostics import error_summary, log_failure
from tariff import TARIFFS, Tariff

logger = logging.getLogger("octobot.browser_switch")


def log_stage(stage: str) -> str:
    logger.debug("Playwright stage: %s", stage)
    return stage


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


def save_stage_screenshot(page, name: str) -> None:
    try:
        screenshot = Path("logs") / f"playwright-{name}-stage.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(
            path=str(screenshot),
            full_page=True,
            mask=[page.locator('input[type="password"], input[type="email"]')],
        )
        logger.info("Playwright stage screenshot saved to %s", screenshot)
    except Exception as exc:
        logger.warning("Could not capture Playwright stage screenshot: %s", error_summary(exc))


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
    except Exception as exc:
        logger.warning("Could not capture Playwright failure screenshot: %s", error_summary(exc))


def wait_for_login_redirect(page, timeout_seconds: int = 60) -> None:
    """Wait for authentication, reporting an interactive challenge explicitly."""
    deadline = time.monotonic() + timeout_seconds
    logger.debug("Waiting up to %ss for login redirect; checking for visible hCaptcha.", timeout_seconds)
    challenge = page.locator('iframe[src*="hcaptcha.com"], iframe[title*="hCaptcha"]')
    while time.monotonic() < deadline:
        if re.match(r"^https://octopus\.energy(?:/|$)", page.url):
            logger.debug("Login redirected to octopus.energy.")
            return
        if any(challenge.nth(index).is_visible() for index in range(challenge.count())):
            raise RuntimeError(
                "Octopus login requires an interactive hCaptcha; headless Playwright cannot continue"
            )
        page.wait_for_timeout(250)
    raise RuntimeError(
        f"Octopus login did not redirect to octopus.energy within {timeout_seconds} seconds"
    )


def prepare_signup(page, tariff: Tariff) -> dict:
    """Select the requested variant and accept any displayed signup terms."""
    flow = SIGNUP_FLOWS[tariff.id]
    checks = {"terms_checked": False, "variable_selected": False}
    log_stage(f"waiting for {tariff.id} Switch Tariff button")
    page.get_by_role(
        "button", name=re.compile(r"^Switch Tariff$", re.IGNORECASE)
    ).wait_for()
    if flow.option:
        log_stage(f"selecting {tariff.id} {flow.option} option")
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

    log_stage(f"checking {tariff.id} signup terms")
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
    logger.info("Website signup ready for %s: terms_checked=%s, variable_selected=%s.",
                tariff.id, checks["terms_checked"], checks["variable_selected"])
    return checks


def is_login_required(page) -> bool:
    url = getattr(page, "url", "")
    if isinstance(url, str) and re.match(r"^https://auth\.octopus\.energy/login(?:/|\?|$)", url):
        return True
    try:
        count_val = page.locator("#id_auth-username").count()
        return isinstance(count_val, int) and count_val > 0
    except Exception as exc:
        # Failure to inspect the page is not evidence of an authenticated session.
        log_failure(logger, "Checking whether website login is required", exc)
        raise


def login_and_verify_account(page, account_number: str, email: str, password: str) -> None:
    """Reuse or establish a website session and verify the configured account."""
    if not account_number:
        raise ValueError("Website login requires ACC_NUMBER")
    step = log_stage("opening dashboard login entry")
    try:
        page.goto("https://octopus.energy/dashboard/")
        page.wait_for_load_state("domcontentloaded")
        step = log_stage("checking whether login is required")
        if is_login_required(page):
            logger.info("Octopus website authentication required; signing in.")
            step = log_stage("waiting for Octopus authentication page")
            page.wait_for_url(
                re.compile(r"^https://auth\.octopus\.energy/login(?:/|\?|$)")
            )
            step = log_stage("entering email")
            page.locator("#id_auth-username").fill(email)
            step = log_stage("entering password")
            page.locator("#id_auth-password").fill(password)
            step = log_stage("submitting login")
            page.locator("#submit-button").click()
            step = log_stage("waiting for login redirect")
            wait_for_login_redirect(page)
            step = log_stage("opening dashboard")
            page.goto("https://octopus.energy/dashboard/")
            page.wait_for_load_state("domcontentloaded")
        else:
            logger.info("Reusing existing authenticated session from persistent browser profile.")

        step = log_stage("verifying dashboard account")
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
    except (Error, InvisiblePlaywrightError) as exc:
        # Playwright call logs may contain the filled password.
        log_failure(logger, f"Octopus website login failed while {step}", exc)
        save_failure_screenshot(page, "login")
        raise RuntimeError(
            f"Octopus website login failed while {step}: {error_summary(exc)}. See logs/octobot.log."
        ) from None
    except Exception as exc:
        log_failure(logger, f"Octopus website login failed while {step}", exc)
        save_failure_screenshot(page, "login")
        raise
    logger.info(
        "Logged in to Octopus website and verified configured account."
    )


@contextmanager
def logged_in_page(account_number: str, email: str, password: str):
    """Share the same login and browser lifecycle for checks and real switches, reusing persistent session."""
    profile_dir = Path("logs") / "browser_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Starting headless Invisible Playwright with persistent browser profile.")
    with InvisiblePlaywright(profile_dir=profile_dir, headless=True) as browser:
        try:
            is_context = hasattr(browser, "new_page") and not hasattr(browser, "new_context")
            logger.debug("Browser launched; persistent_context=%s.", is_context)
            if is_context:
                context = browser
            else:
                context = browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    locale="en-GB",
                    timezone_id="Europe/London",
                )
            try:
                pages = getattr(context, "pages", None)
                if isinstance(pages, list) and len(pages) > 0:
                    page = pages[0]
                else:
                    page = context.new_page()
                page.set_default_timeout(60_000)
                login_and_verify_account(page, account_number, email, password)
                yield page
            finally:
                logger.debug("Closing website browser context.")
                if not is_context:
                    context.close()
        finally:
            logger.debug("Closing Invisible Playwright browser.")
            browser.close()


def prewarm_browser_login(account_number: str, email: str, password: str) -> str:
    """Verify login and close the browser to persist its session before returning."""
    if not account_number or not email or not password:
        raise RuntimeError(
            "Browser login requires ACC_NUMBER, OCTOPUS_LOGIN_EMAIL and OCTOPUS_LOGIN_PASSWD"
        )
    with logged_in_page(account_number, email, password):
        pass
    return account_number


def open_signup(page, tariff: Tariff, account_number: str) -> None:
    slug = SIGNUP_FLOWS[tariff.id].slug
    logger.info("Opening %s website signup.", tariff.id)
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
                # Checks visibility and enabled state without clicking.
                switch_button = page.get_by_role(
                    "button", name=re.compile(r"^Switch Tariff$", re.IGNORECASE)
                )
                switch_button.wait_for(state="visible")
                if not switch_button.is_enabled():
                    raise RuntimeError("Switch Tariff button is disabled")
                save_stage_screenshot(page, identifier)
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
                    "error": error_summary(exc),
                }
                logger.error(
                    "TEST_PLAYWRIGHT FAIL %s: %s; no switch submitted.",
                    identifier,
                    error_summary(exc),
                )
                log_failure(logger, f"TEST_PLAYWRIGHT {identifier} signup check", exc)
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

    with logged_in_page(account_number, email, password) as page:
        stage = log_stage(f"opening {tariff.id} signup")
        try:
            open_signup(page, tariff, account_number)
            stage = log_stage(f"preparing {tariff.id} signup controls")
            prepare_signup(page, tariff)
            stage = "tariff submission (check your account before retrying)"
            logger.info("Submitting %s website tariff switch.", tariff.id)
            page.get_by_role(
                "button", name=re.compile(r"^Switch Tariff$", re.IGNORECASE)
            ).click()
            stage = "verifying enrolment after submission (check your account before retrying)"
            logger.info("Website switch clicked; waiting for exact target product enrolment.")
            enrolment_id = wait_for_enrolment()
            logger.info("Website tariff initiation verified for %s.", tariff.id)
            return enrolment_id
        except (Error, InvisiblePlaywrightError) as exc:
            log_failure(logger, f"Octopus website failed during {stage}", exc)
            save_failure_screenshot(page, tariff.id)
            raise RuntimeError(
                f"Octopus website failed during {stage}: {error_summary(exc)}. See logs/octobot.log."
            ) from None
        except Exception as exc:
            log_failure(logger, f"Octopus website failed during {stage}", exc)
            save_failure_screenshot(page, tariff.id)
            raise
