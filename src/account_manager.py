import logging
import time
from datetime import date, datetime
from typing import Optional

import config
from account_info import AccountInfo
from browser_switch import SIGNUP_FLOWS, initiate_browser_switch
from queries import (
    accept_terms_query,
    account_query,
    consumption_query,
    enrolment_query,
    get_terms_version_query,
    switch_query,
)
from query_service import QueryService
from tariff import Tariff

logger = logging.getLogger("octobot.account_manager")


class AccountManager:
    _instance: Optional["AccountManager"] = None

    def __init__(self, query_service: QueryService, available_tariffs: list[Tariff]):
        """
        Initializes the AccountManager. This should only be called once via get_instance.
        Args:
            query_service: The service instance for executing API queries.
            available_tariffs: A list of all Tariff objects that the system knows about,
            used to match against the account's current tariff.
        """
        logger.debug(f"Initialising {__class__.__name__}")
        if hasattr(self, "_initialized") and self._initialized:
            return

        self.query_service: QueryService = query_service
        self.config = config
        self.available_tariffs: list[Tariff] = available_tariffs

        self._current_account_info: AccountInfo | None = None
        self.mpan: str | None = None
        self.device_id: str | None = None
        self.region_code: str | None = None
        self._website_accepted_enrolments = set()

        self._initialized: bool = True

    @classmethod
    def get_instance(
        cls, query_service: QueryService = None, available_tariffs: list[Tariff] = None
    ) -> "AccountManager":
        """
        Gets the singleton instance of AccountManager.
        The query_service and available_tariffs must be provided on the first call.
        """
        if cls._instance is None:
            if query_service is None or available_tariffs is None:
                raise ValueError(
                    "QueryService and available_tariffs must be provided for the first instantiation of AccountManager."
                )
            cls._instance = cls(query_service, available_tariffs)
        return cls._instance

    def _get_agreement_terms_version(self, product_code: str) -> dict[str, int]:
        """Fetches the major and minor version of terms and conditions for a product."""
        query = get_terms_version_query.format(product_code=product_code)
        result = self.query_service.execute_gql_query(query)
        terms_version_str = (
            result.get("termsAndConditionsForProduct", {})
            .get("version", "1.0")
            .split(".")
        )
        return {"major": int(terms_version_str[0]), "minor": int(terms_version_str[1])}

    def fetch_current_account_info(self) -> AccountInfo:
        """
        Fetches comprehensive information about the current electricity account,
        including tariff, consumption, MPAN, and device ID.
        Stores key details like MPAN, device ID, and region code as instance attributes.
        """
        query = account_query.format(acc_number=self.config.ACC_NUMBER)
        result = self.query_service.execute_gql_query(query)

        import_agreement = None
        for agreement in result.get("account", {}).get("electricityAgreements", []):
            meter_point_data = agreement.get("meterPoint", {})
            if meter_point_data.get("direction") == "IMPORT":
                import_agreement = agreement
                break

        if not import_agreement:
            raise Exception("ERROR: No IMPORT meter point found in account data")

        tariff_data = import_agreement.get("tariff")
        if not tariff_data:
            raise Exception("ERROR: No tariff information found for the IMPORT meter")

        tariff_code = tariff_data.get("tariffCode")
        if not tariff_code:
            raise Exception("ERROR: No tariff code found for the IMPORT tariff")

        current_standing_charge = tariff_data.get("standingCharge")
        # A standing charge can be 0.0, so check for None explicitly
        if current_standing_charge is None:
            raise Exception(
                "ERROR: No standing charge found for the IMPORT meter tariff"
            )

        self.region_code = tariff_code[-1]

        meter_point_details = import_agreement.get("meterPoint", {})
        self.mpan = meter_point_details.get("mpan")
        if not self.mpan:
            raise Exception("ERROR: No MPAN found for the IMPORT meter")

        # Reset device_id before trying to find it
        self.device_id = None
        for meter in meter_point_details.get("meters", []):
            for device in meter.get("smartDevices", []):
                if "deviceId" in device:
                    self.device_id = device["deviceId"]
                    break
            if self.device_id:
                break

        if not self.device_id:
            raise Exception("ERROR: No device ID found for the IMPORT meter")

        matching_tariff_obj = next(
            (
                tariff
                for tariff in self.available_tariffs
                if tariff.is_tariff(tariff_code)
            ),
            None,
        )
        if matching_tariff_obj is None:
            raise Exception(
                f"ERROR: Found no supported tariff object for '{tariff_code}' among available tariffs."
            )

        # Get consumption for today
        consumption_gql_query = consumption_query.format(
            device_id=self.device_id,
            start_date=f"{date.today()}T00:00:00Z",
            end_date=f"{date.today()}T23:59:59Z",
        )
        consumption_result = self.query_service.execute_gql_query(consumption_gql_query)
        consumption_data = consumption_result.get("smartMeterTelemetry", [])

        self._current_account_info = AccountInfo(
            current_tariff=matching_tariff_obj,
            standing_charge=current_standing_charge,
            region_code=self.region_code,
            consumption=consumption_data,
            mpan=self.mpan,
        )
        return self._current_account_info

    def initiate_tariff_switch(self, target_product_code: str) -> str | None:
        """Initiates the process of switching to a new electricity tariff."""
        if not self.mpan:
            # Attempt to fetch account details if MPAN is not already set
            logger.info("MPAN not readily available, fetching account details first...")
            self.fetch_current_account_info()
            if not self.mpan:
                raise Exception(
                    "ERROR: MPAN could not be determined. Cannot switch tariff."
                )

        change_date = date.today()
        query = switch_query.format(
            account_number=self.config.ACC_NUMBER,
            mpan=self.mpan,
            product_code=target_product_code,
            change_date=change_date.isoformat(),  # Ensure date is in YYYY-MM-DD format
        )
        try:
            logger.info("Requesting API tariff initiation for %s.", target_product_code)
            result = self.query_service.execute_gql_query(query)
            enrolment = (result.get("startOnboardingProcess") or {}).get(
                "productEnrolment"
            ) or {}
            if not enrolment.get("id"):
                raise RuntimeError("Tariff initiation returned no enrolment ID")
            logger.info("API tariff initiation succeeded for %s.", target_product_code)
            return enrolment["id"]
        except Exception as exc:
            logger.warning("API tariff initiation failed (%s); checking website fallback for %s.", type(exc).__name__, target_product_code)
            return self._initiate_tariff_switch_in_browser(target_product_code)

    def _fetch_enrolments(self) -> list[dict]:
        result = self.query_service.execute_gql_query(
            enrolment_query.format(acc_number=self.config.ACC_NUMBER)
        )
        entries = result.get("productEnrolments")
        if not isinstance(entries, list):
            raise RuntimeError(
                "Cannot verify tariff switch: product enrolments are unavailable"
            )
        return entries

    @staticmethod
    def _matching_enrolment(entries: list[dict], product_code: str) -> str | None:
        matches = [
            entry["id"]
            for entry in entries
            if entry.get("id")
            and entry.get("status") == "IN_PROGRESS"
            and (entry.get("product") or {}).get("code") == product_code
        ]
        if len(matches) > 1:
            raise RuntimeError(
                "Multiple matching tariff enrolments; check your Octopus account"
            )
        return matches[0] if matches else None

    def _initiate_tariff_switch_in_browser(self, target_product_code: str) -> str:
        tariff = next(
            (
                t
                for t in self.available_tariffs
                if t.product_code == target_product_code
            ),
            None,
        )
        if tariff is None:
            raise ValueError("Cannot find the target tariff for the website fallback")
        if tariff.id not in SIGNUP_FLOWS:
            raise ValueError(
                f"Tariff '{tariff.id}' supports GraphQL switching only; no website fallback is available"
            )
        logger.info("Using website fallback for tariff '%s'.", tariff.id)

        # A failed API response may still have created an enrolment. Reuse it.
        before = self._fetch_enrolments()
        logger.debug("Found %s enrolments before website submission.", len(before))
        pending_id = self._matching_enrolment(before, target_product_code)
        if pending_id:
            logger.info("Using existing in-progress enrolment for the target product.")
            return pending_id
        previous_ids = {entry.get("id") for entry in before}

        def wait_for_enrolment():
            for attempt in range(13):
                logger.debug("Polling target product enrolment: attempt %s/13, product=%s.", attempt + 1, target_product_code)
                entries = [
                    entry
                    for entry in self._fetch_enrolments()
                    if entry.get("id") not in previous_ids
                ]
                completed = [
                    entry
                    for entry in entries
                    if entry.get("id")
                    and (entry.get("product") or {}).get("code") == target_product_code
                    and (
                        entry.get("status") == "COMPLETED"
                        or any(
                            stage.get("name") == "post-enrolment"
                            and stage.get("status") == "COMPLETED"
                            for stage in (entry.get("stages") or [])
                        )
                    )
                ]
                if len(completed) > 1:
                    raise RuntimeError(
                        "Multiple completed target enrolments; check your Octopus account"
                    )
                if completed:
                    logger.info("Target product enrolment completed on the website.")
                    enrolment_id = completed[0]["id"]
                    self._website_accepted_enrolments.add(
                        (target_product_code, enrolment_id)
                    )
                    return enrolment_id
                enrolment_id = self._matching_enrolment(entries, target_product_code)
                if enrolment_id:
                    logger.info("Found new in-progress enrolment for target product.")
                    return enrolment_id
                if attempt < 12:
                    time.sleep(10)
            raise RuntimeError(
                "Website switch submitted, but no new enrolment for the exact "
                "target product appeared within two minutes. Check your Octopus account "
                "and emails before trying again (the website may have auto-accepted)."
            )

        return initiate_browser_switch(
            tariff,
            self.config.ACC_NUMBER,
            self.config.OCTOPUS_LOGIN_EMAIL,
            self.config.OCTOPUS_LOGIN_PASSWD,
            wait_for_enrolment,
        )

    def accept_new_agreement(self, product_code: str, enrolment_id: str) -> str | None:
        logger.info("Checking agreement acceptance for %s.", product_code)
        if (product_code, enrolment_id) in self._website_accepted_enrolments:
            return "already accepted on website"
        # get terms and conditions version
        version = self._get_agreement_terms_version(product_code)
        # accept terms and conditions
        query = accept_terms_query.format(
            account_number=self.config.ACC_NUMBER,
            enrolment_id=enrolment_id,
            version_major=version["major"],
            version_minor=version["minor"],
        )
        result = self.query_service.execute_gql_query(query)
        return result.get("acceptTermsAndConditions", {}).get(
            "acceptedVersion", "unknown version"
        )

    def verify_new_agreement_status(self, product_code: str | None = None) -> bool:
        """Verifies if the new tariff agreement is active as of today."""
        logger.info("Verifying active agreement for %s.", product_code or "current product")
        query = account_query.format(acc_number=self.config.ACC_NUMBER)
        result = self.query_service.execute_gql_query(query)

        today_date = datetime.now().date()
        for agreement in result.get("account", {}).get("electricityAgreements", []):
            if (
                product_code
                and (agreement.get("tariff") or {}).get("productCode") != product_code
            ):
                continue
            meter_point = agreement.get("meterPoint") or {}
            if meter_point.get("direction") != "IMPORT" or (
                self.mpan and meter_point.get("mpan") != self.mpan
            ):
                continue
            valid_from_str = agreement.get("validFrom")
            if valid_from_str:
                try:
                    # API might return a full datetime string or just a date string
                    if "T" in valid_from_str:
                        agreement_start_date = datetime.fromisoformat(
                            valid_from_str.replace("Z", "+00:00")
                        ).date()
                    else:
                        agreement_start_date = date.fromisoformat(valid_from_str)

                    if agreement_start_date == today_date:
                        return True
                except ValueError:
                    logger.warning(
                        f"Could not parse agreement 'validFrom' date: {valid_from_str}"
                    )
                    continue
        return False
