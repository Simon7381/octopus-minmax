"""Website fallback for tariff initiation; agreement acceptance stays in the API."""

import logging
import re
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlencode

from tariff import Tariff

logger = logging.getLogger('octobot.browser_switch')


@dataclass(frozen=True)
class SignupFlow:
    slug: str
    option: Optional[str] = None
    require_terms: bool = False


SIGNUP_FLOWS = {
    "go": SignupFlow("go", require_terms=True),
    "agile": SignupFlow("agile"),
    "cosy": SignupFlow("cosy-octopus", option="Variable"),
}


def prepare_signup(page, tariff: Tariff) -> None:
    """Select the requested variant and accept any displayed signup terms."""
    flow = SIGNUP_FLOWS[tariff.id]
    page.get_by_role("button", name=re.compile(r"^Switch Tariff$", re.I)).wait_for()
    if flow.option:
        option_name = re.compile(rf"\b{flow.option}\b", re.I)
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
            raise RuntimeError("Cosy signup has no Variable option; switch was not submitted")

    terms = page.get_by_role("checkbox", name=re.compile(r"I accept.*Terms", re.I))
    if flow.require_terms:
        terms.wait_for()
        terms.check()
    elif terms.count():
        terms.check()


def initiate_browser_switch(tariff: Tariff, account_number: str, email: str,
                            password: str, wait_for_enrolment: Callable[[], str]) -> str:
    if tariff.id not in SIGNUP_FLOWS:
        raise ValueError(f"Website fallback is not supported for tariff '{tariff.id}'")
    if not email or not password:
        raise RuntimeError("Website fallback requires OCTOPUS_LOGIN_EMAIL and OCTOPUS_LOGIN_PASSWD")
    if not account_number:
        raise ValueError("Website fallback requires ACC_NUMBER")

    # Lazy import keeps API-only installations usable without a browser runtime.
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.firefox.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": 1920, "height": 1080})
            stage = "login"
            try:
                page = context.new_page()
                page.set_default_timeout(60_000)
                page.goto("https://octopus.energy/")
                page.get_by_label("Log in to my account").click()
                page.get_by_placeholder("Email address").fill(email)
                page.get_by_placeholder("Password", exact=True).fill(password)
                page.get_by_placeholder("Password", exact=True).press("Enter")
                page.wait_for_url(re.compile(r"https://octopus\.energy/dashboard(?:/|$)"))
                logger.info("Logged in to Octopus website for tariff initiation.")

                stage = "tariff selection"
                slug = SIGNUP_FLOWS[tariff.id].slug
                page.goto(f"https://octopus.energy/smart/{slug}/sign-up/?{urlencode({'accountNumber': account_number})}")
                prepare_signup(page, tariff)
                stage = "tariff submission (check your account before retrying)"
                page.get_by_role("button", name=re.compile(r"^Switch Tariff$", re.I)).click()
                # Keep the browser alive while its request completes and Octopus
                # generates the enrolment. A click alone is not success.
                return wait_for_enrolment()
            except Error as exc:
                # Playwright's call log can contain filled credentials.
                raise RuntimeError(f"Octopus website failed during {stage}: {type(exc).__name__}") from None
            finally:
                context.close()
        finally:
            browser.close()
