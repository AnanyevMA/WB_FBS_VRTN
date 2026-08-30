"""
Wildberries Marketplace API Client.
Handles authentication, requests, rate limits, retries, and error mapping for WB FBS Manager.
"""
import asyncio
from datetime import datetime
import logging
from typing import Any, Dict, List, Optional, Union

try:
    import httpx
except (ImportError, ModuleNotFoundError):
    class DummyHTTPX:
        RequestError = Exception
        TimeoutException = Exception
        HTTPStatusError = Exception
        NetworkError = Exception
    httpx = DummyHTTPX()

try:
    from tenacity import (
        retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
    )
except (ImportError, ModuleNotFoundError):
    def retry(*args, **kwargs):
        def decorator(f):
            return f
        return decorator
    stop_after_attempt = wait_exponential = retry_if_exception_type = lambda *a, **k: None

logger = logging.getLogger(__name__)


class WBAPIError(Exception):
    """Base exception for WB API errors."""
    pass


class WBUnauthorizedError(WBAPIError):
    """401 Unauthorized."""
    pass


class WBRateLimitError(WBAPIError):
    """429 Too Many Requests."""
    pass


class WBMetaValidationError(WBAPIError):
    """409 Conflict, typically MetaValidationFail (e.g. KIZ not attached before delivery)."""
    pass


class WBClient:
    """
    Wildberries API Client.
    Multi-tenant ready: takes the token in the constructor.
    """
    BASE_URL = "https://marketplace-api.wildberries.ru"

    def __init__(self, api_token: str):
        self.api_token = api_token
        self.headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._client: Optional[httpx.AsyncClient] = None
        self._loop = None
        self.log = logger

    @property
    def client(self) -> httpx.AsyncClient:
        """Returns or creates an AsyncClient bound to the currently running event loop."""
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if (
            self._client is None
            or getattr(self._client, "is_closed", False)
            or self._loop != current_loop
        ):
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                headers=self.headers,
                timeout=30.0,
            )
            self._loop = current_loop
        return self._client

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        if self._client and not getattr(self._client, "is_closed", False):
            await self._client.aclose()
            self._client = None
            self._loop = None

    @retry(
        retry=retry_if_exception_type((WBRateLimitError, httpx.RequestError, httpx.TimeoutException)),
        stop=stop_after_attempt(4),  # 1 initial attempt + 3 retries
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        """Internal method to execute HTTP requests with retries and error handling."""
        self.log.debug(f"Calling WB API: {method} {endpoint}")
        
        try:
            response = await self.client.request(method, endpoint, **kwargs)
        except (httpx.RequestError, httpx.TimeoutException) as e:
            self.log.error(f"Network error calling WB API {method} {endpoint}: {str(e)}")
            raise
        
        if response.status_code == 401:
            self.log.error(f"WB API Unauthorized: {endpoint}")
            raise WBUnauthorizedError("Invalid or expired API token.")
        
        if response.status_code == 429:
            self.log.warning(f"WB API Rate limit hit: {endpoint}")
            raise WBRateLimitError("Too Many Requests.")
            
        if response.status_code == 409:
            self.log.error(f"WB API Conflict / Meta Validation Fail {endpoint}: {response.text}")
            raise WBMetaValidationError(f"Meta Validation Fail: {response.text}")
            
        if not response.is_success:
            self.log.error(f"WB API Error {response.status_code} {endpoint}: {response.text}")
            raise WBAPIError(f"HTTP {response.status_code}: {response.text}")

        if not response.content:
            return None
            
        try:
            return response.json()
        except ValueError:
            return response.text

    # --- Orders ---

    async def get_new_orders(self) -> List[Dict]:
        """
        GET /api/v3/orders/new
        Returns list of new assembly tasks.
        
        Response structure:
        {
          "orders": [
            {
              "id": 123456,
              "article": "ITEM-1",
              "rid": "1234567890",
              "createdAt": "2024-01-01T10:00:00Z",
              "warehouseId": 123,
              "nmId": 987654,
              "chrtId": 4567,
              "price": 1000,
              "convertedPrice": 1000,
              "currencyCode": 643,
              "deliveryType": "fbs",
              "cargoType": 1,
              ...
            }
          ]
        }
        """
        data = await self._request("GET", "/api/v3/orders/new")
        if data and isinstance(data, dict):
            return data.get("orders", [])
        return []

    async def get_orders(self, date_start: datetime, date_end: datetime) -> List[Dict]:
        """
        GET /api/v3/orders
        Retrieves orders created within the specified date range.
        WB API expects Unix timestamps for dates.
        """
        params = {
            "dateFrom": int(date_start.timestamp()),
            "dateTo": int(date_end.timestamp()),
            "limit": 1000,
            "next": 0
        }
        data = await self._request("GET", "/api/v3/orders", params=params)
        if data and isinstance(data, dict):
            return data.get("orders", [])
        return []

    async def get_orders_status(self, order_ids: List[int]) -> List[Dict]:
        """
        POST /api/v3/orders/status
        Body: { "orders": [id1, id2, ...] }
        
        Response:
        {
          "orders": [
            {
              "id": 123456,
              "supplierStatus": "complete",
              "wbStatus": "sorted",
              "isCancellable": false
            }
          ]
        }
        """
        if not order_ids:
            return []
        payload = {"orders": order_ids}
        data = await self._request("POST", "/api/v3/orders/status", json=payload)
        if data and isinstance(data, dict):
            return data.get("orders", [])
        return []

    async def cancel_order(self, order_id: int) -> bool:
        """
        PATCH /api/v3/orders/{orderId}/cancel
        Cancels a specific order.
        """
        await self._request("PATCH", f"/api/v3/orders/{order_id}/cancel")
        return True

    # --- Stickers ---

    async def get_stickers(self, order_ids: List[int], format: str = 'svg') -> List[Dict]:
        """
        POST /api/v3/orders/stickers
        Body: {orders: [id1, id2], type: 'svg'|'zpl', width: 58, height: 40}
        
        Response structure:
        {
          "stickers": [
            {
              "orderId": 123456,
              "partA": 1234,
              "partB": 5678,
              "barcode": "12345678",
              "file": "base64_encoded_content..." 
            }
          ]
        }
        """
        payload = {
            "orders": order_ids,
            "type": format,
            "width": 58,
            "height": 40
        }
        data = await self._request("POST", "/api/v3/orders/stickers", json=payload)
        if data and isinstance(data, dict):
            return data.get("stickers", [])
        return []

    async def get_order_sticker(self, order_id: int, format: str = 'svg') -> Optional[Dict]:
        """
        Обёртка над get_stickers для скачивания стикера одного заказа.
        Возвращает dict стикера или None.
        """
        stickers = await self.get_stickers([order_id], format=format)
        if stickers:
            return stickers[0]
        return None

    # --- Meta / KIZ ---

    async def set_order_sgtin(self, order_id: int, kiz_codes: List[str]) -> bool:
        """
        PUT /api/v3/orders/{orderId}/meta/sgtin
        Body: {shgt: ["kiz_code1", ...]}
        """
        payload = {
            "shgt": kiz_codes
        }
        await self._request("PUT", f"/api/v3/orders/{order_id}/meta/sgtin", json=payload)
        return True

    async def get_orders_meta(self, order_ids: List[int]) -> Dict:
        """
        POST /api/marketplace/v3/orders/meta
        Body: {orders: [id1, id2]}
        Returns meta info including KIZ validation status.
        """
        payload = {
            "orders": order_ids
        }
        data = await self._request("POST", "/api/marketplace/v3/orders/meta", json=payload)
        return data or {}

    # --- Supplies ---

    async def get_supplies(self, limit: int = 100, next_id: int = 0) -> Dict:
        """
        GET /api/v3/supplies
        Params: limit (default 100), next (default 0)
        Returns: {"supplies": [...], "next": ...}
        """
        params = {"limit": limit, "next": next_id}
        data = await self._request("GET", "/api/v3/supplies", params=params)
        return data or {}

    async def create_supply(self, name: str) -> Dict:
        """
        POST /api/v3/supplies
        Body: {name: "Supply name"}
        
        Response structure:
        {
          "id": "WB-GI-1234567"
        }
        """
        payload = {"name": name}
        data = await self._request("POST", "/api/v3/supplies", json=payload)
        return data or {}

    async def add_order_to_supply(self, supply_id: str, order_id: int) -> bool:
        """
        PUT /api/v3/supplies/{supplyId}/orders/{orderId}
        """
        await self._request("PUT", f"/api/v3/supplies/{supply_id}/orders/{order_id}")
        return True

    async def get_supply_orders(self, supply_id: str) -> List[Dict]:
        """
        GET /api/v3/supplies/{supplyId}/orders
        Returns the list of orders currently in the supply.
        """
        data = await self._request("GET", f"/api/v3/supplies/{supply_id}/orders")
        if data and isinstance(data, dict):
            return data.get("orders", [])
        return []

    async def deliver_supply(self, supply_id: str) -> bool:
        """
        PATCH /api/v3/supplies/{supplyId}/deliver
        This closes the supply and sends it to delivery.
        May raise WBMetaValidationError (409) if KIZ not attached.
        """
        await self._request("PATCH", f"/api/v3/supplies/{supply_id}/deliver")
        return True

    async def get_supply_barcode(self, supply_id: str, format: str = 'svg') -> Dict:
        """
        GET /api/v3/supplies/{supplyId}/barcode?type=svg
        Returns the supply barcode (usually base64 encoded).
        """
        params = {"type": format}
        data = await self._request("GET", f"/api/v3/supplies/{supply_id}/barcode", params=params)
        return data or {}

    # --- Content API / Product Cards ---

    async def get_cards_catalog(self, limit: int = 100) -> Dict[str, Any]:
        """
        Fetch product catalog cards from WB Content API:
        POST https://content-api.wildberries.ru/content/v2/get/cards/list

        Returns indexed dictionary:
        {
          "by_vendor_code": { vendor_code: card_info },
          "by_nm_id": { nm_id: card_info },
          "by_chrt_id": { chrt_id: { "title": ..., "subjectName": ..., "brand": ..., "techSize": ..., "wbSize": ..., "tnved": ... } }
        }
        """
        catalog = {
            "by_vendor_code": {},
            "by_nm_id": {},
            "by_chrt_id": {},
        }
        url = "https://content-api.wildberries.ru/content/v2/get/cards/list"
        payload = {
            "settings": {
                "cursor": {"limit": limit},
                "filter": {"withPhoto": -1}
            }
        }
        try:
            headers = {
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            async with httpx.AsyncClient(timeout=30.0) as content_client:
                res = await content_client.post(url, json=payload, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    cards = data.get("cards", []) if isinstance(data, dict) else []
                    for c in cards:
                        v_code = (c.get("vendorCode") or "").strip()
                        nm_id = c.get("nmID")
                        title = c.get("title") or ""
                        subj = c.get("subjectName") or ""
                        brand = c.get("brand") or ""
                        sizes = c.get("sizes", [])

                        # Extract TNVED from characteristics
                        tnved = ""
                        for ch in c.get("characteristics", []):
                            if ch.get("name") in ["ТНВЭД", "ТН ВЭД", "Код ТН ВЭД"]:
                                vals = ch.get("value", [])
                                if isinstance(vals, list) and vals:
                                    tnved = str(vals[0])
                                elif isinstance(vals, str):
                                    tnved = vals
                                break

                        card_info = {
                            "vendorCode": v_code,
                            "nmID": nm_id,
                            "title": title,
                            "subjectName": subj,
                            "brand": brand,
                            "sizes": sizes,
                            "tnved": tnved,
                        }

                        if v_code:
                            catalog["by_vendor_code"][v_code] = card_info
                            catalog["by_vendor_code"][v_code.lower()] = card_info
                        if nm_id:
                            catalog["by_nm_id"][nm_id] = card_info

                        for s in sizes:
                            chrt = s.get("chrtID")
                            tech_size = s.get("techSize") or ""
                            wb_size = s.get("wbSize") or ""
                            if chrt:
                                catalog["by_chrt_id"][chrt] = {
                                    "vendorCode": v_code,
                                    "nmID": nm_id,
                                    "title": title,
                                    "subjectName": subj,
                                    "brand": brand,
                                    "techSize": tech_size,
                                    "wbSize": wb_size,
                                    "tnved": tnved,
                                }
                else:
                    self.log.warning(f"Content API get cards list returned status {res.status_code}: {res.text}")
        except Exception as exc:
            self.log.error(f"Error fetching WB cards catalog: {exc}")

        return catalog


def is_kiz_required(
    subject: Optional[str] = None,
    tnved: Optional[str] = None,
    order_raw: Optional[dict] = None
) -> bool:
    """
    Определяет обязательность маркировки «Честный Знак» (КИЗ / SGTIN).

    Приоритет проверки (по документации WB API):
    1. Поле `requiredMeta` из ответа GET /api/v3/orders/new —
       если содержит "sgtin", маркировка ОБЯЗАТЕЛЬНА.
       Если `requiredMeta` присутствует и НЕ содержит "sgtin" — маркировка НЕ нужна
       (возврат False без дальнейших эвристик).
    2. Эвристики по ТН ВЭД и названию категории — используются ТОЛЬКО если
       `requiredMeta` не передано (например, при работе с архивом,
       не через orders/new).
    """
    # --- 1. requiredMeta из WB API (первичный и определяющий источник) ---
    if order_raw:
        required_meta = order_raw.get("requiredMeta")

        # requiredMeta присутствует в payload — это авторитетный ответ WB
        if required_meta is not None:
            if isinstance(required_meta, list):
                return any(
                    str(item).lower() in ("sgtin", "kiz")
                    for item in required_meta
                )
            elif isinstance(required_meta, str):
                meta_lower = required_meta.lower()
                return "sgtin" in meta_lower or "kiz" in meta_lower
            # requiredMeta присутствует, но пустой/неизвестный тип → не нужен
            return False

    # --- 2. Эвристики (вторичный источник, без requiredMeta) ---
    if tnved:
        clean_tnved = str(tnved).replace(" ", "").replace(".", "").strip()
        marked_tnved_prefixes = (
            "6101", "6102", "6103", "6104", "6105", "6106", "6107", "6108", "6109", "6110",
            "6111", "6112", "6113", "6114", "6115", "6116", "6117",
            "6201", "6202", "6203", "6204", "6205", "6206", "6207", "6208", "6209", "6210",
            "6211", "6212", "6214", "6215", "6216", "6217",
            "6505",
            "6401", "6402", "6403", "6404", "6405",
            "6302", "6301", "6303", "6304",
            "4203",
            "3303",
            "4011",
            "9004",
        )
        if any(clean_tnved.startswith(prefix) for prefix in marked_tnved_prefixes):
            return True

    if subject:
        subj_lower = str(subject).lower().strip()
        marked_subjects = (
            "капор", "капоры", "юбк", "брюк", "джинс", "худи", "свитшот", "толстовк", "свитер",
            "кофт", "кардиган", "рубашк", "блузк", "футболк", "поло", "топ", "лонгслив", "куртк",
            "пальто", "пуховик", "ветровк", "плащ", "жакет", "пиджак", "костюм", "плать",
            "сарафан", "комбинезон", "шорт", "пижам", "халат", "варежк", "перчатк", "шарф",
            "манишк", "платок", "панам", "кепк", "шапк", "головн", "одежд", "трикотаж", "обув",
            "ботинк", "туфл", "кроссовк", "сапог", "сандал", "белье постельн", "полотенц",
            "текстиль", "духи", "туалетная вода", "парфюм", "парфюмер"
        )
        if any(kw in subj_lower for kw in marked_subjects):
            return True

    return False

