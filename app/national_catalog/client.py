import asyncio
import logging
from typing import Any, Dict, List, Optional
import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class NKAPIError(Exception):
    """Исключение при взаимодействии с API Национального каталога."""
    def __init__(self, message: str, status_code: int = 0, response_body: str = ""):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_body = response_body


class NKClient:
    """
    Асинхронный клиент для взаимодействия с подсистемой «Национальный каталог» (НКТ)
    в контуре ГИС МТ True API (v719.0 Секция 10).
    """

    def __init__(
        self,
        token: str,
        inn: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 60.0,
    ):
        self.token = token
        self.inn = inn
        self.base_url = (base_url or settings.cz_effective_url).rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        await self._ensure_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _ensure_client(self):
        if self._client is None:
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
                headers["clientToken"] = self.token

            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(self.timeout),
            )

    def _extract_error(self, response: httpx.Response) -> str:
        try:
            data = response.json()
            if isinstance(data, dict):
                return (
                    data.get("error_message")
                    or data.get("errorMessage")
                    or data.get("message")
                    or data.get("description")
                    or str(data)
                )
            elif isinstance(data, list) and data:
                return str(data[0])
        except Exception:
            pass
        return response.text.strip() or f"HTTP {response.status_code}"

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
    ) -> Any:
        """
        Выполняет запрос к True API с автоматической обработкой префикса /api/v3/true-api/nk/
        и fallback на /nk/.
        """
        await self._ensure_client()

        # Приоритетный путь через шлюз True API
        clean_endpoint = endpoint.lstrip("/")
        if not clean_endpoint.startswith("nk/"):
            clean_endpoint = f"nk/{clean_endpoint}"

        primary_path = f"/api/v3/true-api/{clean_endpoint}"

        query_params = dict(params or {})
        if self.api_key and "apikey" not in query_params and not self.token:
            query_params["apikey"] = self.api_key

        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = await self._client.request(
                    method=method,
                    url=primary_path,
                    params=query_params if query_params else None,
                    json=json_body,
                )

                # Если 404 на шлюзе v3, пробуем прямой вызов /nk/...
                if resp.status_code == 404:
                    fallback_path = f"/{clean_endpoint}"
                    resp_fallback = await self._client.request(
                        method=method,
                        url=fallback_path,
                        params=query_params if query_params else None,
                        json=json_body,
                    )
                    if resp_fallback.status_code != 404:
                        resp = resp_fallback

                if resp.status_code in (200, 201):
                    if resp.content:
                        try:
                            return resp.json()
                        except Exception:
                            return resp.text
                    return {}

                # Если 429 (Слишком много запросов), повторяем с задержкой
                if resp.status_code == 429 and attempt < max_retries - 1:
                    wait_sec = (attempt + 1) * 1.5
                    logger.warning("Честный Знак 429 (Too Many Requests) на %s, повтор через %.1f сек...", endpoint, wait_sec)
                    await asyncio.sleep(wait_sec)
                    continue

                err_msg = self._extract_error(resp)
                raise NKAPIError(
                    message=f"Ошибка Честного Знака ({resp.status_code}): {err_msg}",
                    status_code=resp.status_code,
                    response_body=resp.text,
                )

            except httpx.RequestError as exc:
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.0)
                    continue
                logger.error("Сетевая ошибка при запросе к ЧЗ (%s %s): %s", method, endpoint, exc)
                raise NKAPIError(f"Сетевая ошибка при подключении к Честному Знаку: {exc}")

    # ================= 1. Создание и обновление фидов =================

    async def create_or_update_feed(self, goods: List[Dict[str, Any]]) -> int:
        """
        Отправка пакета обновлений (фида) товаров (POST /nk/feed).
        Возвращает feed_id.
        """
        res = await self._request("POST", "/nk/feed", json_body=goods)
        if isinstance(res, dict):
            res_body = res.get("result", {})
            if isinstance(res_body, dict) and "feed_id" in res_body:
                return int(res_body["feed_id"])
            if "feed_id" in res:
                return int(res["feed_id"])
        raise NKAPIError(f"Неожиданный ответ при отправке фида: {res}")

    async def get_feed_status(self, feed_id: int) -> Dict[str, Any]:
        """
        Получение статуса ранее отправленного фида (GET /nk/feed-status?feed_id=...).
        """
        res = await self._request("GET", "/nk/feed-status", params={"feed_id": feed_id})
        if isinstance(res, dict):
            return res.get("result", res)
        return {"status": "Processing", "feed_id": feed_id}

    # ================= 2. Чтение карточек товаров =================

    async def get_feed_product(
        self,
        good_id: Optional[int] = None,
        gtin: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Получение полной карточки товара, включая черновики (GET /nk/feed-product).
        """
        params: Dict[str, Any] = {}
        if good_id:
            params["good_id"] = good_id
        elif gtin:
            params["gtin"] = gtin
        else:
            raise ValueError("Необходимо указать good_id или gtin")

        res = await self._request("GET", "/nk/feed-product", params=params)
        if isinstance(res, dict):
            items = res.get("result", [])
            return items if isinstance(items, list) else [items]
        return []

    async def get_product_info(self, gtin: str) -> Optional[Dict[str, Any]]:
        """
        Получение опубликованного описания товара по GTIN (GET /nk/product).
        """
        res = await self._request("GET", "/nk/product", params={"gtin": gtin})
        if isinstance(res, dict):
            items = res.get("result", [])
            if isinstance(items, list) and items:
                return items[0]
        return None

    # ================= 3. Модерация =================

    async def send_to_moderation(
        self,
        good_id: Optional[int] = None,
        gtin: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Отправка карточки товара на модерацию (GET /nk/feed-moderation).
        """
        params: Dict[str, Any] = {}
        if good_id:
            params["good_id"] = good_id
        elif gtin and self.inn:
            params["gtin"] = gtin
            params["inn"] = self.inn
        else:
            raise ValueError("Необходимо указать good_id или gtin c ИНН")

        res = await self._request("GET", "/nk/feed-moderation", params=params)
        if isinstance(res, dict):
            return res.get("result", res)
        return {"success": True}

    # ================= 4. Подготовка и подписание документов =================

    async def get_product_document_xml(self, good_ids: List[int]) -> List[Dict[str, Any]]:
        """
        Получение XML товара для подписания (POST /nk/feed-product-document).
        Возвращает [{"goodId": int, "gtin": str, "xml": str}]
        """
        payload = {
            "goodIds": good_ids,
            "publicationAgreement": True,
        }
        res = await self._request("POST", "/nk/feed-product-document", json_body=payload)
        if isinstance(res, dict):
            result = res.get("result", [])
            if isinstance(result, list) and result:
                first = result[0]
                if isinstance(first, dict) and "xmls" in first:
                    return first["xmls"]
            if isinstance(result, dict) and "xmls" in result:
                return result["xmls"]
        return []

    async def sign_product_pkcs(
        self,
        signed_items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Отправка открепленной подписи PKCS#7 / CMS карточки товара
        (POST /nk/feed-product-sign-pkcs).
        signed_items: [{"goodId": int, "base64Xml": str, "signature": str}]
        """
        res = await self._request("POST", "/nk/feed-product-sign-pkcs", json_body=signed_items)
        if isinstance(res, dict):
            return res.get("result", res)
        return {"signed": [item["goodId"] for item in signed_items]}

    # ================= 5. Справочники =================

    async def get_categories(
        self,
        tnved: Optional[str] = None,
        cat_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Получение дерева категорий (GET /nk/categories).
        """
        params: Dict[str, Any] = {}
        if tnved:
            params["tnved"] = tnved
        if cat_id:
            params["cat_id"] = cat_id

        res = await self._request("GET", "/nk/categories", params=params)
        if isinstance(res, dict):
            items = res.get("result", [])
            return items if isinstance(items, list) else []
        return []

    async def get_attributes(
        self,
        cat_id: Optional[int] = None,
        tnved: Optional[str] = None,
        attr_type: str = "m",
    ) -> List[Dict[str, Any]]:
        """
        Получение состава атрибутов (GET /nk/attributes).
        attr_type: m (обязательные), r (рекомендуемые), o (опциональные), a (все).
        """
        params: Dict[str, Any] = {"attr_type": attr_type}
        if cat_id:
            params["cat_id"] = cat_id
        if tnved:
            params["tnved"] = tnved

        res = await self._request("GET", "/nk/attributes", params=params)
        if isinstance(res, dict):
            items = res.get("result", [])
            return items if isinstance(items, list) else []
        return []

    async def get_brands(self, name: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Поиск торговых марок / брендов (GET /nk/brands).
        """
        res = await self._request("GET", "/nk/brands", params={"name": name, "limit": limit})
        if isinstance(res, dict):
            items = res.get("result", [])
            return items if isinstance(items, list) else []
        return []

    async def generate_gtin(self, quantity: int = 1) -> List[str]:
        """
        Генерация черновиков GTIN по пулу ГС1 РУС (GET /nk/generate-gtins).
        """
        res = await self._request("GET", "/nk/generate-gtins", params={"quantity": quantity})
        gtins = []
        if isinstance(res, dict):
            result = res.get("result", {})
            drafts = result.get("drafts", []) if isinstance(result, dict) else []
            for d in drafts:
                if isinstance(d, dict) and "gtin" in d:
                    gtins.append(d["gtin"])
        return gtins

    async def get_etags_list(self, cat_id: Optional[int] = None, offset: int = 0) -> Dict[str, Any]:
        """
        Получение списка товаров и хешей страниц (GET /nk/etagslist).
        """
        params: Dict[str, Any] = {"offset": offset}
        if cat_id:
            params["cat_id"] = cat_id
        if self.inn:
            params["owner_inn"] = self.inn

        res = await self._request("GET", "/nk/etagslist", params=params)
        if isinstance(res, dict):
            return res.get("result", res)
        return {}
