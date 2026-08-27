"""
Archive Processor Agent — пакетная обработка архива WB для вывода КИЗ из оборота
Запускается ежедневно в 03:00 по расписанию
"""
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    import pandas as pd
except (ImportError, ModuleNotFoundError):
    pd = None
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.models.seller import Seller
from app.models.order import Order, KizStatus
from app.services.encryption import decrypt

logger = logging.getLogger(__name__)
sync_engine = create_engine(settings.database_url_sync)


@celery_app.task(
    name="app.agents.archive_processor.process_all_archives",
    queue="archive",
    bind=True,
    max_retries=1,
    soft_time_limit=1800,  # 30 min
)
def process_all_archives(self):
    """
    Ежедневный запуск: скачивает и обрабатывает архив WB для каждого активного продавца.
    Запускает вывод КИЗ из оборота по факту продажи.
    """
    with Session(sync_engine) as db:
        sellers = db.execute(
            select(Seller).where(Seller.is_active == True)
        ).scalars().all()

    logger.info(f"[Archive] Processing archives for {len(sellers)} sellers")

    for seller in sellers:
        if not seller.cz_token_encrypted:
            logger.debug(f"Seller {seller.id} has no CZ token, skipping archive")
            continue
        process_seller_archive.delay(str(seller.id))


@celery_app.task(
    name="app.agents.archive_processor.process_seller_archive_download",
    queue="archive",
    bind=True,
    max_retries=2,
)
def process_seller_archive(self, seller_id: str):
    """
    Обрабатывает архив WB для одного продавца.

    Логика:
    1. Найти заказы со статусом DELIVERED и KIZ статусом ATTACHED (ещё не выведены)
    2. Для каждого — запустить CZ withdrawal
    3. Найти возвращённые заказы — запустить CZ return
    """
    import asyncio

    with Session(sync_engine) as db:
        seller = db.execute(
            select(Seller).where(Seller.id == seller_id)
        ).scalar_one_or_none()

        if not seller:
            logger.error(f"Seller {seller_id} not found")
            return

        # Find orders pending CZ withdrawal: ONLY when confirmed sold / delivered
        from sqlalchemy import or_, and_
        from app.models.order import OrderStatus
        pending_withdrawal = db.execute(
            select(Order).where(
                Order.seller_id == seller_id,
                or_(
                    Order.wb_status == "sold",
                    and_(Order.status == OrderStatus.DELIVERED, Order.wb_status.is_(None))
                ),
                Order.kiz_code.isnot(None),
                Order.kiz_status == KizStatus.ATTACHED,
            )
        ).scalars().all()

        # Find returned orders pending CZ return: CANCELLED + WITHDRAWN
        pending_return = db.execute(
            select(Order).where(
                Order.seller_id == seller_id,
                Order.status == OrderStatus.CANCELLED,
                Order.kiz_code.isnot(None),
                Order.kiz_status == KizStatus.WITHDRAWN,
            )
        ).scalars().all()

    logger.info(
        f"[Archive] Seller {seller_id}: "
        f"{len(pending_withdrawal)} pending withdrawals, "
        f"{len(pending_return)} pending returns"
    )

    # Queue withdrawal tasks
    from app.agents.cz_withdrawal import withdraw_order_kiz
    for order in pending_withdrawal:
        price_kopecks = int((order.price or 0) * 100)
        withdraw_order_kiz.apply_async(
            kwargs={
                "seller_id": seller_id,
                "order_id": order.id,
                "kiz_code": order.kiz_code,
                "price_kopecks": price_kopecks,
            },
            queue="cz_operations",
            countdown=5,  # stagger requests
        )

    # Queue return tasks
    from app.agents.cz_return import return_order_kiz
    for order in pending_return:
        return_order_kiz.apply_async(
            kwargs={
                "seller_id": seller_id,
                "order_id": order.id,
                "kiz_code": order.kiz_code,
            },
            queue="cz_operations",
            countdown=5,
        )

    logger.info(
        f"[Archive] Queued {len(pending_withdrawal)} withdrawals "
        f"and {len(pending_return)} returns for seller {seller_id}"
    )


@celery_app.task(
    name="app.agents.archive_processor.sync_order_statuses",
    queue="orders",
)
def sync_order_statuses(seller_id: str):
    """
    Синхронизация статусов заказов с WB API через POST /api/v3/orders/status.
    Обновляет статусы доставленных и отменённых заказов.
    """
    import asyncio
    from app.models.order import OrderStatus

    with Session(sync_engine) as db:
        seller = db.execute(
            select(Seller).where(Seller.id == seller_id)
        ).scalar_one_or_none()
        if not seller or not seller.wb_api_token_encrypted:
            return

        # Get orders in active states
        active_orders = db.execute(
            select(Order).where(
                Order.seller_id == seller_id,
                Order.status.in_([OrderStatus.NEW, OrderStatus.ASSEMBLING, OrderStatus.DELIVERING]),
            )
        ).scalars().all()

        if not active_orders:
            return

        order_ids = [o.id for o in active_orders]
        wb_token = decrypt(seller.wb_api_token_encrypted)

    async def _sync():
        from app.services.wb_client import WBClient
        async with WBClient(wb_token) as client:
            st_list = await client.get_orders_status(order_ids)
            return {st["id"]: st for st in st_list if "id" in st}

    status_data = asyncio.run(_sync())

    with Session(sync_engine) as db:
        updated = 0
        for order_id, st in status_data.items():
            wb_status = st.get("wbStatus")
            supp_status = st.get("supplierStatus")

            order = db.execute(
                select(Order).where(Order.id == order_id)
            ).scalar_one_or_none()
            if not order:
                continue

            order_updated = False
            if wb_status and order.wb_status != wb_status:
                order.wb_status = wb_status
                order_updated = True
            if supp_status and order.supplier_status != supp_status:
                order.supplier_status = supp_status
                order_updated = True

            if wb_status == "sold" and order.status != OrderStatus.DELIVERED:
                order.status = OrderStatus.DELIVERED
                order_updated = True
            elif wb_status in ["canceled", "canceled_by_client", "declined_by_client", "defect"] and order.status != OrderStatus.CANCELLED:
                order.status = OrderStatus.CANCELLED
                order_updated = True
            elif wb_status in ["sorted", "ready_for_pickup", "waiting"] and order.status != OrderStatus.DELIVERING:
                order.status = OrderStatus.DELIVERING
                order_updated = True

            if order_updated:
                order.updated_at = datetime.now(timezone.utc)
                updated += 1

        db.commit()
        logger.info(f"[Sync] Updated {updated} order statuses for seller {seller_id}")


@celery_app.task(
    name="app.agents.archive_processor.check_archive_reminders",
    queue="notifications",
    bind=True,
    max_retries=1,
)
def check_archive_reminders(self):
    """
    Проверяет, нужно ли отправить менеджеру напоминание о загрузке архива WB (раз в N дней, по умолчанию 2).
    Запускается по расписанию Celery Beat.
    """
    import asyncio
    from app.services.telegram_service import TelegramService

    now = datetime.now(timezone.utc)
    with Session(sync_engine) as db:
        sellers = db.execute(
            select(Seller).where(
                Seller.is_active == True,
                Seller.archive_reminder_enabled == True,
                Seller.telegram_bot_token_encrypted.isnot(None),
            )
        ).scalars().all()

        reminders_sent = 0
        for seller in sellers:
            if not seller.telegram_chat_ids:
                continue

            interval_days = seller.archive_reminder_days or 2
            last_upload = seller.last_archive_uploaded_at
            last_sent = seller.last_archive_reminder_sent_at

            if last_upload and last_upload.tzinfo is None:
                last_upload = last_upload.replace(tzinfo=timezone.utc)
            if last_sent and last_sent.tzinfo is None:
                last_sent = last_sent.replace(tzinfo=timezone.utc)

            # Don't send more than once in 20 hours to prevent duplicate spam
            if last_sent and (now - last_sent).total_seconds() < 20 * 3600:
                continue

            should_send = False
            days_since = None
            if last_upload is None:
                # Never uploaded
                should_send = True
            else:
                diff = now - last_upload
                days_since = int(diff.total_seconds() // 86400)
                if diff.total_seconds() >= interval_days * 86400:
                    should_send = True

            if should_send:
                bot_token = decrypt(seller.telegram_bot_token_encrypted)
                tg = TelegramService(bot_token)
                try:
                    asyncio.run(
                        tg.send_archive_reminder(
                            chat_ids=seller.telegram_chat_ids,
                            seller_name=seller.name,
                            days_since_last=days_since,
                        )
                    )
                    seller.last_archive_reminder_sent_at = now
                    reminders_sent += 1
                    logger.info(f"[Archive Reminder] Sent reminder for seller {seller.name} (ID: {seller.id})")
                except Exception as e:
                    logger.error(f"[Archive Reminder] Error sending reminder for {seller.id}: {e}")

        if reminders_sent > 0:
            db.commit()
            logger.info(f"[Archive Reminder] Total reminders sent: {reminders_sent}")
