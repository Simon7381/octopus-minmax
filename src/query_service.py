import logging
import time

import requests

from diagnostics import error_summary
from queries import *

logger = logging.getLogger("octobot.query_service")
MAX_RETRIES = 5
BASE_WAIT_BEFORE_RETRY_SECONDS = 30


class GQLAuthorizationError(Exception):
    """The authenticated viewer lacks permission; refreshing/retrying cannot help."""


class QueryService:
    _shared_token = None

    def __init__(self, api_key: str, base_url: str):
        logger.debug(f"Initialising {__class__.__name__}")
        self.base_url = base_url
        self.api_key = api_key
        self.headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
        }
        self.graphql_endpoint = f"{self.base_url}/graphql/"

        if QueryService._shared_token is None:
            QueryService._shared_token = self._get_token()

    def _get_token(self):
        logger.debug("Getting token")
        formatted_token_query = token_query.format(api_key=self.api_key)
        headers = self.headers.copy()
        payload = {"query": formatted_token_query, "variables": {}}

        try:
            response = requests.post(
                self.graphql_endpoint, headers=headers, json=payload, timeout=60
            )

            response.raise_for_status()
            result = response.json()
            logger.debug("Token request response: HTTP %s.", response.status_code)

            if "errors" in result:
                raise Exception("GraphQL token request returned errors")

            token = result.get("data", {}).get("obtainKrakenToken", {}).get("token")

            if not token:
                raise Exception("GQL token missing from response")

            logger.info("Acquired Octopus API token.")
            return token
        except Exception as e:
            logger.error("Failed to get token: %s", error_summary(e))
            raise Exception("Failed to get token")

    def execute_gql_query(self, query: str):
        logger.debug("Executing GraphQL request (query and response bodies omitted).")
        retry = 0
        token_refreshed = False
        while retry < MAX_RETRIES:
            headers = self.headers.copy()

            if self._shared_token:
                headers["Authorization"] = self._shared_token

            payload = {"query": query, "variables": {}}
            try:
                response = requests.post(
                    self.graphql_endpoint, headers=headers, json=payload, timeout=60
                )

                logger.debug("GraphQL response: HTTP %s, attempt %s/%s.",
                             response.status_code, retry + 1, MAX_RETRIES)
                if response.ok:
                    result = response.json()
                    if "errors" in result:
                        error_codes = [
                            e.get("extensions", {}).get("errorCode")
                            for e in result.get("errors", [])
                        ]
                        logger.debug("GraphQL error codes: %s", error_codes)
                        if "KT-CT-1111" in error_codes:
                            raise GQLAuthorizationError(
                                "GQL permission denied (KT-CT-1111)"
                            )
                        if "KT-CT-1124" in error_codes and not token_refreshed:
                            logger.debug("JWT expired, refreshing token...")
                            try:
                                QueryService._shared_token = self._get_token()
                                token_refreshed = True
                                continue  # Retry with new token
                            except Exception as e:
                                logger.warning("Failed to refresh token: %s", error_summary(e))
                        raise Exception(f"GQL error codes: {error_codes}")

                    data = result.get("data")
                    if data and isinstance(data, dict) and len(data) > 0:
                        return data
                    else:
                        raise Exception("No 'data' returned from GraphQL query")

                if response.status_code in [401, 403] and not token_refreshed:
                    logger.debug("Authentication failed, refreshing token...")
                    try:
                        QueryService._shared_token = self._get_token()
                        token_refreshed = True
                        continue

                    except Exception as e:
                        logger.warning("Failed to refresh token: %s", error_summary(e))

            except GQLAuthorizationError:
                raise
            except Exception as e:
                logger.warning(
                    f"Request exception on attempt {retry + 1}/{MAX_RETRIES}: {error_summary(e)}"
                )
                if retry == MAX_RETRIES - 1:
                    raise Exception(
                        f"GQL query failed after {MAX_RETRIES} attempts: {error_summary(e)}"
                    )

            if retry == MAX_RETRIES - 1:
                logger.warning(
                    f"GQL query failed after {MAX_RETRIES} attempts: HTTP {response.status_code}"
                )
                raise Exception(
                    f"GQL query failed after {MAX_RETRIES} attempts: HTTP {response.status_code}"
                )

            # Calculate wait time with exponential backoff
            wait_time = BASE_WAIT_BEFORE_RETRY_SECONDS * (2**retry)
            logger.debug(
                f"Request failed. Retrying in {wait_time} seconds... (attempt {retry + 1}/{MAX_RETRIES})"
            )
            retry += 1
            time.sleep(wait_time)

    def execute_rest_query(self, url: str):
        logger.debug("Executing REST request.")
        try:
            response = requests.get(url, timeout=60)
            logger.debug("REST response: HTTP %s.", response.status_code)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error("REST request failed: %s", error_summary(e))
            raise Exception(
                f"ERROR: REST request failed: {error_summary(e)}"
            )
