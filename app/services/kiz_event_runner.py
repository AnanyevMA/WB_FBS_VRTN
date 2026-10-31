"""
KIZ Event Runner — WB FBS Manager
Запускает сборщик источника, сохраняет события и фиксирует результат в журнале аудита.
"""
import logging
from typing import Any, Awaitable, Callable, Dict

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.kiz_event import SOURCE_EXCISE, SOURCE_FINANCE, SOURCE_ITEM_RETURNS, SOURCE_ORDERS
from app.models.seller import Seller
from app.services.kiz_event_collectors import (
    SOURCE_ORDER_STATUS,
    CollectResult,
    collect_excise,
    collect_finance,
    collect_item_returns,
    collect_orders,
    refresh_delivered_order_statuses,
)
from app.services.kiz_event_service import save_events

logger = logging.getLogger(__name__)

COLLECTORS: Dict[str, Callable[[AsyncSession, Seller], Awaitable[CollectResult]]] = {
    SOURCE_FINANCE: collect_finance,
    SOURCE_EXCISE: collect_excise,
    SOURCE_ITEM_RETURNS: collect_item_returns,
    SOURCE_ORDERS: collect_orders,
    SOURCE_ORDER_STATUS: refresh_delivered_order_statuses,
}
# Runs every 30 minutes from local data: an audit row per run would only add noise
QUIET_SOURCES = {SOURCE_ORDERS}


async def run_collector(db: AsyncSession, seller: Seller, source: str) -> Dict[str, Any]:
    """Runs one collector and records the outcome. Zero rows is reported as no_data, not as success."""
    seller_id = str(seller.id)
    payload: Dict[str, Any] = {"source": source}
    error = None
    try:
        result = await COLLECTORS[source](db, seller)
        counts = await save_events(db, seller_id, result.drafts)
        payload.update(counts, seen=result.seen, skipped=result.skipped, changed=result.changed)
        payload["status"] = "ok" if result.seen else "no_data"
        await db.commit()
    except Exception as exc:
        await db.rollback()
        error = f"{type(exc).__name__}: {exc}"[:500]
        payload["status"] = "error"
        logger.warning(f"[KIZ Events] {source} failed for seller {seller_id}: {error}")

    if error or source not in QUIET_SOURCES or payload.get("inserted"):
        db.add(AuditLog(
            seller_id=seller_id, agent="kiz_events_agent", action="KIZ_EVENTS_SYNC",
            entity_type="seller", entity_id=seller_id, payload=payload, error=error,
        ))
        await db.commit()
    return payload
