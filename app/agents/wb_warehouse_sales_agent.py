"""
WB Warehouse Sales Celery Agent — WB FBS Manager
Периодический опрос отчетов маркировки WB (excise-report) для выявления и вывода
из оборота товаров, повторно проданных со склада WB (FBO / остатки после возвратов).
"""
import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Dict

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.database import AsyncSessionLocal
from app.models.seller import Seller
from app.services.wb_warehouse_sales_service import process_warehouse_sales_for_seller

logger = logging.getLogger(__name__)
sync_engine = create_engine(settings.database_url_sync)


@celery_app.task(
    name="app.agents.wb_warehouse_sales_agent.sync_all_sellers_warehouse_sales",
    queue="cz_operations",
    bind=True,
    max_retries=1,
)
def sync_all_sellers_warehouse_sales(self) -> Dict[str, Any]:
    """
    Периодическая задача Celery Beat (ежедневно ночью, например в 04:30).
    Запускает проверку отчета маркировки WB по всем активным продавцам с токеном WB.
    """
    now_utc = datetime.now(timezone.utc)
    target_seller_ids = []

    with Session(sync_engine) as db:
        sellers = db.execute(
            select(Seller).where(
                Seller.is_active == True,
                Seller.wb_api_token_encrypted.isnot(None),
            )
        ).scalars().all()

        for s in sellers:
            target_seller_ids.append(str(s.id))

    logger.info(f"[Warehouse Sales Beat] Disagreeing {len(target_seller_ids)} sellers for warehouse sales sync")

    dispatched = 0
    for sid in target_seller_ids:
        sync_seller_warehouse_sales.delay(seller_id=sid, days=14)
        dispatched += 1

    return {"checked_at": now_utc.isoformat(), "dispatched": dispatched}


@celery_app.task(
    name="app.agents.wb_warehouse_sales_agent.sync_seller_warehouse_sales",
    queue="cz_operations",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
)
def sync_seller_warehouse_sales(self, seller_id: str, days: int = 14) -> Dict[str, Any]:
    """
    Фоновая задача обработки продаж со склада WB для конкретного продавца.
    """
    async def _run():
        async with AsyncSessionLocal() as db:
            seller = await db.get(Seller, seller_id)
            if not seller or not seller.is_active or not seller.wb_api_token_encrypted:
                return {"success": False, "error": f"Seller {seller_id} not eligible"}

            return await process_warehouse_sales_for_seller(seller=seller, db=db, days=days)

    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(asyncio.run, _run()).result()
        else:
            return asyncio.run(_run())
    except Exception as exc:
        logger.error(f"[Warehouse Sales Task] Error for seller {seller_id}: {exc}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=120)
        return {"success": False, "error": str(exc)}
