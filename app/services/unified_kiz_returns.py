"""
Unified KIZ Returns — Confirmation of WB returns for unified_kiz_batch_service.
A past return does not cover a later return of the same KIZ. A code is proposed again
only when the last event is a WB return and True API confirms it is retired on the seller balance.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Set

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.seller import Seller
from app.services.unified_kiz_history import merge_cz_records, normalize_event_dt
from app.services.unified_kiz_resale import KizOperationState

logger = logging.getLogger(__name__)

WB_RETURN_SOURCES = ("wb_sales_report", "wb_finance", "wb_finance_srid")


@dataclass
class ReturnCheck:
    confirmed: Set[str] = field(default_factory=set)
    needs_review: Set[str] = field(default_factory=set)
    failed: bool = False


def _find_return_candidates(
    return_candidates: Dict[str, Dict[str, Any]],
    cz_info_map: Dict[str, Dict[str, Any]],
    seller_inn: str,
) -> List[str]:
    candidates = []
    for cis, ev in return_candidates.items():
        cz_item = cz_info_map.get(cis, {})
        if ev.get("source") not in WB_RETURN_SOURCES or cz_item.get("status") != "RETIRED":
            continue
        if (cz_item.get("ownerInn") or "").strip() == seller_inn:
            candidates.append(cis)
    return candidates


def _is_live_retired(rec: Any, checked_from: datetime, seller_inn: str) -> bool:
    if not rec or not rec.checked_at or normalize_event_dt(rec.checked_at) < checked_from:
        return False
    owner_inn = (rec.cz_owner_inn or (rec.raw_cz_payload or {}).get("ownerInn") or "").strip()
    return rec.cz_status == "RETIRED" and owner_inn == seller_inn


async def confirm_returns(
    seller: Seller,
    db: AsyncSession,
    verify: Callable[..., Awaitable[Dict[str, Any]]],
    return_candidates: Dict[str, Dict[str, Any]],
    cz_info_map: Dict[str, Dict[str, Any]],
    ops: KizOperationState,
) -> ReturnCheck:
    """Подтверждает возвраты WB живым запросом в True API."""
    seller_inn = (seller.cz_inn or "").strip()
    check = ReturnCheck()
    if not seller_inn:
        return check
    candidates = _find_return_candidates(return_candidates, cz_info_map, seller_inn)
    if not candidates:
        return check

    checked_from = datetime.now(timezone.utc)
    try:
        records = await verify(db=db, seller=seller, kiz_codes=candidates, force_refresh=True)
    except Exception as e:
        logger.error(f"Could not verify returned KIZ in True API for seller {seller.id}: {e}")
        check.failed = True
        return check

    merge_cz_records(records, cz_info_map)
    for cis in candidates:
        if not _is_live_retired(records.get(cis), checked_from, seller_inn):
            continue
        check.confirmed.add(cis)
        # Вывод отправлен уже после возврата: возможна продажа, которой еще нет в отчетах
        withdrawn_at = ops.last_withdrawal_at.get(cis)
        if withdrawn_at and withdrawn_at >= normalize_event_dt(return_candidates[cis]["date"]):
            check.needs_review.add(cis)
    return check
