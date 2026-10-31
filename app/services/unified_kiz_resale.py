"""
Unified KIZ Resale — Repeat sales of a KIZ for unified_kiz_batch_service.
A past withdrawal does not cover a sale made after the code came back to circulation.
A code counts as resold only with a WB sale without a return and a live True API status.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kiz import KizOperation, KizOperationType
from app.models.order import KizStatus, Order
from app.models.seller import Seller
from app.services.kiz_service import is_kiz_withdrawn, parse_kiz_code
from app.services.unified_kiz_history import merge_cz_records, normalize_event_dt

logger = logging.getLogger(__name__)

WB_SALE_SOURCES = ("wb_excise_report", "wb_finance")


@dataclass
class KizOperationState:
    withdrawn_cises: Set[str] = field(default_factory=set)
    returned_cises: Set[str] = field(default_factory=set)
    pending_withdrawal_cises: Set[str] = field(default_factory=set)
    last_withdrawal_at: Dict[str, datetime] = field(default_factory=dict)


@dataclass
class ResaleCheck:
    confirmed: Set[str] = field(default_factory=set)
    needs_review: Set[str] = field(default_factory=set)
    failed: bool = False


async def load_kiz_operation_state(db: AsyncSession, seller_id: str) -> KizOperationState:
    """Собирает КИЗ с уже отправленными или выполненными операциями маркировки."""
    stmt = select(KizOperation.kiz_code, KizOperation.operation, KizOperation.status, KizOperation.created_at).where(
        KizOperation.seller_id == seller_id,
        KizOperation.operation.in_([KizOperationType.WITHDRAWAL, KizOperationType.RETURN]),
        KizOperation.status.in_(["SUCCESS", "IN_PROGRESS"]),
        (KizOperation.cz_doc_status.is_(None) | (KizOperation.cz_doc_status != "CHECKED_NOT_OK")),
    )
    state = KizOperationState()
    for kiz_code, operation, status, created_at in (await db.execute(stmt)).all():
        if not kiz_code:
            continue
        cis = parse_kiz_code(kiz_code).get("clean_cis") or kiz_code
        if operation != KizOperationType.WITHDRAWAL:
            state.returned_cises.add(cis)
            continue
        state.withdrawn_cises.add(cis)
        if status == "IN_PROGRESS":
            state.pending_withdrawal_cises.add(cis)
        created = normalize_event_dt(created_at)
        if cis not in state.last_withdrawal_at or created > state.last_withdrawal_at[cis]:
            state.last_withdrawal_at[cis] = created
    return state


def is_order_withdrawn(order: Optional[Order]) -> bool:
    """Заказ FBS показывает, что КИЗ списан или документ вывода в обработке."""
    if not order:
        return False
    if order.kiz_status == KizStatus.WITHDRAWN:
        return True
    if order.cz_withdrawal_doc_id and order.cz_doc_status in ("IN_PROGRESS", "CHECKED_OK", "SUCCESS"):
        return True
    return order.cz_doc_status == "CHECKED_OK"


def latest_unreturned_wb_sale(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Последняя продажа из отчетов WB, если после нее и по ее srid возврата нет."""
    sales = [e for e in events if e["type"] == "SALE" and e.get("source") in WB_SALE_SOURCES and e.get("srid")]
    if not sales:
        return None
    sale = max(sales, key=lambda e: normalize_event_dt(e["date"]))
    sale_dt = normalize_event_dt(sale["date"])
    for e in events:
        if e["type"] != "RETURN":
            continue
        if e.get("srid") == sale["srid"] or normalize_event_dt(e["date"]) > sale_dt:
            return None
    return sale


def _find_resale_candidates(
    sales_candidates: Dict[str, Dict[str, Any]],
    history_by_cis: Dict[str, List[Dict[str, Any]]],
    cz_info_map: Dict[str, Dict[str, Any]],
    fbs_order_lookup: Dict[str, Order],
    ops: KizOperationState,
    seller_inn: str,
) -> List[str]:
    candidates = []
    for cis, ev in sales_candidates.items():
        order = ev.get("order") or fbs_order_lookup.get(cis)
        if cis not in ops.withdrawn_cises and not is_order_withdrawn(order):
            continue
        if cis in ops.pending_withdrawal_cises:
            continue
        if order and order.cz_withdrawal_doc_id and order.cz_doc_status == "IN_PROGRESS":
            continue
        cz_item = cz_info_map.get(cis, {})
        if cz_item.get("status") != "INTRODUCED" or (cz_item.get("ownerInn") or "").strip() != seller_inn:
            continue
        if latest_unreturned_wb_sale(history_by_cis.get(cis, [])):
            candidates.append(cis)
    return candidates


def _is_live_in_circulation(rec: Any, checked_from: datetime, seller_inn: str) -> bool:
    if not rec or not rec.checked_at or normalize_event_dt(rec.checked_at) < checked_from:
        return False
    raw = rec.raw_cz_payload or {}
    owner_inn = (rec.cz_owner_inn or raw.get("ownerInn") or "").strip()
    withdrawn, _ = is_kiz_withdrawn(status=rec.cz_status, status_ex=rec.cz_status_ex, raw_payload=raw)
    return rec.cz_status == "INTRODUCED" and owner_inn == seller_inn and not withdrawn


async def confirm_resales(
    seller: Seller,
    db: AsyncSession,
    verify: Callable[..., Awaitable[Dict[str, Any]]],
    sales_candidates: Dict[str, Dict[str, Any]],
    history_by_cis: Dict[str, List[Dict[str, Any]]],
    cz_info_map: Dict[str, Dict[str, Any]],
    fbs_order_lookup: Dict[str, Order],
    ops: KizOperationState,
) -> ResaleCheck:
    """Подтверждает повторные продажи живым запросом в True API и подставляет продажу WB в кандидаты."""
    seller_inn = (seller.cz_inn or "").strip()
    check = ResaleCheck()
    if not seller_inn:
        return check
    candidates = _find_resale_candidates(sales_candidates, history_by_cis, cz_info_map, fbs_order_lookup, ops, seller_inn)
    if not candidates:
        return check

    checked_from = datetime.now(timezone.utc)
    try:
        records = await verify(db=db, seller=seller, kiz_codes=candidates, force_refresh=True)
    except Exception as e:
        logger.error(f"Could not verify resold KIZ in True API for seller {seller.id}: {e}")
        check.failed = True
        return check

    merge_cz_records(records, cz_info_map)
    for cis in candidates:
        if not _is_live_in_circulation(records.get(cis), checked_from, seller_inn):
            continue
        sale = latest_unreturned_wb_sale(history_by_cis[cis])
        check.confirmed.add(cis)
        sales_candidates[cis] = sale
        # Документ вывода отправлен уже после этой продажи, а затем код вернули в оборот
        withdrawn_at = ops.last_withdrawal_at.get(cis)
        if withdrawn_at and withdrawn_at >= normalize_event_dt(sale["date"]):
            check.needs_review.add(cis)
    return check
