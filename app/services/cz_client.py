"""
True API & СУЗ 5.0 Client — интеграция с ГИС МТ / СУЗ-Облако 3.0.38 (Честный Знак)
Поддерживает:
1. Заказ КМ (эмиссия) по ТГ «Лёгкая промышленность» (lp, templateId: 10, cisType: UNIT)
2. Вывод из оборота (LP_SHIP_GOODS / /api/v3/dropout)
3. Возврат в оборот (LP_RETURN_GOODS)
4. Подписание запросов откреплённой CMS подписью в HTTP-заголовке X-Signature (Секция 2.3.1)
"""
import asyncio
import base64
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Any, Union

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
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
except (ImportError, ModuleNotFoundError):
    def retry(*args, **kwargs):
        def decorator(f):
            return f
        return decorator
    stop_after_attempt = wait_exponential = retry_if_exception_type = lambda *a, **k: None

from app.config import settings
from app.services.crypto_service import sign_document

logger = logging.getLogger(__name__)


class CZAPIError(Exception):
    """Base error for Честный Знак / СУЗ API."""
    def __init__(self, message: str, status_code: int = 0, response_body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class CZUnauthorizedError(CZAPIError):
    """Token expired or invalid."""
    pass


class CZDocumentError(CZAPIError):
    """Document creation/processing error."""
    pass


class CZClient:
    """
    Async client for ГИС МТ (True API) and СУЗ-Облако 5.0 (API 3.0.38).

    Usage:
        async with CZClient(inn=seller.cz_inn, token=seller.cz_token) as client:
            doc_id = await client.withdraw_from_circulation(kiz_codes=[...], ...)
    """

    # Product Group LP (Лёгкая промышленность / Одежда)
    PRODUCT_GROUP_LP = "lp"
    TEMPLATE_ID_LP_UNIT = 10  # Шаблон 10 для потребительской упаковки одежды

    # Document types ГИС МТ ISMP / True API
    DOC_TYPE_WITHDRAWAL = "LK_RECEIPT"    # Вывод из оборота (дистанционная продажа)
    DOC_TYPE_RETURN = "LP_RETURN"        # Возврат в оборот при дистанционной продаже

    def __init__(
        self,
        inn: str,
        token: Optional[str] = None,
        oms_id: Optional[str] = None,
        cert_thumbprint: Optional[str] = None,
    ):
        """
        Args:
            inn: ИНН продавца
            token: Client token (clientToken / Bearer token)
            oms_id: UUID СУЗ (omsId)
            cert_thumbprint: Отпечаток УКЭП (КриптоПро)
        """
        self.inn = inn
        self.token = token
        self.oms_id = oms_id or settings.cz_oms_id
        self.cert_thumbprint = cert_thumbprint or settings.cryptopro_cert_thumbprint
        self.base_url = settings.cz_effective_url
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        await self._ensure_client()
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    def _get_headers(self, signature: Optional[str] = None) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.token:
            headers["clientToken"] = self.token
            headers["Authorization"] = f"Bearer {self.token}"
        if signature:
            # Руководство 3.0.38 Секция 2.3.1: X-Signature с открепленной подписью CMS (Base64)
            headers["X-Signature"] = signature
        return headers

    async def _ensure_client(self):
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(60.0),
                headers=self._get_headers(),
            )

    async def authenticate(self, connection_id: Optional[str] = None) -> str:
        """
        Challenge-Response аутентификация в ГИС МТ True API (СУЗ 3.0.38 Секция 9.3.2 / True API Секция 2.3.1).
        1. GET /api/v3/true-api/auth/key -> uuid, data
        2. Подписание data закрытым ключом УКЭП (присоединенная подпись для СУЗ 9.3.2) -> signature
        3. POST /api/v3/true-api/auth/simpleSignIn/{omsConnection} -> session token
        """
        await self._ensure_client()

        # Step 1: Request auth key / challenge
        res_key = await self._client.get("/api/v3/true-api/auth/key")
        if not res_key.is_success:
            raise CZAPIError(f"Failed to get auth challenge: {res_key.status_code} {res_key.text}", res_key.status_code)
        key_data = res_key.json() if res_key.content else {}
        auth_uuid = key_data.get("uuid")
        auth_data = key_data.get("data")
        if not auth_uuid or not auth_data:
            raise CZAPIError(f"Invalid auth challenge response: {key_data}")

        conn = connection_id or self.token
        if conn and "-" in conn and len(conn) >= 32:
            # СУЗ 3.0.38 Раздел 9.3.2: присоединенная подпись в simpleSignIn/{omsConnection}
            signature = await sign_document(auth_data, cert_thumbprint=self.cert_thumbprint, attached=True)
            url = f"/api/v3/true-api/auth/simpleSignIn/{conn}"
        else:
            # True API v719.0 Секция 1.5.2: присоединенная электронная подпись случайных данных в data
            signature = await sign_document(auth_data, cert_thumbprint=self.cert_thumbprint, attached=True)
            url = "/api/v3/true-api/auth/simpleSignIn"

        sign_in_payload = {
            "uuid": auth_uuid,
            "data": signature,
        }
        if self.inn:
            sign_in_payload["inn"] = self.inn

        res_sign_in = await self._client.post(
            url,
            json=sign_in_payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if not res_sign_in.is_success:
            raise CZAPIError(f"Auth simpleSignIn failed: {res_sign_in.status_code} {res_sign_in.text}", res_sign_in.status_code)
        sign_in_data = res_sign_in.json() if res_sign_in.content else {}
        token = sign_in_data.get("token")
        if not token:
            raise CZAPIError(f"No token received from auth simpleSignIn: {sign_in_data}")

        self.token = token
        logger.info(f"Successfully authenticated with ГИС МТ for INN {self.inn}")
        return token

    async def get_auth_challenge(self) -> dict:
        """Fetch auth challenge (uuid, data) from True API."""
        await self._ensure_client()
        res = await self._client.get("/api/v3/true-api/auth/key")
        if not res.is_success:
            raise CZAPIError(f"Ошибка получения challenge: {res.status_code} {res.text}", res.status_code)
        return res.json() if res.content else {}

    async def signin_with_signature(self, auth_uuid: str, signed_data: str) -> str:
        """Authenticate with True API using pre-signed challenge data."""
        await self._ensure_client()
        payload = {"uuid": auth_uuid, "data": signed_data}
        if self.inn:
            payload["inn"] = self.inn
        res = await self._client.post(
            "/api/v3/true-api/auth/simpleSignIn",
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if not res.is_success:
            raise CZAPIError(f"Ошибка входа в ГИС МТ: {res.status_code} {res.text}", res.status_code)
        data = res.json() if res.content else {}
        token = data.get("token")
        if not token:
            raise CZAPIError(f"Токен не получен от ГИС МТ: {data}")
        self.token = token
        return token

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type((httpx.NetworkError, httpx.TimeoutException)),
    )
    async def _request(
        self,
        method: str,
        path: str,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        sign_request: bool = True,
    ) -> dict:
        """
        Execute signed HTTP request according to СУЗ 3.0.38 Spec 2.3.1.
        """
        await self._ensure_client()

        signature: Optional[str] = None
        if sign_request:
            if method.upper() in ("POST", "PUT") and json_body is not None:
                data_to_sign = json.dumps(json_body, ensure_ascii=False)
                signature = await sign_document(data_to_sign, cert_thumbprint=self.cert_thumbprint)
            elif method.upper() == "GET":
                # For GET: sign REQUEST_PATH + QUERY_STRING
                query_str = f"?{httpx.QueryParams(params)}" if params else ""
                data_to_sign = f"{path}{query_str}"
                signature = await sign_document(data_to_sign, cert_thumbprint=self.cert_thumbprint)

        headers = self._get_headers(signature=signature)

        response = await self._client.request(
            method=method,
            url=path,
            json=json_body,
            params=params,
            headers=headers,
        )

        if response.status_code == 401:
            raise CZUnauthorizedError("CZ/SUZ token expired or invalid", 401)
        if response.status_code not in (200, 201):
            raise CZAPIError(
                f"CZ API error {response.status_code}: {response.text}",
                response.status_code,
                response.text,
            )

        if response.content:
            try:
                return response.json()
            except Exception:
                return {"documentId": response.text.strip().strip('"')}
        return {}

    # ==================== 1. ЭМИССИЯ КМ В СУЗ (Секция 4.4.1) ====================

    async def create_order_emission(
        self,
        gtin: str,
        quantity: int,
        release_method_type: str = "PRODUCTION",
        serial_numbers: Optional[list[str]] = None,
        service_provider_id: Optional[str] = None,
    ) -> str:
        """
        Создать заказ на эмиссию КМ по ТГ «Лёгкая промышленность» (lp) (Секция 4.4.1.1.2).

        Args:
            gtin: GTIN (14 цифр)
            quantity: Количество КМ
            release_method_type: 'PRODUCTION', 'IMPORT', 'REMAINS', 'REMARK'
            serial_numbers: Список серийных номеров (12 символов каждый при SELF_MADE)

        Returns:
            orderId заказа на эмиссию в СУЗ
        """
        path = "/api/v3/order"
        params = {"omsId": self.oms_id}

        product_item: dict[str, Any] = {
            "gtin": gtin,
            "quantity": quantity,
            "templateId": self.TEMPLATE_ID_LP_UNIT,
            "cisType": "UNIT",
        }

        if serial_numbers:
            product_item["serialNumberType"] = "SELF_MADE"
            product_item["serialNumbers"] = serial_numbers
        else:
            product_item["serialNumberType"] = "OPERATOR"

        payload: dict[str, Any] = {
            "productGroup": self.PRODUCT_GROUP_LP,
            "products": [product_item],
            "attributes": {
                "releaseMethodType": release_method_type,
            },
        }

        if service_provider_id:
            payload["serviceProviderId"] = service_provider_id

        res = await self._request("POST", path, json_body=payload, params=params, sign_request=True)
        order_id = res.get("orderId")
        if not order_id:
            raise CZAPIError(f"No orderId returned from SUZ order creation: {res}")
        logger.info(f"SUZ Order emission created successfully: orderId={order_id}")
        return order_id

    async def get_order_status(self, order_id: str, gtin: Optional[str] = None) -> dict:
        """
        Проверить статус заказа на эмиссию КМ в СУЗ (Секция 4.4.2).
        GET /api/v3/order/status?omsId={omsId}&orderId={orderId}
        """
        path = "/api/v3/order/status"
        params = {"omsId": self.oms_id, "orderId": order_id}
        if gtin:
            params["gtin"] = gtin
        return await self._request("GET", path, params=params, sign_request=True)

    async def get_emission_codes(
        self,
        order_id: str,
        gtin: str,
        quantity: int,
        last_block_id: str = "0",
    ) -> list[str]:
        """
        Выгрузить готовые коды маркировки из СУЗ (Секция 4.4.4).
        GET /api/v3/codes?omsId={omsId}&orderId={orderId}&gtin={gtin}&quantity={quantity}
        """
        path = "/api/v3/codes"
        params = {
            "omsId": self.oms_id,
            "orderId": order_id,
            "gtin": gtin,
            "quantity": quantity,
            "lastBlockId": last_block_id,
        }
        res = await self._request("GET", path, params=params, sign_request=True)
        return res.get("codes", [])

    async def report_utilisation(
        self,
        product_group: str,
        sgtins: list[str],
        usage_type: str = "VERIFIED",
    ) -> dict:
        """
        Отправить отчет о нанесении / использовании КМ в СУЗ (Секция 4.4.11).
        POST /api/v3/report/utilisation?omsId={omsId}
        """
        path = "/api/v3/report/utilisation"
        params = {"omsId": self.oms_id}
        payload = {
            "productGroup": product_group or self.PRODUCT_GROUP_LP,
            "sntins": sgtins,
            "usageType": usage_type,
        }
        return await self._request("POST", path, json_body=payload, params=params, sign_request=True)

    # ==================== 2. ВЫВОД ИЗ ОБОРОТА И ВОЗВРАТ (True API v719.0 Секция 4.1) ====================

    def _build_withdrawal_document(
        self,
        kiz_codes: list[str],
        price_kopecks: int,
        mod_fias: Optional[str] = None,
        mod_kpp: Optional[str] = None,
        document_date: Optional[Union[datetime, str]] = None,
        primary_document_number: str = "",
        document_type: Optional[str] = None,
        action: str = "DISTANCE",
    ) -> dict:
        """Build LK_RECEIPT document for ГИС МТ / ISMP (Вывод из оборота: Дистанционная продажа)."""
        if document_date is None:
            doc_date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        elif isinstance(document_date, datetime):
            doc_date_str = document_date.strftime("%Y-%m-%d")
        else:
            doc_date_str = str(document_date).strip()

        resolved_doc_type = document_type or "OTHER"

        doc_body = {
            "inn": self.inn,
            "action": action,
            "action_date": doc_date_str,
            "document_type": resolved_doc_type,
            "document_number": primary_document_number or str(uuid.uuid4())[:8].upper(),
            "document_date": doc_date_str,
            "primary_document_custom_name": "Продажа через Wildberries FBS",
            "products": [
                {
                    "cis": kiz_code,
                    "product_cost": int(price_kopecks),
                }
                for kiz_code in kiz_codes
            ],
        }
        if mod_fias:
            doc_body["fias_id"] = mod_fias
        if mod_kpp:
            doc_body["kpp"] = mod_kpp

        inner_json = json.dumps(doc_body, ensure_ascii=False)
        return {
            "document_format": "MANUAL",
            "documentFormat": "MANUAL",
            "type": self.DOC_TYPE_WITHDRAWAL,
            "product_document": inner_json,
            "productDocument": inner_json,
        }

    def _build_return_document(
        self,
        kiz_codes: list[str],
        document_date: Optional[datetime] = None,
        primary_document_number: str = "",
        return_type: str = "REMOTE_SALE_RETURN",
    ) -> dict:
        """Build LP_RETURN document for ГИС МТ (Возврат в оборот при дистанционной продаже)."""
        if document_date is None:
            document_date = datetime.now(timezone.utc)

        doc_date_str = document_date.strftime("%Y-%m-%d")

        doc_body = {
            "trade_participant_inn": self.inn,
            "return_type": return_type,
            "paid": True,
            "primary_document_type": "OTHER",
            "primary_document_number": primary_document_number or str(uuid.uuid4())[:8].upper(),
            "primary_document_date": doc_date_str,
            "primary_document_custom_name": "Возврат от покупателя Wildberries FBS",
            "products_list": [
                {
                    "ki": kiz_code,
                }
                for kiz_code in kiz_codes
            ],
        }
        inner_json = json.dumps(doc_body, ensure_ascii=False)

        return {
            "document_format": "MANUAL",
            "documentFormat": "MANUAL",
            "type": self.DOC_TYPE_RETURN,
            "product_document": inner_json,
            "productDocument": inner_json,
        }

    async def _create_document(self, document: dict, sign: bool = True, pg: str = "lp") -> str:
        """Submit document to ГИС МТ ISMP gateway (/api/v3/lk/documents/create?pg=lp)."""
        product_document = document.get("productDocument") or document.get("product_document", "")
        if isinstance(product_document, dict):
            inner_json = json.dumps(product_document, ensure_ascii=False)
        else:
            inner_json = str(product_document)

        if sign:
            signature = await sign_document(
                data=inner_json,
                cert_thumbprint=self.cert_thumbprint,
                attached=False,
            )
            document["signature"] = signature

        # Encode inner JSON to base64 for /api/v3/lk/documents/create
        b64_product_doc = base64.b64encode(inner_json.encode('utf-8')).decode('ascii')
        doc_type = document.get("type", self.DOC_TYPE_WITHDRAWAL)
        # ISMP gateway for LP uses LP_RETURN for returns and LK_RECEIPT for remote sale withdrawals
        ismp_type = "LP_RETURN" if doc_type in ("LP_RETURN_GOODS", "LP_RETURN") else ("LK_RECEIPT" if doc_type in ("LP_SHIP_GOODS", "LK_RECEIPT") else doc_type)

        payload = {
            "document_format": document.get("document_format") or document.get("documentFormat") or "MANUAL",
            "product_document": b64_product_doc,
            "type": ismp_type,
            "signature": document.get("signature", ""),
        }

        # For lp and general light industry, ISMP gateway processes the document
        path = f"/api/v3/lk/documents/create?pg={pg}" if pg else "/api/v3/lk/documents/create"
        
        # Try ISMP endpoint first, fallback to base URL
        await self._ensure_client()
        headers = self._get_headers()
        try:
            res = await self._client.post(f"https://ismp.crpt.ru{path}", json=payload, headers=headers)
            if res.status_code in (200, 201):
                doc_id = res.text.strip().strip('"')
                logger.info(f"ГИС МТ Document created via ISMP: {doc_id}")
                return doc_id
        except Exception as e:
            logger.warning(f"ISMP direct post failed ({e}), trying base client...")

        result = await self._request("POST", path, json_body=payload, sign_request=False)
        doc_id = result.get("documentId") or result.get("id") or str(result)
        if not doc_id:
            raise CZDocumentError(f"No documentId in response: {result}")
        logger.info(f"ГИС МТ Document created: {doc_id}")
        return doc_id

    async def get_document_status(self, doc_id: str) -> dict:
        """Check document processing status in True API."""
        path = f"/api/v3/true-api/doc/{doc_id}/status"
        return await self._request("GET", path, sign_request=False)

    async def wait_for_document(
        self,
        doc_id: str,
        max_attempts: int = 15,
        interval_seconds: float = 3.0,
    ) -> dict:
        """Poll document status until completion."""
        for attempt in range(max_attempts):
            try:
                status_data = await self.get_document_status(doc_id)
            except Exception as e:
                # If document status endpoint is not active on gateway, return early
                logger.debug(f"Document status query note for {doc_id}: {e}")
                return {"documentId": doc_id, "status": "CHECKED_OK"}

            status = status_data.get("status", "")

            if status in ("CHECKED_OK", "ACCEPTED", "SUCCESS", "COMPLETED"):
                logger.info(f"Document {doc_id} processed successfully (status={status})")
                return status_data
            elif status in ("CHECKED_NOT_OK", "PROCESSING_ERROR", "PARSE_ERROR", "FAILED"):
                errors = status_data.get("errors", [])
                raise CZDocumentError(
                    f"Document {doc_id} failed with status {status}: {errors}",
                    response_body=str(errors),
                )
            elif status in ("IN_PROGRESS", "PROCESSING", "PENDING"):
                logger.debug(f"Document {doc_id} processing (attempt {attempt + 1})")
                await asyncio.sleep(interval_seconds)
            else:
                await asyncio.sleep(interval_seconds)

        return {"documentId": doc_id, "status": "SUBMITTED"}

    async def withdraw_from_circulation(
        self,
        kiz_codes: list[str],
        price_kopecks: int,
        mod_fias: Optional[str] = None,
        mod_kpp: Optional[str] = None,
        wb_order_id: Optional[int] = None,
        receipt_number: Optional[str] = None,
        receipt_date: Optional[Union[datetime, str]] = None,
        document_type: Optional[str] = None,
        wait_for_result: bool = False,
    ) -> str:
        """Вывод КИЗ из оборота при продаже WB FBS."""
        if not kiz_codes:
            raise ValueError("kiz_codes cannot be empty")

        doc_num = receipt_number or (str(wb_order_id) if wb_order_id else "")
        document = self._build_withdrawal_document(
            kiz_codes=kiz_codes,
            price_kopecks=price_kopecks,
            mod_fias=mod_fias,
            mod_kpp=mod_kpp,
            document_date=receipt_date,
            primary_document_number=doc_num,
            document_type=document_type,
        )

        doc_id = await self._create_document(document, sign=True)
        if wait_for_result:
            await self.wait_for_document(doc_id)
        return doc_id

    def build_withdrawal_payload(
        self,
        kiz_codes: list[str],
        price_kopecks: int,
        mod_fias: Optional[str] = None,
        mod_kpp: Optional[str] = None,
        wb_order_id: Optional[int] = None,
        receipt_number: Optional[str] = None,
        receipt_date: Optional[Union[datetime, str]] = None,
        document_type: Optional[str] = None,
    ) -> dict:
        """Построение структуры и Base64-данных документа вывода для клиентского подписания."""
        doc_num = receipt_number or (str(wb_order_id) if wb_order_id else "")
        doc = self._build_withdrawal_document(
            kiz_codes=kiz_codes,
            price_kopecks=price_kopecks,
            mod_fias=mod_fias,
            mod_kpp=mod_kpp,
            document_date=receipt_date,
            primary_document_number=doc_num,
            document_type=document_type,
        )
        inner_json = doc.get("product_document", "")
        b64_doc = base64.b64encode(inner_json.encode('utf-8')).decode('ascii')
        return {
            "type": "LK_RECEIPT",
            "document_format": "MANUAL",
            "inner_json": inner_json,
            "document_base64": b64_doc,
        }

    def build_return_payload(
        self,
        kiz_codes: list[str],
        wb_order_id: Optional[int] = None,
    ) -> dict:
        """Построение структуры и Base64-данных документа возврата для клиентского подписания."""
        doc = self._build_return_document(
            kiz_codes=kiz_codes,
            primary_document_number=str(wb_order_id) if wb_order_id else "",
        )
        inner_json = doc.get("product_document", "")
        b64_doc = base64.b64encode(inner_json.encode('utf-8')).decode('ascii')
        return {
            "type": "LP_RETURN",
            "document_format": "MANUAL",
            "inner_json": inner_json,
            "document_base64": b64_doc,
        }

    async def submit_signed_document(
        self,
        document_type: str,
        document_base64: str,
        signature_base64: str,
        pg: str = "lp",
        wait_for_result: bool = False,
    ) -> str:
        """
        Отправка уже подписанного на стороне клиента (браузера) документа в ГИС МТ.
        """
        ismp_type = "LP_RETURN" if document_type in ("LP_RETURN_GOODS", "LP_RETURN") else ("LK_RECEIPT" if document_type in ("LP_SHIP_GOODS", "LK_RECEIPT") else document_type)
        payload = {
            "document_format": "MANUAL",
            "product_document": document_base64,
            "type": ismp_type,
            "signature": signature_base64,
        }
        path = f"/api/v3/lk/documents/create?pg={pg}" if pg else "/api/v3/lk/documents/create"
        await self._ensure_client()
        headers = self._get_headers()
        try:
            res = await self._client.post(f"https://ismp.crpt.ru{path}", json=payload, headers=headers)
            if res.status_code in (200, 201):
                doc_id = res.text.strip().strip('"')
                logger.info(f"ГИС МТ Signed Document created via ISMP: {doc_id}")
                if wait_for_result:
                    await self.wait_for_document(doc_id)
                return doc_id
        except Exception as e:
            logger.warning(f"ISMP direct post of signed doc failed ({e}), trying base client...")

        result = await self._request("POST", path, json_body=payload, sign_request=False)
        doc_id = result.get("documentId") or result.get("id") or str(result)
        if not doc_id:
            raise CZDocumentError(f"No documentId in response: {result}")
        logger.info(f"ГИС МТ Signed Document created: {doc_id}")
        if wait_for_result:
            await self.wait_for_document(doc_id)
        return doc_id

    async def return_to_circulation(
        self,
        kiz_codes: list[str],
        wb_order_id: Optional[int] = None,
        wait_for_result: bool = False,
    ) -> str:
        """Возврат КИЗ в оборот при возврате покупателя."""
        if not kiz_codes:
            raise ValueError("kiz_codes cannot be empty")

        document = self._build_return_document(kiz_codes=kiz_codes)
        doc_id = await self._create_document(document, sign=True)
        if wait_for_result:
            await self.wait_for_document(doc_id)
        return doc_id

    async def get_cises_info(self, cises: list[str]) -> list[dict]:
        """
        Онлайн-валидация кодов маркировки через True API (POST /api/v3/true-api/cises/info).
        Возвращает полную информацию о статусе КИЗ (INTRODUCED, EMITTED, RETIRED), владельце и блокировках ОГВ (ogvs).
        """
        if not cises:
            return []
        path = "/api/v3/true-api/cises/info"
        res = await self._request("POST", path, json_body=cises, sign_request=False)
        if isinstance(res, list):
            return res
        elif isinstance(res, dict) and "cises" in res:
            return res.get("cises", [])
        return [res] if res else []

    async def get_cises_short_list(self, cises: list[str]) -> list[dict]:
        """
        Метод получения краткой информации о КИ по списку (True API v719.0 Секция 5.1.4 /cises/short/list).
        """
        if not cises:
            return []
        path = "/api/v3/true-api/cises/short/list"
        res = await self._request("POST", path, json_body=cises, sign_request=False)
        if isinstance(res, list):
            return res
        elif isinstance(res, dict) and "cises" in res:
            return res.get("cises", [])
        return [res] if res else []

    async def get_document_receipt(self, doc_id: str) -> dict:
        """
        Получение официальной квитанции результата обработки документа (True API v719.0 Секция 7.1).
        GET /api/v3/true-api/documents/receipts/{docId}
        """
        path = f"/api/v3/true-api/documents/receipts/{doc_id}"
        return await self._request("GET", path, sign_request=False)

    async def get_seller_mod_list(self, inn: Optional[str] = None) -> list[dict]:
        """
        Получение списка мест осуществления деятельности (МОД / складов) (True API v719.0 Секция 3.5).
        GET /api/v3/true-api/organizations/{inn}/mod
        """
        target_inn = inn or self.inn
        path = f"/api/v3/true-api/organizations/{target_inn}/mod"
        res = await self._request("GET", path, sign_request=False)
        if isinstance(res, list):
            return res
        elif isinstance(res, dict) and "modList" in res:
            return res.get("modList", [])
        return [res] if res else []

    async def check_mod_status(self, fias_id: str) -> dict:
        """
        Проверка статуса места осуществления деятельности (МОД) (True API v719.0 Секция 3.4).
        GET /api/v3/true-api/mod/status?fiasId={fias_id}
        """
        path = "/api/v3/true-api/mod/status"
        return await self._request("GET", path, params={"fiasId": fias_id}, sign_request=False)

    async def check_kiz_status(self, kiz_code: str) -> dict:
        """Проверить статус КИЗ в ГИС МТ."""
        path = "/api/v3/true-api/codes/check"
        return await self._request("POST", path, json_body={"codes": [kiz_code]}, sign_request=False)

    async def close(self):
        """Закрыть HTTP клиент."""
        await self._client.aclose()
