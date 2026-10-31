"""
Unified KIZ History — Helpers for unified_kiz_batch_service.
Adds events from pending batches to the KIZ history and merges True API records.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

from app.services.kiz_service import parse_kiz_code


def normalize_event_dt(dt: Any) -> datetime:
    if isinstance(dt, datetime):
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    if hasattr(dt, "year") and hasattr(dt, "month") and hasattr(dt, "day"):
        from datetime import date
        if isinstance(dt, date):
            return datetime.combine(dt, datetime.min.time(), tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _pending_item_event(item: Dict[str, Any], event_type: str, created_at: Any, now_utc: datetime) -> tuple:
    raw_k = item.get("kiz_code")
    clean_cis = parse_kiz_code(raw_k).get("clean_cis") or raw_k
    date_str = str(item.get("receipt_date") or "").strip()
    try:
        ev_date = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        ev_date = created_at or now_utc
    event = {
        "source": "archive_upload",
        "type": event_type,
        "date": ev_date,
        "order_id": item.get("order_id"),
        "price": float(item.get("price") or 0.0),
        "article": str(item.get("article") or ""),
        "name": item.get("name") or "",
        "raw_kiz": raw_k,
        "receipt_number": item.get("receipt_number"),
        "receipt_date": item.get("receipt_date"),
    }
    if event_type == "SALE":
        event["fn_number"] = item.get("fn_number")
    return clean_cis, event


def add_pending_batch_events(
    history_by_cis: Dict[str, List[Dict[str, Any]]],
    pending_batches: Iterable[Any],
    now_utc: datetime,
) -> None:
    """Добавляет в историю КИЗ позиции из пакетов, ожидающих подписи."""
    for batch in pending_batches:
        payload = batch.data_payload or {}
        for item in payload.get("returns", []):
            if not item.get("needs_cz_return", True) or not item.get("selected", True) or not item.get("kiz_code"):
                continue
            cis, event = _pending_item_event(item, "RETURN", batch.created_at, now_utc)
            history_by_cis.setdefault(cis, []).append(event)
        for item in payload.get("withdrawals", []):
            if not item.get("needs_withdrawal", True) or not item.get("selected", True) or not item.get("kiz_code"):
                continue
            cis, event = _pending_item_event(item, "SALE", batch.created_at, now_utc)
            history_by_cis.setdefault(cis, []).append(event)


def merge_cz_records(k_info_map: Dict[str, Any], cz_info_map: Dict[str, Dict[str, Any]]) -> None:
    """Переносит записи kiz_product_info в словарь сведений True API."""
    for cis_key, rec in k_info_map.items():
        if not rec:
            continue
        info = dict(rec.raw_cz_payload or {})
        if rec.cz_status:
            info["status"] = rec.cz_status
        if rec.cz_owner_inn:
            info["ownerInn"] = rec.cz_owner_inn
        if rec.cz_owner_name:
            info["ownerName"] = rec.cz_owner_name
        if rec.cz_producer_inn:
            info["producerInn"] = rec.cz_producer_inn
        if rec.cz_status_ex:
            info["statusEx"] = rec.cz_status_ex

        cz_info_map[cis_key] = info
        if rec.clean_cis:
            cz_info_map[rec.clean_cis] = info
        if rec.kiz_code:
            cz_info_map[rec.kiz_code] = info
