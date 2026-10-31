"""
Wildberries Analytics API Client.
Handles requests to https://seller-analytics-api.wildberries.ru for excise report & marked goods.
"""
import asyncio
from datetime import date, datetime
import logging
from typing import Any, Dict, List, Optional, Union
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


class WBAnalyticsAPIError(Exception):
    """Base exception for WB Analytics API."""


class WBAnalyticsRateLimitError(WBAnalyticsAPIError):
    """429 Too Many Requests."""


class WBAnalyticsUnauthorizedError(WBAnalyticsAPIError):
    """401 Unauthorized."""


RETRY_POLICY = dict(
    retry=retry_if_exception_type(WBAnalyticsRateLimitError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=3, max=30),
    reraise=True,
)


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

    @retry(**RETRY_POLICY)
    async def get_excise_report(
        self,
        date_from: str,
        date_to: str,
        countries: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """POST /api/v1/analytics/excise-report - KIZ / SGTIN sales & receipts report."""
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        should_close = self._client is None
        url = f"{self.BASE_URL}/api/v1/analytics/excise-report"
        params = {"dateFrom": date_from, "dateTo": date_to}
        body = {"countries": countries} if countries else {}

        try:
            resp = await client.post(url, headers=self.headers, params=params, json=body)
            if resp.status_code == 429:
                hdr = resp.headers.get("x-ratelimit-retry") or resp.headers.get("X-RateLimit-Retry") or resp.headers.get("Retry-After")
                wait_sec = int(hdr) if hdr and hdr.isdigit() else 35
                if wait_sec > 60:
                    raise WBAnalyticsRateLimitError(f"Лимит запросов WB исчерпан. Сервер вернет доступ через {wait_sec} сек.")
                logger.warning(f"[WB Analytics] 429 Rate limit hit, waiting {wait_sec}s per server header...")
                await asyncio.sleep(wait_sec + 2)
                resp = await client.post(url, headers=self.headers, params=params, json=body)
                if resp.status_code == 429:
                    raise WBAnalyticsRateLimitError(f"Rate limit exceeded (429) after {wait_sec}s wait")
            if resp.status_code == 401:
                logger.error("[WB Analytics] 401 Unauthorized on excise-report")
                raise WBAnalyticsUnauthorizedError("WB token invalid or expired (401)")
            elif resp.status_code >= 400:
                err_text = resp.text[:300]
                logger.error(f"[WB Analytics] HTTP {resp.status_code}: {err_text}")
                raise WBAnalyticsAPIError(f"API Error {resp.status_code}: {err_text}")
            if resp.status_code == 204:
                return []

            res_json = resp.json()
            return res_json.get("response", {}).get("data", [])
        finally:
            if should_close and not client.is_closed:
                await client.aclose()

    @retry(**RETRY_POLICY)
    async def get_item_returns(
        self,
        date_from: Union[str, datetime, date],
        date_to: Union[str, datetime, date],
        nm_ids: Optional[List[int]] = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """GET /api/analytics/v1/item-returns - Buyer returns with flexible wrapping."""
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        should_close = self._client is None
        url = f"{self.BASE_URL}/api/analytics/v1/item-returns"
        df_str = date_from.strftime("%Y-%m-%d") if hasattr(date_from, "strftime") else str(date_from)
        dt_str = date_to.strftime("%Y-%m-%d") if hasattr(date_to, "strftime") else str(date_to)
        params: Dict[str, Any] = {"dateFrom": df_str, "dateTo": dt_str, "limit": limit, "offset": offset}
        if nm_ids:
            params["nmIds"] = ",".join(str(x) for x in nm_ids) if isinstance(nm_ids, (list, tuple, set)) else str(nm_ids)

        try:
            resp = await client.get(url, headers=self.headers, params=params)
            if resp.status_code == 429:
                hdr = resp.headers.get("x-ratelimit-retry") or resp.headers.get("X-RateLimit-Retry") or resp.headers.get("Retry-After")
                wait_sec = int(hdr) if hdr and hdr.isdigit() else 35
                if wait_sec > 60:
                    raise WBAnalyticsRateLimitError(f"Лимит запросов WB исчерпан. Сервер вернет доступ через {wait_sec} сек.")
                logger.warning(f"[WB Analytics] 429 Rate limit hit, waiting {wait_sec}s per server header...")
                await asyncio.sleep(wait_sec + 2)
                resp = await client.get(url, headers=self.headers, params=params)
                if resp.status_code == 429:
                    raise WBAnalyticsRateLimitError(f"Rate limit exceeded (429) after {wait_sec}s wait")
            if resp.status_code == 401:
                logger.error("[WB Analytics] 401 Unauthorized on item-returns")
                raise WBAnalyticsUnauthorizedError("WB token invalid or expired (401)")
            elif resp.status_code >= 400:
                err_text = resp.text[:300]
                logger.error(f"[WB Analytics] HTTP {resp.status_code}: {err_text}")
                raise WBAnalyticsAPIError(f"API Error {resp.status_code}: {err_text}")
            if resp.status_code == 204:
                # WB answers 204 No Content when the seller has no returns for the period
                return []

            try:
                res_json = resp.json()
            except Exception as e:
                logger.error(f"[WB Analytics] Failed to parse JSON response (HTTP {resp.status_code}): {e}")
                raise WBAnalyticsAPIError(f"Failed to parse JSON response (HTTP {resp.status_code}): {e}")

            if isinstance(res_json, dict):
                if "response" in res_json and isinstance(res_json["response"], dict):
                    return res_json["response"].get("data") or []
                return res_json.get("data") or []
            elif isinstance(res_json, list):
                return res_json
            return []
        finally:
            if should_close and not client.is_closed:
                await client.aclose()

    @retry(**RETRY_POLICY)
    async def _get_item_returns_page(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """One page of the item returns report: {"count": N, "report": [...]}."""
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        should_close = self._client is None
        url = f"{self.BASE_URL}/api/analytics/v1/item-returns"
        try:
            resp = await client.get(url, headers=self.headers, params=params)
            if resp.status_code == 429:
                raise WBAnalyticsRateLimitError("Rate limit exceeded (429) on item-returns")
            if resp.status_code == 401:
                raise WBAnalyticsUnauthorizedError("WB token invalid or expired (401)")
            if resp.status_code >= 400:
                raise WBAnalyticsAPIError(f"API Error {resp.status_code}: {resp.text[:300]}")
            if resp.status_code == 204:
                return []
            try:
                return resp.json().get("report") or []
            except (ValueError, AttributeError) as e:
                raise WBAnalyticsAPIError(f"Failed to parse JSON response (HTTP {resp.status_code}): {e}")
        finally:
            if should_close and not client.is_closed:
                await client.aclose()

    async def get_item_returns_report(self, date_from: str, date_to: str, status: str, page_size: int = 1000) -> List[Dict[str, Any]]:
        """All rows of the item returns report for one status: active or archive."""
        rows: List[Dict[str, Any]] = []
        while True:
            params = {"dateFrom": date_from, "dateTo": date_to, "status": status, "limit": page_size, "offset": len(rows)}
            page = await self._get_item_returns_page(params)
            rows.extend(page)
            if len(page) < page_size:
                return rows
