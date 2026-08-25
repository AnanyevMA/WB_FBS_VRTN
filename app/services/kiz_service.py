import re
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple, List

logger = logging.getLogger(__name__)

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.seller import Seller
from app.models.order import Order
from app.models.kiz import KizProductInfo
from app.services.encryption import decrypt
from app.services.wb_client import WBClient
from app.services.cz_client import CZClient

# Справочник органов государственной власти, установивших блокировку на КМ (True API v719.0 от 18.08.2026)
OGV_AGENCIES_MAP = {
    "MZ": "Министерство здравоохранения РФ (Минздрав)",
    "VETRF": "ФГИС ВетИС (Меркурий)",
    "RD": "Федеральная служба по аккредитации (Росаккредитация)",
    "FSSP": "Федеральная служба судебных приставов (ФССП)",
    "RPN": "Федеральная служба по надзору в сфере защиты прав потребителей (Роспотребнадзор)",
    "RZN": "Федеральная служба по надзору в сфере здравоохранения (Росздравнадзор)",
    "FTS": "Федеральная таможенная служба (ФТС)",
    "RAR": "Росалкогольрегулирование",
    "FNS": "ФНС России",
    "RSHN": "Россельхознадзор",
    "MVD": "МВД России",
}

# Статусы вывода из оборота / выбытия / списания (True API v719.0 Справочник «Статусы КИ»)
CZ_WITHDRAWAL_STATUSES = {
    "RETIRED",             # Выбыл (легпром, обувь, вода, шины, парфюмерия, БАД, медизделия и др.)
    "WITHDRAWN",           # Выбыл (табачная, никотиновая продукция и специализированные API)
    "WRITTEN_OFF",         # Списан (брак, порча, утеря, уничтожение)
    "DISAGGREGATION",      # Расформирован (для транспортных упаковок / агрегатов)
    "DISAGGREGATED",       # Расформирован (табачная продукция)
    "KILLED",              # Аннулирован / уничтожен
    "APPLIED_NOT_PAID",    # Нанесен, но не оплачен
}

# Особые состояния (statusEx), означающие факт вывода из оборота или списания
CZ_WITHDRAWAL_STATUS_EX = {
    "LOAN_RETIRED",         # Выведен из оборота по договору рассрочки
    "REMARK_RETIRED",       # Перемаркирован (старый код выбыл)
    "WAIT_REMARK",          # Ожидает перемаркировку (старый код выведен)
    "RETIRED_CANCELLATION", # Списан / Аннулирован
    "LOST_INVENTORY",       # Не найден по итогу инвентаризации
    "EAS_RESPOND_NOT_OK",   # Отрицательное решение ЕАЭС
}

# Статусы, при которых товар еще НЕ введен в оборот (не может быть продан)
CZ_NOT_INTRODUCED_STATUSES = {
    "EMITTED",              # Эмитирован в СУЗ, но не нанесен и не введен
    "APPLIED",              # Нанесен, но не введен в оборот
}

# Человекочитаемые наименования статусов ГИС МТ
CZ_STATUS_DESCRIPTIONS = {
    "INTRODUCED": "В обороте",
    "IN_CIRCULATION": "В обороте",
    "RETIRED": "Выбыл (выведен из оборота)",
    "WITHDRAWN": "Выбыл (выведен из оборота)",
    "WRITTEN_OFF": "Списан",
    "DISAGGREGATION": "Расформирован",
    "DISAGGREGATED": "Расформирован",
    "KILLED": "Списан / Аннулирован",
    "APPLIED_NOT_PAID": "Нанесен (не оплачен)",
    "APPLIED": "Нанесен (не введен в оборот)",
    "EMITTED": "Эмитирован (не введен в оборот)",
}


def is_kiz_withdrawn(
    status: Optional[str],
    status_ex: Optional[str] = None,
    raw_payload: Optional[dict] = None
) -> Tuple[bool, str]:
    """
    Проверяет, является ли КИЗ выведенным из оборота / выбывшим / списанным по спецификации ГИС МТ (Честный Знак).
    
    Returns:
        (is_withdrawn: bool, reason_message: str)
    """
    s = (status or "").upper().strip()
    sex = (status_ex or "").upper().strip()
    payload = raw_payload or {}

    # 1. Проверка основного статуса
    if s in CZ_WITHDRAWAL_STATUSES:
        desc = CZ_STATUS_DESCRIPTIONS.get(s, s)
        return True, f"Код маркировки выведен из оборота ({desc})"

    # 2. Проверка особого состояния (statusEx)
    if sex in CZ_WITHDRAWAL_STATUS_EX:
        return True, f"Особое состояние КИЗ указывает на выбытие ({sex})"

    # 3. Проверка флага markWithdraw (выбытие от невладельца по кассовому чеку)
    if payload.get("markWithdraw") is True or payload.get("mark_withdraw") is True:
        return True, "Зафиксировано выбытие КИЗ по чеку (markWithdraw: true)"

    # 4. Проверка причин выбытия в теле ответа
    if payload.get("withdrawReason") or payload.get("eliminationReason"):
        w_reason = payload.get("withdrawReason") or payload.get("eliminationReason")
        return True, f"Указана причина выбытия ({w_reason})"

    return False, ""


def extract_cz_item_info(cises_info: Any) -> Optional[dict]:
    """
    Извлекает валидный объект информации о КИЗ из ответа True API (/cises/info или /cises/short/list).
    Отсеивает записи с ошибками (404/504), находя запись с заполненным status / result / cisInfo.
    """
    if not cises_info:
        return None

    items = cises_info if isinstance(cises_info, list) else [cises_info]
    first_fallback = None

    for item in items:
        if not isinstance(item, dict):
            continue

        info = item.get("cisInfo") or item.get("result") or item
        if not isinstance(info, dict):
            continue

        st = info.get("status") or info.get("cisStatus")
        err_code = item.get("errorCode") or info.get("errorCode")

        # Если найден объект со статусом и без ошибки — это целевой результат
        if st and not err_code:
            # Сохраняем флаги и ошибки верхнего уровня, если они есть
            if "ogvs" in item and "ogvs" not in info:
                info["ogvs"] = item["ogvs"]
            if "markWithdraw" in item and "markWithdraw" not in info:
                info["markWithdraw"] = item["markWithdraw"]
            return info

        if not first_fallback and not err_code:
            first_fallback = info

    if first_fallback:
        return first_fallback

    if items and isinstance(items[0], dict):
        return items[0].get("cisInfo") or items[0].get("result") or items[0]

    return None


def parse_kiz_code(raw_code: str) -> Dict[str, Optional[str]]:
    """
    Разбирает код маркировки GS1 DataMatrix / SGTIN / КИЗ.

    Поддерживает форматы:
    - Стандартный с разделителями GS1: 0104630199251318215QTSRh>4sVc+.
    - Со скобками: (01)04630199251318(21)5QTSRh>4sVc+.
    - С символами-разделителями FNC1 / GS (\x1d / \u001d / \x1e)
    - Короткий с GTIN

    Returns:
        {
            "raw_code": str,
            "gtin": str (14 digits),
            "serial_number": str,
            "crypto_key": str or None,
            "crypto_tail": str or None,
            "clean_cis": str (01...21...)
        }
    """
    if not raw_code:
        return {
            "raw_code": "",
            "gtin": "",
            "serial_number": None,
            "crypto_key": None,
            "crypto_tail": None,
            "clean_cis": None,
        }

    code = raw_code.strip()

    normalized = re.sub(r'\(01\)', '01', code)
    normalized = re.sub(r'\(21\)', '21', normalized)
    normalized = re.sub(r'\(91\)', '91', normalized)
    normalized = re.sub(r'\(92\)', '92', normalized)

    gtin = ""
    serial = ""
    crypto_key = None
    crypto_tail = None

    if normalized.startswith("01") and len(normalized) >= 16:
        gtin = normalized[2:16]
        rest = normalized[16:]

        if rest.startswith("21"):
            rest = rest[2:]

        parts = re.split(r'[\x1d\x1e\x1f\u001d\u001e\u001f]', rest)
        serial_raw = parts[0]

        match_crypto = re.search(r'91(.{4})92(.+)$', serial_raw)
        if match_crypto:
            crypto_key = match_crypto.group(1)
            crypto_tail = match_crypto.group(2)
            serial = serial_raw[:match_crypto.start()]
        else:
            serial = serial_raw

        if len(parts) > 1:
            for p in parts[1:]:
                m_cr = re.search(r'^91(.{4})92(.+)$', p)
                if m_cr:
                    crypto_key = m_cr.group(1)
                    crypto_tail = m_cr.group(2)
                elif p.startswith("91") and len(p) >= 6:
                    crypto_key = p[2:6]
                    if len(p) > 6 and p[6:8] == "92":
                        crypto_tail = p[8:]
                elif p.startswith("92"):
                    crypto_tail = p[2:]

    elif len(normalized) >= 14 and normalized[:14].isdigit():
        gtin = normalized[:14]
        serial = normalized[14:] if len(normalized) > 14 else None
    else:
        m = re.search(r'(01)?(\d{14})21([^\x1d\x1e\s]+)', normalized)
        if m:
            gtin = m.group(2)
            serial = m.group(3)
        else:
            gtin = normalized[:14] if len(normalized) >= 14 else normalized

    clean_cis = f"01{gtin}21{serial}" if (gtin and serial) else (normalized if len(normalized) >= 20 else None)

    return {
        "raw_code": code,
        "gtin": gtin,
        "serial_number": serial,
        "crypto_key": crypto_key,
        "crypto_tail": crypto_tail,
        "clean_cis": clean_cis,
    }


async def resolve_kiz_product_info(
    kiz_code: str,
    seller: Optional[Seller] = None,
    order: Optional[Order] = None,
    db: Optional[AsyncSession] = None,
    force_refresh: bool = False,
) -> KizProductInfo:
    """
    Получает и сохраняет полную информацию о товаре по коду КИЗ (SGTIN).
    """
    parsed = parse_kiz_code(kiz_code)
    gtin = parsed["gtin"]
    serial = parsed["serial_number"]
    clean_cis = parsed["clean_cis"] or kiz_code.strip()

    kiz_info_obj: Optional[KizProductInfo] = None

    if db:
        res = await db.execute(
            select(KizProductInfo).where(KizProductInfo.kiz_code == kiz_code)
        )
        kiz_info_obj = res.scalars().first()

    if kiz_info_obj and not force_refresh:
        if order and kiz_info_obj.order_id != order.id and db:
            kiz_info_obj.order_id = order.id
            await db.flush()
        return kiz_info_obj

    if not kiz_info_obj:
        kiz_info_obj = KizProductInfo(
            id=str(uuid.uuid4()),
            kiz_code=kiz_code,
            gtin=gtin,
            serial_number=serial,
            clean_cis=clean_cis,
            seller_id=str(seller.id) if (seller and seller.id) else None,
            order_id=order.id if order else None,
            is_valid=True,
        )
        if db:
            db.add(kiz_info_obj)

    # 1. Enrich from WB Cards Catalog
    if seller and seller.wb_api_token_encrypted:
        try:
            token = decrypt(seller.wb_api_token_encrypted)
        except Exception:
            token = None

        if token:
            wb_client = WBClient(token)
            try:
                catalog = await wb_client.get_cards_catalog(limit=100)
                matched_card = None
                matched_size = None

                for vcode, card in catalog.get("by_vendor_code", {}).items():
                    for s in card.get("sizes", []):
                        skus = [str(sku).strip() for sku in s.get("skus", [])]
                        if gtin in skus or (len(gtin) == 14 and gtin.lstrip("0") in skus) or any(sku in gtin for sku in skus if len(sku) >= 8):
                            matched_card = card
                            matched_size = s
                            break
                    if matched_card:
                        break

                if not matched_card and order:
                    chrt = order.chrt_id
                    if chrt and chrt in catalog.get("by_chrt_id", {}):
                        cinfo = catalog["by_chrt_id"][chrt]
                        kiz_info_obj.product_name = kiz_info_obj.product_name or cinfo.get("title")
                        kiz_info_obj.brand = kiz_info_obj.brand or cinfo.get("brand")
                        kiz_info_obj.article = kiz_info_obj.article or cinfo.get("vendorCode")
                        kiz_info_obj.tech_size = kiz_info_obj.tech_size or cinfo.get("techSize")
                        kiz_info_obj.wb_size = kiz_info_obj.wb_size or cinfo.get("wbSize")
                        kiz_info_obj.tnved = kiz_info_obj.tnved or cinfo.get("tnved")

                if matched_card:
                    kiz_info_obj.product_name = matched_card.get("title") or kiz_info_obj.product_name
                    kiz_info_obj.brand = matched_card.get("brand") or kiz_info_obj.brand
                    kiz_info_obj.article = matched_card.get("vendorCode") or kiz_info_obj.article
                    kiz_info_obj.tnved = matched_card.get("tnved") or kiz_info_obj.tnved
                    if matched_size:
                        kiz_info_obj.tech_size = matched_size.get("techSize") or kiz_info_obj.tech_size
                        kiz_info_obj.wb_size = matched_size.get("wbSize") or kiz_info_obj.wb_size
            except Exception as e:
                logger.warning(f"Error resolving KIZ from WB cards catalog: {e}")
            finally:
                await wb_client.close()

    # 2. Enrich from Honest Sign (True API)
    if seller and seller.cz_inn:
        try:
            cz_token = decrypt(seller.cz_token_encrypted) if seller.cz_token_encrypted else None
        except Exception:
            cz_token = None

        if cz_token:
            cz_client = CZClient(inn=seller.cz_inn, token=cz_token)
            try:
                # В True API отправляем строго чистый clean_cis
                lookup_cises = [clean_cis] if clean_cis else ([kiz_code.strip()] if kiz_code else [])
                cises_info = await cz_client.get_cises_info(lookup_cises)
                
                info = extract_cz_item_info(cises_info)
                if info and isinstance(info, dict):
                    cz_st = info.get("status") or info.get("cisStatus")
                    if cz_st:
                        kiz_info_obj.cz_status = str(cz_st).upper().strip()
                    
                    st_ex = info.get("statusEx")
                    if st_ex:
                        kiz_info_obj.cz_status_ex = str(st_ex).strip()
                    elif kiz_info_obj.cz_status:
                        kiz_info_obj.cz_status_ex = CZ_STATUS_DESCRIPTIONS.get(kiz_info_obj.cz_status, kiz_info_obj.cz_status)

                    kiz_info_obj.cz_owner_inn = info.get("ownerInn") or seller.cz_inn
                    kiz_info_obj.cz_owner_name = info.get("ownerName")
                    kiz_info_obj.cz_producer_inn = info.get("producerInn")
                    kiz_info_obj.cz_producer_name = info.get("producerName")
                    kiz_info_obj.product_group = info.get("productGroup") or "lp"
                    kiz_info_obj.raw_cz_payload = info

                    if info.get("productName"):
                        kiz_info_obj.product_name = info.get("productName")
                    if info.get("brand"):
                        kiz_info_obj.brand = info.get("brand")
                    if info.get("tnVed") or info.get("tnVed10"):
                        kiz_info_obj.tnved = info.get("tnVed") or info.get("tnVed10")

                    em_str = info.get("emissionDate")
                    if em_str:
                        try:
                            kiz_info_obj.cz_emission_date = datetime.fromisoformat(em_str.replace("Z", "+00:00"))
                        except Exception:
                            pass
            except Exception as e:
                logger.debug(f"True API info lookup note: {e}")
            finally:
                await cz_client.__aexit__()

    # 3. Fallback defaults from order if still missing
    if order:
        if not kiz_info_obj.product_name:
            kiz_info_obj.product_name = order.name or order.subject
        if not kiz_info_obj.article:
            kiz_info_obj.article = order.article
        if not kiz_info_obj.brand:
            kiz_info_obj.brand = order.brand
        if not kiz_info_obj.tech_size:
            kiz_info_obj.tech_size = order.tech_size
        if not kiz_info_obj.wb_size:
            kiz_info_obj.wb_size = order.wb_size

    # Если продавец указан, но ИНН владельца еще не заполнен
    if not kiz_info_obj.cz_owner_inn and seller:
        kiz_info_obj.cz_owner_inn = seller.cz_inn

    # 4. Perform Cross-Validations for Product Checks
    validation_errors = []

    # 4.1. Проверка блокировок ОГВ (Органов государственной власти) согласно True API v719.0
    raw_payload = kiz_info_obj.raw_cz_payload or {}
    ogvs = raw_payload.get("ogvs") or []
    if ogvs:
        blocked_agencies = [OGV_AGENCIES_MAP.get(code, f"Госорган ({code})") for code in ogvs]
        validation_errors.append(
            f"Код маркировки заблокирован госорганами: {', '.join(blocked_agencies)}"
        )

    # 4.2. Проверка соответствия владельца КИЗ в ГИС МТ
    if seller and seller.cz_inn and kiz_info_obj.cz_owner_inn:
        if seller.cz_inn != kiz_info_obj.cz_owner_inn:
            validation_errors.append(
                f"Владелец КИЗ в ЧЗ ({kiz_info_obj.cz_owner_inn}) не совпадает с продавцом ({seller.cz_inn})"
            )

    # 4.3. Проверка статуса нахождения в обороте и выбытия (True API v719.0)
    withdrawn, withdraw_reason = is_kiz_withdrawn(
        status=kiz_info_obj.cz_status,
        status_ex=kiz_info_obj.cz_status_ex,
        raw_payload=raw_payload
    )
    if withdrawn:
        validation_errors.append(withdraw_reason)
    elif kiz_info_obj.cz_status in CZ_NOT_INTRODUCED_STATUSES:
        desc = CZ_STATUS_DESCRIPTIONS.get(kiz_info_obj.cz_status, kiz_info_obj.cz_status)
        validation_errors.append(f"Код маркировки еще не введен в оборот ({desc})")

    # 4.4. Проверка соответствия артикула и размера заказу WB
    if order:
        if kiz_info_obj.article and order.article and kiz_info_obj.article.lower() != order.article.lower():
            validation_errors.append(
                f"Артикул КИЗ ({kiz_info_obj.article}) не совпадает с артикулом заказа ({order.article})"
            )
        if kiz_info_obj.tech_size and order.tech_size and kiz_info_obj.tech_size.upper() != order.tech_size.upper():
            validation_errors.append(
                f"Размер КИЗ ({kiz_info_obj.tech_size}) не совпадает с размером заказа ({order.tech_size})"
            )

    kiz_info_obj.is_valid = len(validation_errors) == 0
    kiz_info_obj.validation_message = "; ".join(validation_errors) if validation_errors else "Товар прошел проверку соответствия"
    kiz_info_obj.checked_at = datetime.now(timezone.utc)

    if db:
        await db.flush()

    return kiz_info_obj
