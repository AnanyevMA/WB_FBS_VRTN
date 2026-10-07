"""
WB Finance Celery Agent — WB FBS Manager
Периодическая синхронизация детальных финансовых отчетов реализации WB (раз в сутки).
Сохранение в БД и аудит КИЗ возвратов в Честном Знаке.
"""
import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Dict

import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.database import AsyncSessionLocal
from app.models.seller import Seller
from app.models.audit import AuditLog
from app.services.wb_finance_service import sync_seller_financial_reports

logger = logging.getLogger(__name__)
sync_engine = create_engine(settings.database_url_sync)


@celery_app.task(
    name="app.agents.wb_finance_agent.sync_all_sellers_financial_reports",
    queue="cz_operations",
    bind=True,
    max_retries=1,
)
def sync_all_sellers_financial_reports(self=None) -> Dict[str, Any]:
    """
    Периодическая задача Celery Beat (ежедневно ночью в 05:00 МСК).
    Запускает сбор детального финансового отчета реализации WB по всем активным продавцам (days=14).
    """
    now_utc = datetime.now(timezone.utc)
    task_id = getattr(getattr(self, "request", None), "id", None) or str(uuid.uuid4())
    audit_id = str(uuid.uuid4())
    target_seller_ids = []

    with Session(sync_engine) as db:
        # Фиксация статуса STARTED перед началом выполнения
        audit_log = AuditLog(
            id=audit_id,
            seller_id=None,
            agent="wb_finance_agent",
            action="SYNC_ALL_FINANCIAL_REPORTS",
            entity_type="celery_task",
            entity_id=task_id,
            payload={"status": "STARTED", "days": 14, "started_at": now_utc.isoformat()},
            trace_id=task_id,
            created_at=now_utc,
        )
        db.add(audit_log)
        db.commit()

        try:
            sellers = db.execute(
                select(Seller).where(
                    Seller.is_active == True,
                    Seller.wb_api_token_encrypted.isnot(None),
                )
            ).scalars().all()

            for s in sellers:
                target_seller_ids.append(str(s.id))

            logger.info(f"[WB Finance Beat] Dispatching {len(target_seller_ids)} sellers for financial reports sync (days=14)")

            dispatched = 0
            for sid in target_seller_ids:
                sync_seller_financial_reports_task.delay(seller_id=sid, days=14)
                dispatched += 1

            # Обновление статуса на COMPLETED при успешном завершении
            audit_log.payload = {
                "status": "COMPLETED",
                "days": 14,
                "dispatched": dispatched,
                "sellers_count": len(target_seller_ids),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            db.commit()
            return {"checked_at": now_utc.isoformat(), "dispatched": dispatched, "status": "COMPLETED"}
        except Exception as exc:
            audit_log.error = str(exc)
            audit_log.payload = {
                **(audit_log.payload or {}),
                "status": "FAILED",
                "failed_at": datetime.now(timezone.utc).isoformat(),
            }
            db.commit()
            logger.error(f"[WB Finance Beat] Error dispatching financial reports: {exc}")
            raise


@celery_app.task(
    name="app.agents.wb_finance_agent.sync_seller_financial_reports_task",
    queue="cz_operations",
    bind=True,
    max_retries=2,
    default_retry_delay=180,
)
def sync_seller_financial_reports_task(self=None, seller_id: str = "", days: int = 14) -> Dict[str, Any]:
    """
    Фоновая задача загрузки финансового отчета реализации WB для конкретного продавца.
    """
    task_id = getattr(getattr(self, "request", None), "id", None) or str(uuid.uuid4())
    if self and hasattr(self, "update_state"):
        self.update_state(state="STARTED", meta={"status": "STARTED", "seller_id": seller_id, "days": days})

    async def _run():
        async with AsyncSessionLocal() as db:
            seller = await db.get(Seller, seller_id)
            if not seller or not seller.is_active or not seller.wb_api_token_encrypted:
                return {"success": False, "error": f"Seller {seller_id} not eligible"}

            return await sync_seller_financial_reports(
                seller=seller,
                db=db,
                days=days,
                verify_cz=True,
                trace_id=task_id,
            )

    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                res = executor.submit(asyncio.run, _run()).result()
        else:
            res = asyncio.run(_run())

        if self and hasattr(self, "update_state"):
            self.update_state(state="SUCCESS", meta={"status": "COMPLETED", "seller_id": seller_id, "result": res})
        return res
    except Exception as exc:
        logger.error(f"[WB Finance Task] Error for seller {seller_id}: {exc}")
        if self and hasattr(self, "request") and self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=180)
        return {"success": False, "error": str(exc), "status": "FAILED"}
