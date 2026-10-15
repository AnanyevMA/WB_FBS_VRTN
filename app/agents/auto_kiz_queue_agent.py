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

# In-memory трекер для защиты от повторного выполнения в рамках одного интервала:
# seller_id -> datetime последнего запуска в UTC
_auto_queue_last_runs: Dict[str, datetime] = {}


def get_seller_most_recent_slot(
    seller_local_now: datetime,
    target_hour: int,
    target_minute: int,
    interval_hours: int,
    seller_tz: Any,
) -> datetime:
    """
    Вычисляет наиболее недавний контрольный слот времени (на сегодня или вчера),
    опираясь на опорный час (target_hour), минуту (target_minute) и интервал (interval_hours).
    """
    interval = max(1, min(24, interval_hours))
    base_hour = target_hour % interval
    candidates = []

    for day_offset in (-1, 0):
        d = (seller_local_now + timedelta(days=day_offset)).date()
        for k in range(24 // interval + 1):
            slot_h = base_hour + k * interval
            if slot_h < 24:
                slot_dt = datetime(d.year, d.month, d.day, slot_h, target_minute, tzinfo=seller_tz)
                if slot_dt <= seller_local_now:
                    candidates.append(slot_dt)

    if not candidates:
        return seller_local_now.replace(minute=target_minute, second=0, microsecond=0)

    return max(candidates)


def is_seller_auto_queue_due(
    seller: Seller,
    now_utc: datetime,
    grace_hours: Optional[float] = None,
) -> bool:
    """
    Проверяет, наступило ли локальное время для формирования очереди КИЗ с заданным интервалом (по умолчанию каждые 6 часов).
    Учитывает часовой пояс продавца, опорный час/минуту и время последнего запуска.
    """
    if not getattr(seller, "auto_kiz_queue_enabled", True):
        return False

    interval_hours = int(getattr(seller, "auto_kiz_queue_interval_hours", 6) or 6)
    target_hour = int(getattr(seller, "auto_kiz_queue_hour", 17) or 17)
    target_minute = int(getattr(seller, "auto_kiz_queue_minute", 0) or 0)

    if grace_hours is None:
        grace_hours = min(3.0, max(1.0, interval_hours * 0.5))

    seller_tz = resolve_timezone(getattr(seller, "timezone", "Europe/Moscow"))
    seller_local_now = now_utc.astimezone(seller_tz)

    most_recent_slot = get_seller_most_recent_slot(
        seller_local_now=seller_local_now,
        target_hour=target_hour,
        target_minute=target_minute,
        interval_hours=interval_hours,
        seller_tz=seller_tz,
    )

    # 1. Если с момента контрольного слота прошло больше времени, чем grace_hours — слот пропущен
    if (seller_local_now - most_recent_slot) > timedelta(hours=grace_hours):
        return False

    # 2. Проверка in-memory кэша последнего запуска
    mem_last_run = _auto_queue_last_runs.get(str(seller.id))
    if mem_last_run:
        if mem_last_run.tzinfo is None:
            mem_last_run = mem_last_run.replace(tzinfo=timezone.utc)
        if mem_last_run.astimezone(seller_tz) >= most_recent_slot:
            return False

    # 3. Проверка даты последнего запуска в БД
    last_run_at = getattr(seller, "last_auto_kiz_queue_at", None)
    if last_run_at:
        if last_run_at.tzinfo is None:
            last_run_at = last_run_at.replace(tzinfo=timezone.utc)
        if last_run_at.astimezone(seller_tz) >= most_recent_slot:
            _auto_queue_last_runs[str(seller.id)] = last_run_at
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
    Периодическая задача Celery Beat (запускается каждые 5 минут).
    Проверяет всех активных продавцов и запускает формирование очереди КИЗ
    по настроенному интервалу (по умолчанию каждые 6 часов).
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
                # Отмечаем в памяти, чтобы не спамить до завершения фонового таска
                _auto_queue_last_runs[str(seller.id)] = now_utc

    logger.info(f"[Auto KIZ Beat] Found {len(due_seller_ids)} sellers due for auto KIZ queue (6h interval)")

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
