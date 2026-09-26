"""
WB Finance Celery Agent — WB FBS Manager
Периодическая синхронизация детальных финансовых отчетов реализации WB (раз в сутки).
Сохранение в БД и аудит КИЗ возвратов в Честном Знаке.
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
from app.services.wb_finance_service import sync_seller_financial_reports

logger = logging.getLogger(__name__)
sync_engine = create_engine(settings.database_url_sync)


@celery_app.task(
    name="app.agents.wb_finance_agent.sync_all_sellers_financial_reports",
    queue="cz_operations",
    bind=True,
    max_retries=1,
)
def sync_all_sellers_financial_reports(self) -> Dict[str, Any]:
    """
    Периодическая задача Celery Beat (ежедневно ночью в 05:00 МСК).
    Запускает сбор детального финансового отчета реализации WB по всем активным продавцам.
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

    logger.info(f"[WB Finance Beat] Dispatching {len(target_seller_ids)} sellers for financial reports sync")

    dispatched = 0
    for sid in target_seller_ids:
        sync_seller_financial_reports_task.delay(seller_id=sid, days=30)
        dispatched += 1

    return {"checked_at": now_utc.isoformat(), "dispatched": dispatched}


@celery_app.task(
    name="app.agents.wb_finance_agent.sync_seller_financial_reports_task",
    queue="cz_operations",
    bind=True,
    max_retries=2,
    default_retry_delay=180,
)
def sync_seller_financial_reports_task(self, seller_id: str, days: int = 30) -> Dict[str, Any]:
    """
    Фоновая задача загрузки финансового отчета реализации WB для конкретного продавца.
    """
    async def _run():
        async with AsyncSessionLocal() as db:
            seller = await db.get(Seller, seller_id)
            if not seller or not seller.is_active or not seller.wb_api_token_encrypted:
                return {"success": False, "error": f"Seller {seller_id} not eligible"}

            return await sync_seller_financial_reports(seller=seller, db=db, days=days, verify_cz=True)

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
        logger.error(f"[WB Finance Task] Error for seller {seller_id}: {exc}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=180)
        return {"success": False, "error": str(exc)}
