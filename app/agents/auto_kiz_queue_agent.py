"""
Auto KIZ Queue Agent — WB FBS Manager
Периодический Celery-агент суточного формирования очереди КИЗ в настраиваемое время (по умолчанию 17:00).
"""
import asyncio
from datetime import datetime, timezone, timedelta
import logging
from typing import Optional, Dict, Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.database import AsyncSessionLocal
from app.models.seller import Seller
from app.services.time_service import resolve_timezone
from app.services.auto_kiz_queue_service import process_auto_kiz_queue_for_seller

logger = logging.getLogger(__name__)
sync_engine = create_engine(settings.database_url_sync)

# In-memory трекер для защиты от повторного выполнения в течение тех же суток:
# seller_id -> "YYYY-MM-DD" (по местному времени продавца)
_auto_queue_runs_today: Dict[str, str] = {}


def is_seller_auto_queue_due(
    seller: Seller,
    now_utc: datetime,
    grace_hours: float = 3.0,
) -> bool:
    """
    Проверяет, наступило ли локальное время (по умолчанию 17:00) для формирования очереди КИЗ.
    Учитывает индивидуальный часовой пояс продавца и факт выполнения сегодня.
    """
    if not getattr(seller, "auto_kiz_queue_enabled", True):
        return False

    seller_tz = resolve_timezone(getattr(seller, "timezone", "Europe/Moscow"))
    seller_local_now = now_utc.astimezone(seller_tz)
    local_today_str = seller_local_now.strftime("%Y-%m-%d")

    # 1. Проверка in-memory кэша
    if _auto_queue_runs_today.get(str(seller.id)) == local_today_str:
        return False

    # 2. Проверка даты последнего запуска в БД
    last_run_at = getattr(seller, "last_auto_kiz_queue_at", None)
    if last_run_at:
        if last_run_at.tzinfo is None:
            last_run_at = last_run_at.replace(tzinfo=timezone.utc)
        last_run_local = last_run_at.astimezone(seller_tz)
        if last_run_local.strftime("%Y-%m-%d") == local_today_str:
            _auto_queue_runs_today[str(seller.id)] = local_today_str
            return False

    # 3. Проверка целевого времени (по умолчанию 17:00)
    target_hour = int(getattr(seller, "auto_kiz_queue_hour", 17) or 17)
    target_minute = int(getattr(seller, "auto_kiz_queue_minute", 0) or 0)

    target_dt = datetime(
        year=seller_local_now.year,
        month=seller_local_now.month,
        day=seller_local_now.day,
        hour=target_hour,
        minute=target_minute,
        tzinfo=seller_tz,
    )

    # Ещё не наступило целевое время
    if seller_local_now < target_dt:
        return False

    # Превышено окно доставки (grace window)
    if seller_local_now >= target_dt + timedelta(hours=grace_hours):
        return False

    return True


@celery_app.task(
    name="app.agents.auto_kiz_queue_agent.check_and_run_auto_kiz_queue",
    queue="cz_operations",
    bind=True,
    max_retries=1,
)
def check_and_run_auto_kiz_queue(self) -> Dict[str, Any]:
    """
    Периодическая задача Celery Beat (запускается раз в минуту).
    Проверяет всех активных продавцов и запускает формирование очереди КИЗ
    в установленное для каждого продавца время (по умолчанию 17:00).
    """
    now_utc = datetime.now(timezone.utc)
    due_seller_ids = []

    with Session(sync_engine) as db:
        sellers = db.execute(
            select(Seller).where(
                Seller.is_active == True,
                Seller.auto_kiz_queue_enabled == True,
            )
        ).scalars().all()

        for seller in sellers:
            if is_seller_auto_queue_due(seller, now_utc):
                due_seller_ids.append(str(seller.id))
                # Отмечаем в памяти, чтобы не спамить в течение минуты
                seller_tz = resolve_timezone(getattr(seller, "timezone", "Europe/Moscow"))
                local_today = now_utc.astimezone(seller_tz).strftime("%Y-%m-%d")
                _auto_queue_runs_today[str(seller.id)] = local_today

    logger.info(f"[Auto KIZ Beat] Found {len(due_seller_ids)} sellers due for daily KIZ queue")

    dispatched = 0
    for sid in due_seller_ids:
        trigger_seller_auto_kiz_queue.delay(seller_id=sid, trigger_source="auto")
        dispatched += 1

    return {"checked_at": now_utc.isoformat(), "dispatched": dispatched}


@celery_app.task(
    name="app.agents.auto_kiz_queue_agent.trigger_seller_auto_kiz_queue",
    queue="cz_operations",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def trigger_seller_auto_kiz_queue(self, seller_id: str, trigger_source: str = "auto") -> Dict[str, Any]:
    """
    Запуск формирования пакета КИЗ для конкретного магазина.
    Может вызываться автоматически планировщиком или вручную через API.
    """
    async def _async_exec():
        async with AsyncSessionLocal() as db:
            seller = await db.get(Seller, seller_id)
            if not seller or not seller.is_active:
                return {"success": False, "error": f"Seller {seller_id} not active or not found"}

            result = await process_auto_kiz_queue_for_seller(
                seller=seller,
                db=db,
                trigger_source=trigger_source,
            )
            return result

    try:
        return asyncio.run(_async_exec())
    except Exception as exc:
        logger.error(f"[Auto KIZ Task] Error running auto KIZ queue for {seller_id}: {exc}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60)
        return {"success": False, "error": str(exc)}
