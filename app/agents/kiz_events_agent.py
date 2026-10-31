"""
KIZ Events Celery Agent — WB FBS Manager
Периодически опрашивает источники WB и пополняет журнал kiz_events.
Таска только координирует: логика сборщиков живет в kiz_event_collectors и kiz_event_runner.
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.models.kiz_event import SOURCE_EXCISE, SOURCE_FINANCE, SOURCE_ITEM_RETURNS, SOURCE_ORDERS
from app.models.seller import Seller
from app.services.kiz_event_collectors import SOURCE_ORDER_STATUS
from app.services.kiz_event_runner import run_collector

logger = logging.getLogger(__name__)

ALL_SOURCES = [SOURCE_ORDER_STATUS, SOURCE_FINANCE, SOURCE_EXCISE, SOURCE_ITEM_RETURNS, SOURCE_ORDERS]


async def _collect(sources: List[str]) -> Dict[str, Any]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(Seller.id).where(Seller.is_active == True, Seller.wb_api_token_encrypted.isnot(None))
        )
        seller_ids = [str(sid) for sid in rows.scalars().all()]

    results: Dict[str, Any] = {}
    for seller_id in seller_ids:
        for source in sources:
            # A separate session per source: a failure in one must not affect the others
            async with AsyncSessionLocal() as db:
                seller = await db.get(Seller, seller_id)
                if seller:
                    results[f"{seller_id}:{source}"] = await run_collector(db, seller, source)
    return results


@celery_app.task(name="app.agents.kiz_events_agent.collect_kiz_events", queue="cz_operations")
def collect_kiz_events(sources: Optional[List[str]] = None) -> Dict[str, Any]:
    """Collects the given sources for all active sellers, or all sources when none are given."""
    return asyncio.run(_collect(sources or ALL_SOURCES))
