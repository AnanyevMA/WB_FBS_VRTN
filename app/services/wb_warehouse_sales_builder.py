"""
WB Warehouse Sales and Returns Builder — WB FBS Manager.
Constructs normalized item payloads for KizSignatureBatch (WITHDRAWAL and RETURN).
Pure builder module with no external side-effects. Strict limit: < 120 lines.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from app.models.order import Order


def extract_owner_inn(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    """Извлекает ИНН текущего владельца КИЗ из ответа True API."""
    if not payload or not isinstance(payload, dict):
        return None
    c_info = payload.get("cisInfo") if isinstance(payload.get("cisInfo"), dict) else payload
    inn = c_info.get("ownerInn") or c_info.get("owner_inn") or payload.get("ownerInn") or payload.get("owner_inn")
    return str(inn).strip() if inn else None


def _resolve_fields(record, clean_cis, matched_order, kw) -> Tuple:
    if isinstance(record, str) and kw.get("cis") is None:
        kw["cis"], record = record, None
    rec = record or {}
    kiz = str(kw.get("cis") or clean_cis or rec.get("excise_short") or "").strip()
    srid_val = str(kw.get("srid") or rec.get("srid") or "").strip()
    nm_val = str(kw.get("nm_id") or rec.get("nm_id") or (matched_order.nm_id if matched_order and matched_order.nm_id else "")).strip()
    raw_doc = kw.get("fiscal_doc_number") if kw.get("fiscal_doc_number") is not None else rec.get("fiscal_doc_number")
    doc_num = str(raw_doc).strip() if raw_doc not in (None, "", 0, "0") else ""
    fn_num = str(kw.get("fiscal_drive_number") or rec.get("fiscal_drive_number") or "").strip()
    doc_date = str(kw.get("fiscal_dt") or rec.get("fiscal_dt") or "").strip()
    price_val = float(kw.get("price") if kw.get("price") is not None else (rec.get("price") or (matched_order.price if matched_order and matched_order.price else 0.0)) or 0.0)
    oid = matched_order.id if matched_order else kw.get("order_id")
    art = matched_order.article if matched_order and matched_order.article else nm_val
    bcode = str(kw.get("barcode") or rec.get("barcode") or "")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return kiz, srid_val, nm_val, doc_num, fn_num, doc_date, price_val, oid, art, bcode, today


def build_warehouse_sale_item(
    record: Optional[Dict[str, Any]] = None,
    clean_cis: Optional[str] = None,
    matched_order: Optional[Order] = None,
    **kw: Any,
) -> Dict[str, Any]:
    """Формирует структуру элемента вывода (WITHDRAWAL) для пакета ЭЦП."""
    kiz, srid, nm, doc, fn, dt, price, oid, art, bcode, today = _resolve_fields(record, clean_cis, matched_order, kw)
    name_val = matched_order.name if matched_order and matched_order.name else f"Товар со склада WB (Арт. {nm})"
    return {
        "order_id": oid, "sticker_id": matched_order.sticker_id if matched_order else None,
        "ki": kiz, "kiz_code": kiz, "clean_cis": kiz, "action": "WITHDRAWAL", "doc_type": "LK_RECEIPT",
        "receipt_number": doc, "fn_number": fn, "receipt_date": dt or today,
        "primary_document_number": doc or str(oid or "1"), "primary_document_type": "RECEIPT",
        "primary_document_date": dt or today, "document_type": "OTHER",
        "primary_document_custom_name": f"Фискальный чек WB (ФД №{doc}, ФН №{fn})",
        "price": price, "price_kopecks": int(round(price * 100)),
        "article": art, "name": name_val, "task_status": "Продажа со склада WB (FBO)",
        "srid": srid, "barcode": bcode,
        "db_status": matched_order.status.value if matched_order else "FBO_WAREHOUSE",
        "db_kiz_status": matched_order.kiz_status.value if matched_order else "UNATTACHED",
        "cz_status": "INTRODUCED", "cz_status_desc": "В обороте на балансе ИП",
        "is_already_withdrawn": False, "needs_withdrawal": True, "selected": True,
    }


def build_warehouse_return_item(
    record: Optional[Dict[str, Any]] = None,
    clean_cis: Optional[str] = None,
    matched_order: Optional[Order] = None,
    **kw: Any,
) -> Dict[str, Any]:
    """Формирует структуру элемента возврата (RETURN / LP_RETURN) для пакета ЭЦП."""
    kiz, srid, nm, doc, fn, dt, price, oid, art, bcode, today = _resolve_fields(record, clean_cis, matched_order, kw)
    name_val = matched_order.name if matched_order and matched_order.name else f"Возврат товара WB (Арт. {nm})"
    return {
        "order_id": oid, "sticker_id": matched_order.sticker_id if matched_order else None,
        "ki": kiz, "kiz_code": kiz, "clean_cis": kiz, "action": "RETURN", "doc_type": "LP_RETURN",
        "receipt_number": doc or str(oid or srid or "1"), "fn_number": fn, "receipt_date": dt or today,
        "primary_document_number": doc or str(srid or oid or "1"), "primary_document_type": "RECEIPT",
        "primary_document_date": dt or today, "document_type": "OTHER",
        "primary_document_custom_name": f"Возвратный чек WB (ФД №{doc}, ФН №{fn})" if doc else f"Возврат WB (SRID: {srid or nm})",
        "price": price, "price_kopecks": int(round(price * 100)),
        "article": art, "name": name_val, "task_status": "Возврат со склада WB (FBO)",
        "srid": srid, "barcode": bcode,
        "db_status": matched_order.status.value if matched_order else "RETURNED",
        "db_kiz_status": matched_order.kiz_status.value if matched_order else "WITHDRAWN",
        "cz_status": "RETIRED", "cz_status_desc": "Выведен из оборота (требует возврата LP_RETURN)",
        "is_already_in_circulation": False, "needs_cz_return": True, "is_seller_owner": True, "selected": True,
    }
