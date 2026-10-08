"""
Wildberries Finance API Client.
Handles requests to https://finance-api.wildberries.ru for weekly realization sales reports.
Method: POST /api/finance/v1/sales-reports/detailed
"""
import asyncio
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


class WBFinanceAPIError(Exception):
    """Base exception for WB Finance API."""
    pass


class WBFinanceRateLimitError(WBFinanceAPIError):
    """429 Too Many Requests."""
    pass


class WBFinanceUnauthorizedError(WBFinanceAPIError):
    """401 Unauthorized."""
    pass


class WBFinanceClient:
    """Client for Wildberries Finance API (Realization sales reports)."""
    BASE_URL = "https://finance-api.wildberries.ru"

    def __init__(self, api_token: str, timeout: float = 60.0):
        self.api_token = api_token.strip()
        self.headers = {
            "Authorization": self.api_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.timeout = httpx.Timeout(timeout, connect=30.0)
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @retry(
        retry=retry_if_exception_type((WBFinanceRateLimitError, httpx.TimeoutException, httpx.NetworkError)),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=3, max=35),
        reraise=True,
    )
    async def get_sales_reports_page(
        self,
        date_from: str,
        date_to: str,
        limit: int = 1000,
        rrd_id: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Запрашивает одну страницу детального отчета реализации WB.
        
        Args:
            date_from: Дата начала периода в формате RFC3339 (например '2026-08-01T00:00:00Z')
            date_to: Дата окончания периода в формате RFC3339 (например '2026-08-31T23:59:59Z')
            limit: Количество записей на странице (макс. 1000)
            rrd_id: ID последней строки предыдущего ответа для пагинации (0 для первой страницы)
        """
        url = f"{self.BASE_URL}/api/finance/v1/sales-reports/detailed"
        body = {
            "dateFrom": date_from,
            "dateTo": date_to,
            "limit": limit,
            "rrdId": rrd_id,
        }

        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        try:
            resp = await client.post(url, headers=self.headers, json=body)

            if resp.status_code == 429:
                retry_header = (
                    resp.headers.get("X-RateLimit-Retry")
                    or resp.headers.get("Retry-After")
                )
                wait_sec = int(retry_header) if retry_header and retry_header.isdigit() else 30
                if wait_sec > 65:
                    raise WBFinanceRateLimitError(
                        f"Лимит запросов WB Finance исчерпан. Повтор через {wait_sec} сек."
                    )
                logger.warning(f"[WB Finance] 429 Rate limit, waiting {wait_sec}s...")
                await asyncio.sleep(wait_sec + 2)
                resp = await client.post(url, headers=self.headers, json=body)

            if resp.status_code == 204:
                return []

            if resp.status_code == 401:
                raise WBFinanceUnauthorizedError(
                    "Недействительный или просроченный токен WB (категория «Финансы»)."
                )

            if resp.status_code == 429:
                raise WBFinanceRateLimitError("429 Too Many Requests от WB Finance API")

            if resp.status_code >= 400:
                resp.raise_for_status()

            data = resp.json()
            if isinstance(data, list):
                return data
            return []
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise WBFinanceUnauthorizedError("Токен WB не авторизован") from e
            if e.response.status_code == 429:
                raise WBFinanceRateLimitError("Превышен лимит запросов WB Finance") from e
            raise WBFinanceAPIError(f"HTTP {e.response.status_code}: {e.response.text}") from e
        except Exception as e:
            if isinstance(e, (WBFinanceRateLimitError, WBFinanceUnauthorizedError, httpx.TimeoutException, httpx.NetworkError)):
                raise
            raise WBFinanceAPIError(f"Ошибка запроса WB Finance API: {e}") from e
        finally:
            if not self._client and not client.is_closed:
                await client.aclose()

    async def fetch_all_sales_reports(
        self,
        date_from: str,
        date_to: str,
        limit: int = 500,
        max_pages: int = 100,
    ) -> AsyncGenerator[List[Dict[str, Any]], None]:
        """
        Асинхронный генератор для постраничной выгрузки полного отчета реализации WB.
        Yields пачки записей (до limit строк в каждой).
        """
        rrd_id = 0
        pages_count = 0

        while pages_count < max_pages:
            page_data = await self.get_sales_reports_page(
                date_from=date_from,
                date_to=date_to,
                limit=limit,
                rrd_id=rrd_id,
            )
            if not page_data:
                break

            yield page_data
            pages_count += 1

            last_row = page_data[-1]
            last_rrd_id = last_row.get("rrdId") or last_row.get("rrd_id")
            if not last_rrd_id or last_rrd_id == rrd_id or len(page_data) < limit:
                break
            rrd_id = last_rrd_id
            # Небольшая пауза между страницами для предотвращения 429
            await asyncio.sleep(0.5)
