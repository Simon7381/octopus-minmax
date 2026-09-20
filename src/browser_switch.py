"""Website fallback for tariff initiation; agreement acceptance stays in the API."""

import logging
import platform
import re
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from invisible_playwright import InvisiblePlaywright
# Invisible Playwright uses its own client and exception hierarchy.
from invisible_playwright._pw.sync_api import Error as InvisiblePlaywrightError, expect
from playwright.sync_api import Error

from diagnostics import error_summary, is_navigation_context_error, log_failure
from tariff import TARIFFS, Tariff

logger = logging.getLogger("octobot.browser_switch")

# Keep the browser identity stable alongside its persisted cookies. CPU/display
# values match the Windows desktop used for testing (Ryzen 9 5900X, RX 6900 XT).
# Invisible Playwright 0.22.2 only accepts validated GPU personas; it has no
# RX 6900 XT persona, so use its supported modern AMD renderer bucket.
BROWSER_SEED = 101
DESKTOP_VIEWPORT = {"width": 1902, "height": 1398}
DESKTOP_HARDWARE_PINS = {
    "gpu.vendor": "Google Inc. (AMD)",
    "gpu.renderer": "ANGLE (AMD, Radeon R9 200 Series Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "hardware.concurrency": 24,
    "screen.width": 3840,
    "screen.height": 1600,
    # Available height is derived from the screen and the Windows taskbar.
    "screen.taskbar_px": 48,
    "screen.dpr": 1.0,
}
LOGIN_FIELD_TIMEOUT_MS = 120_000
CREDENTIAL_MASK_SELECTOR = (
    'input[type="password"], input[type="email"], '
    '#id_auth-username, #id_auth-password, input[autocomplete="username"]'
)


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
    _save_diagnostic_screenshot(page, name, "stage")


def save_failure_screenshot(page, name: str) -> None:
    _save_diagnostic_screenshot(page, name, "failure")


def _save_diagnostic_screenshot(page, name: str, kind: str) -> None:
    try:
        screenshot = Path("logs") / f"playwright-{name}-{kind}.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        # Invisible Playwright 0.23.0 ignores screenshot(mask=...). Hide the
        # controls in the document as well, without changing their values.
        redaction_id = f"octobot-redaction-{uuid4().hex}"
        page.evaluate(
            """mask => {
                const style = document.createElement('style');
                style.id = mask.id;
                style.textContent = mask.css;
                document.documentElement.appendChild(style);
            }""",
            {"id": redaction_id, "css": f"{CREDENTIAL_MASK_SELECTOR} {{ visibility: hidden !important; }}"},
        )
        try:
            capture = page.screenshot(
                full_page=True,
                mask=[page.locator(CREDENTIAL_MASK_SELECTOR)],
            )
            if not page.evaluate("id => Boolean(document.getElementById(id))", redaction_id):
                raise RuntimeError("Page changed during redacted capture; screenshot discarded")
            screenshot.write_bytes(capture)
        finally:
            page.evaluate("id => document.getElementById(id)?.remove()", redaction_id)
        logger.info("Playwright %s screenshot saved to %s", kind, screenshot)
    except Exception as exc:
        logger.warning("Could not capture Playwright %s screenshot: %s", kind, error_summary(exc))


def wait_for_login_redirect(page, timeout_seconds: int = 60) -> None:
    """Wait for authentication, reporting an interactive challenge explicitly."""
    deadline = time.monotonic() + timeout_seconds
    logger.debug("Waiting up to %ss for login redirect; checking for visible hCaptcha.", timeout_seconds)
    challenge = page.locator('iframe[src*="hcaptcha.com"], iframe[title*="hCaptcha"]')
    while time.monotonic() < deadline:
        if re.match(r"^https://octopus\.energy(?:/|$)", page.url):
            logger.debug("Login redirected to octopus.energy.")
            return
        try:
            challenge_visible = any(challenge.nth(index).is_visible() for index in range(challenge.count()))
        except (Error, InvisiblePlaywrightError) as exc:
            if not is_navigation_context_error(exc):
                raise
            # Authentication can replace the document between reading the URL
            # and inspecting challenge frames. Keep polling; never resubmit.
            logger.debug("Login page navigated during challenge check; waiting for the new document.")
            challenge_visible = False
        if challenge_visible:
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


def fill_login_field(page, selector: str, value: str, label: str) -> None:
    """Replace a credential in one input operation and check it without logging it."""
    for attempt in range(2):
        field = page.locator(selector)
        try:
            field.wait_for(state="visible", timeout=LOGIN_FIELD_TIMEOUT_MS)
            expect(field).to_be_editable(timeout=LOGIN_FIELD_TIMEOUT_MS)
            # Invisible Playwright's fill() types character by character. Select
            # the entire existing value and insert once, including on a retry.
            field.press("ControlOrMeta+A", timeout=LOGIN_FIELD_TIMEOUT_MS)
            page.keyboard.insert_text(value)
            if field.input_value(timeout=LOGIN_FIELD_TIMEOUT_MS) != value:
                raise RuntimeError(f"{label} entry was incomplete; login was not submitted")
            logger.debug("%s entry verified.", label)
            return
        except AssertionError:
            # Locator assertions can include DOM snippets; keep credentials out
            # of callers that display the exception message (e.g. test mode).
            raise RuntimeError(f"{label} field did not become editable; login was not submitted") from None
        except (Error, InvisiblePlaywrightError, RuntimeError) as exc:
            if attempt == 1 or page.is_closed():
                raise
            logger.warning("Retrying %s entry before login submission: %s", label, error_summary(exc))


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
            fill_login_field(page, "#id_auth-username", email, "Email")
            step = log_stage("entering password")
            fill_login_field(page, "#id_auth-password", password, "Password")
            step = log_stage("verifying login fields before submission")
            if (
                page.locator("#id_auth-username").input_value(timeout=LOGIN_FIELD_TIMEOUT_MS) != email
                or page.locator("#id_auth-password").input_value(timeout=LOGIN_FIELD_TIMEOUT_MS) != password
            ):
                raise RuntimeError("Login fields changed before submission; login was not submitted")
            step = log_stage("submitting login")
            page.locator("#submit-button").click()
            step = log_stage("waiting for login redirect")
            wait_for_login_redirect(page)
            step = log_stage("opening dashboard")
            page.wait_for_load_state("domcontentloaded")
            if not re.match(r"^https://octopus\.energy/dashboard(?:/|$)", page.url):
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
            step = log_stage("waiting for dashboard account details")
            # The dashboard shell can load before its account data on slow hosts.
            page.wait_for_function(
                """account => location.href.toLowerCase().includes(account.toLowerCase())
                    || document.documentElement.innerHTML.toLowerCase().includes(account.toLowerCase())""",
                arg=account_number, timeout=LOGIN_FIELD_TIMEOUT_MS,
            )
            if (
                account_number.lower() not in page.url.lower()
                and account_number.lower() not in page.content().lower()
            ):
                raise RuntimeError("authenticated dashboard does not contain the configured account")
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
    logger.debug(
        "Browser runtime: invisible-playwright=%s; Python=%s; architecture=%s.",
        version("invisible-playwright"), platform.python_version(), platform.machine(),
    )
    with InvisiblePlaywright(
        profile_dir=profile_dir,
        headless=True,
        seed=BROWSER_SEED,
        pin=DESKTOP_HARDWARE_PINS.copy(),
        locale="en-GB",
        timezone="Europe/London",
    ) as browser:
        try:
            is_context = hasattr(browser, "new_page") and not hasattr(browser, "new_context")
            logger.debug("Browser launched; persistent_context=%s.", is_context)
            if is_context:
                context = browser
            else:
                # The wrapper derives screen, locale and timezone from the pins
                # and launch options for both persistent and ordinary contexts.
                context = browser.new_context()
            try:
                step = log_stage("checking existing browser pages")
                pages = getattr(context, "pages", None)
                if isinstance(pages, list) and len(pages) > 0:
                    page = pages[0]
                else:
                    step = log_stage("creating website browser page")
                    page = context.new_page()
                # Viewport is the page's content area, not the physical monitor.
                # Apply it to reused pages too, before any login navigation.
                step = log_stage("setting website browser viewport")
                page.set_viewport_size(DESKTOP_VIEWPORT.copy())
                page.set_default_timeout(60_000)
            except Exception as exc:
                log_failure(logger, f"Preparing website browser failed while {step}", exc)
                raise
            try:
                login_and_verify_account(page, account_number, email, password)
                yield page
            finally:
                logger.debug("Closing website browser context.")
                if not is_context:
                    context.close()
        finally:
            # The InvisiblePlaywright context manager owns its persistent
            # context/browser and closes it on exit. Do not close it twice.
            logger.debug("Closing Invisible Playwright session through its context manager.")


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
