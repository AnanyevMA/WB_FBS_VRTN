"""
WB Statistics API Client.
Handles requests to https://statistics-api.wildberries.ru for the sales report.
"""
import asyncio
import logging
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

# The statistics API allows one request per minute per token. A wait longer than this is not
# worth holding the user's request open for: the caller reports the report as unavailable instead
MAX_RATE_LIMIT_WAIT = 65


class WBStatisticsAPIError(Exception):
    """Base exception for WB Statistics API."""


class WBStatisticsClient:
    """Client for Wildberries Statistics API."""
    BASE_URL = "https://statistics-api.wildberries.ru"

    def __init__(self, api_token: str, timeout: float = 60.0, transport: httpx.AsyncBaseTransport | None = None):
        self.headers = {"Authorization": api_token.strip(), "Accept": "application/json"}
        self.timeout = timeout
        self._transport = transport

    @staticmethod
    def _retry_after(resp: httpx.Response) -> int:
        for name in ("X-Ratelimit-Retry", "Retry-After"):
            value = resp.headers.get(name)
            if value and value.strip().isdigit():
                return int(value.strip())
        return 60

    async def get_sales(self, date_from: str) -> List[Dict[str, Any]]:
        """GET /api/v1/supplier/sales - sales (saleID S...) and returns (saleID R...) changed since date_from."""
        async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
            params = {"dateFrom": date_from, "flag": 0}
            url = f"{self.BASE_URL}/api/v1/supplier/sales"
            resp = await client.get(url, headers=self.headers, params=params)
            if resp.status_code == 429:
                wait = self._retry_after(resp)
                if wait <= MAX_RATE_LIMIT_WAIT:
                    logger.warning(f"[WB Statistics] 429 on supplier/sales, retrying in {wait + 1}s")
                    await asyncio.sleep(wait + 1)
                    resp = await client.get(url, headers=self.headers, params=params)
        if resp.status_code == 204:
            return []
        if resp.status_code != 200:
            raise WBStatisticsAPIError(f"API Error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return data if isinstance(data, list) else []
