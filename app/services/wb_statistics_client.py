"""
WB Statistics API Client.
Handles requests to https://statistics-api.wildberries.ru for the sales report.
"""
from typing import Any, Dict, List

import httpx


class WBStatisticsAPIError(Exception):
    """Base exception for WB Statistics API."""


class WBStatisticsClient:
    """Client for Wildberries Statistics API."""
    BASE_URL = "https://statistics-api.wildberries.ru"

    def __init__(self, api_token: str, timeout: float = 60.0):
        self.headers = {"Authorization": api_token.strip(), "Accept": "application/json"}
        self.timeout = timeout

    async def get_sales(self, date_from: str) -> List[Dict[str, Any]]:
        """GET /api/v1/supplier/sales - sales (saleID S...) and returns (saleID R...) changed since date_from."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.BASE_URL}/api/v1/supplier/sales",
                headers=self.headers,
                params={"dateFrom": date_from, "flag": 0},
            )
        if resp.status_code == 204:
            return []
        if resp.status_code != 200:
            raise WBStatisticsAPIError(f"API Error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return data if isinstance(data, list) else []
