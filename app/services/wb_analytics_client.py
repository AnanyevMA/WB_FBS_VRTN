"""
Wildberries Analytics API Client.
Handles requests to https://seller-analytics-api.wildberries.ru for excise report & marked goods.
"""
import logging
from typing import Any, Dict, List, Optional
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


class WBAnalyticsAPIError(Exception):
    """Base exception for WB Analytics API."""
    pass


class WBAnalyticsRateLimitError(WBAnalyticsAPIError):
    """429 Too Many Requests."""
    pass


class WBAnalyticsUnauthorizedError(WBAnalyticsAPIError):
    """401 Unauthorized."""
    pass


class WBAnalyticsClient:
    """Client for Wildberries Seller Analytics API."""
    BASE_URL = "https://seller-analytics-api.wildberries.ru"

    def __init__(self, api_token: str, timeout: float = 30.0):
        self.api_token = api_token.strip()
        self.headers = {
            "Authorization": self.api_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @retry(
        retry=retry_if_exception_type(WBAnalyticsRateLimitError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=3, max=30),
        reraise=True,
    )
    async def get_excise_report(
        self,
        date_from: str,
        date_to: str,
        countries: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        POST /api/v1/analytics/excise-report
        Retrieves mandatory labeling items (KIZ / SGTIN) sales & receipts report.
        """
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        should_close = self._client is None

        url = f"{self.BASE_URL}/api/v1/analytics/excise-report"
        params = {"dateFrom": date_from, "dateTo": date_to}
        body = {}
        if countries:
            body["countries"] = countries

        try:
            resp = await client.post(url, headers=self.headers, params=params, json=body)
            if resp.status_code == 429:
                logger.warning("[WB Analytics] 429 Rate limit hit on excise-report")
                raise WBAnalyticsRateLimitError("Rate limit exceeded (429)")
            elif resp.status_code == 401:
                logger.error("[WB Analytics] 401 Unauthorized on excise-report")
                raise WBAnalyticsUnauthorizedError("WB token invalid or expired (401)")
            elif resp.status_code >= 400:
                err_text = resp.text[:300]
                logger.error(f"[WB Analytics] HTTP {resp.status_code}: {err_text}")
                raise WBAnalyticsAPIError(f"API Error {resp.status_code}: {err_text}")

            res_json = resp.json()
            return res_json.get("response", {}).get("data", [])
        finally:
            if should_close and not client.is_closed:
                await client.aclose()
