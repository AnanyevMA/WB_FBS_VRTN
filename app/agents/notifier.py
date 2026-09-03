"""
Notifier Agent — отправка уведомлений через Telegram

Delegated agent responsible for dispatching real-time alerts, new order notifications,
supply delivery updates, and CZ operation status updates to managers via Telegram.
"""
from datetime import datetime, timezone
import logging
from typing import Optional, Dict, Any

from sqlalchemy import create_engine, select, and_, update
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.models.seller import Seller
from app.models.audit import AuditLog
from app.services.encryption import decrypt

logger = logging.getLogger(__name__)
sync_engine = create_engine(settings.database_url_sync)


def _get_seller(db: Session, seller_id: str):
    return db.execute(select(Seller).where(Seller.id == seller_id)).scalar_one_or_none()


def _log_audit(
    db: Session,
    seller_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    payload: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
):
    import uuid
    log = AuditLog(
        seller_id=seller_id,
        agent="notifier",
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=payload,
        error=error,
        trace_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc),
    )
    db.add(log)


@celery_app.task(name="app.agents.notifier.notify_new_order", queue="notifications")
def notify_new_order(seller_id: str, order_id: int, order_data: Optional[dict] = None):
    """Send Telegram notification about new order."""
    import asyncio
    with Session(sync_engine) as db:
        seller = _get_seller(db, seller_id)
        if not seller or not seller.telegram_bot_token_encrypted:
            return
        bot_token = decrypt(seller.telegram_bot_token_encrypted)
        chat_ids = seller.telegram_chat_ids or []
        if not chat_ids:
            return

        # Если order_data не передан — подгрузить из БД
        if not order_data:
            from app.models.order import Order
            order = db.execute(
                select(Order).where(Order.id == order_id, Order.seller_id == seller_id)
            ).scalar_one_or_none()
            if order:
                order_data = {
                    "id": order.id,
                    "name": order.name or order.subject or "—",
                    "article": order.article or "—",
                    "subject": order.subject or "—",
                    "brand": order.brand or "—",
                    "price": int((order.price or 0) * 100),  # в копейках для совместимости
                    "kiz_required": order.kiz_required or False,
                }
            else:
                order_data = {}

    from app.services.telegram_service import TelegramService
    async def _send():
        svc = TelegramService(bot_token)
        try:
            await svc.send_new_order_notification(chat_ids, order_id, order_data)
            with Session(sync_engine) as db:
                from app.models.order import Order
                ord_obj = db.execute(select(Order).where(Order.id == order_id)).scalar_one_or_none()
                if ord_obj:
                    ord_obj.notified_at = datetime.now(timezone.utc)
                _log_audit(db, seller_id, "NOTIFY_NEW_ORDER_SUCCESS", "order", str(order_id))
                db.commit()
        except Exception as exc:
            with Session(sync_engine) as db:
                _log_audit(db, seller_id, "NOTIFY_NEW_ORDER_FAILED", "order", str(order_id), error=str(exc))
                db.commit()
            raise exc
        finally:
            await svc.close()

    asyncio.run(_send())


@celery_app.task(name="app.agents.notifier.notify_batch_orders", queue="notifications")
def notify_batch_orders(seller_id: str, orders_payload: list):
    """
    Пакетное уведомление о N новых заказах (для N ≥ 2 за один цикл опроса).

    orders_payload: список словарей с ключами id, name, article, price, kiz_required.
    """
    import asyncio
    with Session(sync_engine) as db:
        seller = _get_seller(db, seller_id)
        if not seller or not seller.telegram_bot_token_encrypted:
            return
        bot_token = decrypt(seller.telegram_bot_token_encrypted)
        chat_ids = seller.telegram_chat_ids or []
        if not chat_ids:
            return

    from app.services.telegram_service import TelegramService

    async def _send():
        svc = TelegramService(bot_token)
        try:
            await svc.send_batch_orders_notification(
                chat_ids=chat_ids,
                seller_id=seller_id,
                orders=orders_payload,
            )
            with Session(sync_engine) as db:
                from app.models.order import Order
                order_ids = [o.get("id") for o in orders_payload if o.get("id")]
                if order_ids:
                    db.execute(
                        update(Order)
                        .where(Order.id.in_(order_ids))
                        .values(notified_at=datetime.now(timezone.utc))
                    )
                _log_audit(
                    db, seller_id, "NOTIFY_BATCH_ORDERS_SUCCESS",
                    "order_batch", f"count:{len(orders_payload)}",
                    payload={"order_ids": [o.get("id") for o in orders_payload]},
                )
                db.commit()
        except Exception as exc:
            with Session(sync_engine) as db:
                _log_audit(
                    db, seller_id, "NOTIFY_BATCH_ORDERS_FAILED",
                    "order_batch", f"count:{len(orders_payload)}",
                    error=str(exc),
                )
                db.commit()
            raise exc
        finally:
            await svc.close()

    asyncio.run(_send())


@celery_app.task(name="app.agents.notifier.send_cz_status_notification", queue="notifications")
def send_cz_status_notification(
    seller_id: str, order_id: int, success: bool,
    doc_id: str = None, error: str = None,
):
    """Send Telegram notification about CZ operation status."""
    import asyncio
    with Session(sync_engine) as db:
        seller = _get_seller(db, seller_id)
        if not seller or not seller.telegram_bot_token_encrypted:
            return
        bot_token = decrypt(seller.telegram_bot_token_encrypted)
        chat_ids = seller.telegram_chat_ids or []

    from app.services.telegram_service import TelegramService
    async def _send():
        svc = TelegramService(bot_token)
        try:
            await svc.send_cz_withdrawal_status(chat_ids, order_id, success, doc_id, error)
            with Session(sync_engine) as db:
                _log_audit(
                    db, seller_id, "NOTIFY_CZ_STATUS_SUCCESS", "order", str(order_id),
                    payload={"success": success, "doc_id": doc_id}
                )
                db.commit()
        except Exception as exc:
            with Session(sync_engine) as db:
                _log_audit(db, seller_id, "NOTIFY_CZ_STATUS_FAILED", "order", str(order_id), error=str(exc))
                db.commit()
            raise exc
        finally:
            await svc.close()

    asyncio.run(_send())


@celery_app.task(name="app.agents.notifier.send_supply_notification", queue="notifications")
def send_supply_notification(seller_id: str, supply_id: str, orders_count: int):
    """Send Telegram notification about supply delivery."""
    import asyncio
    with Session(sync_engine) as db:
        seller = _get_seller(db, seller_id)
        if not seller or not seller.telegram_bot_token_encrypted:
            return
        bot_token = decrypt(seller.telegram_bot_token_encrypted)
        chat_ids = seller.telegram_chat_ids or []

    from app.services.telegram_service import TelegramService
    async def _send():
        svc = TelegramService(bot_token)
        try:
            await svc.send_supply_delivered(chat_ids, supply_id, orders_count)
            with Session(sync_engine) as db:
                _log_audit(
                    db, seller_id, "NOTIFY_SUPPLY_SUCCESS", "supply", str(supply_id),
                    payload={"orders_count": orders_count}
                )
                db.commit()
        except Exception as exc:
            with Session(sync_engine) as db:
                _log_audit(db, seller_id, "NOTIFY_SUPPLY_FAILED", "supply", str(supply_id), error=str(exc))
                db.commit()
            raise exc
        finally:
            await svc.close()

    asyncio.run(_send())


@celery_app.task(name="app.agents.notifier.send_alert", queue="notifications")
def send_alert(seller_id: str, agent: str, message: str):
    """Send error alert to seller's admin."""
    import asyncio
    with Session(sync_engine) as db:
        seller = _get_seller(db, seller_id)
        if not seller or not seller.telegram_bot_token_encrypted:
            return
        bot_token = decrypt(seller.telegram_bot_token_encrypted)
        chat_ids = seller.telegram_chat_ids or []

    from app.services.telegram_service import TelegramService
    async def _send():
        svc = TelegramService(bot_token)
        try:
            await svc.send_error_alert(chat_ids, agent, message)
            with Session(sync_engine) as db:
                _log_audit(db, seller_id, "SEND_ALERT_SUCCESS", "agent", agent, payload={"message": message})
                db.commit()
        except Exception as exc:
            with Session(sync_engine) as db:
                _log_audit(db, seller_id, "SEND_ALERT_FAILED", "agent", agent, error=str(exc))
                db.commit()
            raise exc
        finally:
            await svc.close()

    asyncio.run(_send())


# In-memory tracking for scheduled digests: (seller_id, YYYY-MM-DD, HH:MM)
_scheduled_digest_sent: set[tuple[str, str, str]] = set()


def is_scheduled_slot_due(
    schedule: list[str],
    local_now: datetime,
    grace_minutes: int = 15,
) -> Optional[str]:
    """
    Check if local_now falls within [slot_time, slot_time + grace_minutes] for any slot in schedule.
    Returns matching slot string 'HH:MM' or None.
    """
    if not schedule:
        return None

    for slot in schedule:
        try:
            parts = str(slot).strip().split(":")
            if len(parts) != 2:
                continue
            sh, sm = int(parts[0]), int(parts[1])
            target_dt = local_now.replace(hour=sh, minute=sm, second=0, microsecond=0)
            diff_sec = (local_now - target_dt).total_seconds()
            if 0 <= diff_sec < (grace_minutes * 60):
                return f"{sh:02d}:{sm:02d}"
        except (ValueError, TypeError):
            continue
    return None


@celery_app.task(
    name="app.agents.notifier.send_scheduled_orders_digest",
    queue="notifications",
    bind=True,
    max_retries=3,
)
def send_scheduled_orders_digest(self=None, now_utc_override: Optional[datetime] = None) -> dict:
    """
    Periodic task checking active sellers with notification_mode='scheduled'.
    Evaluates current time in seller's timezone against seller.notification_schedule.
    Dispatches summary of all unnotified orders and stamps order.notified_at.
    """
    import asyncio
    import zoneinfo
    from app.models.order import Order, OrderStatus
    from app.services.telegram_service import TelegramService

    now_utc = now_utc_override or datetime.now(timezone.utc)
    results = {"processed_sellers": 0, "sent_digests": 0, "orders_notified": 0}

    with Session(sync_engine) as db:
        try:
            sellers = db.execute(
                select(Seller).where(
                    and_(
                        Seller.is_active.is_(True),
                        Seller.polling_enabled.is_(True),
                        Seller.notification_mode == "scheduled",
                    )
                )
            ).scalars().all()
        except Exception as exc:
            logger.error(f"[ScheduledDigest] Failed to query sellers: {exc}")
            if self:
                raise self.retry(exc=exc, countdown=60)
            return results

        for seller in sellers:
            seller_id = str(seller.id)
            results["processed_sellers"] += 1

            if not seller.telegram_bot_token_encrypted or not seller.telegram_chat_ids:
                logger.debug(f"[ScheduledDigest] Seller {seller_id} lacks TG token or chat IDs")
                continue

            # Determine seller local time
            tz_name = getattr(seller, "timezone", None) or getattr(seller, "digest_timezone", None) or "Europe/Moscow"
            try:
                seller_tz = zoneinfo.ZoneInfo(tz_name)
            except Exception:
                seller_tz = zoneinfo.ZoneInfo("Europe/Moscow")

            local_now = now_utc.astimezone(seller_tz)
            date_str = local_now.strftime("%Y-%m-%d")

            schedule = getattr(seller, "notification_schedule", None) or ["10:00", "14:00", "18:00"]
            matched_slot = is_scheduled_slot_due(schedule, local_now)
            if not matched_slot:
                continue

            dedup_key = (seller_id, date_str, matched_slot)
            if dedup_key in _scheduled_digest_sent:
                continue

            # Check persistent audit log
            already_recorded = db.execute(
                select(AuditLog.id).where(
                    and_(
                        AuditLog.seller_id == seller_id,
                        AuditLog.action.in_(["SCHEDULED_DIGEST_SUCCESS", "SCHEDULED_DIGEST_SKIPPED_EMPTY"]),
                        AuditLog.entity_id == f"{date_str}:{matched_slot}",
                    )
                ).limit(1)
            ).scalar_one_or_none()
            if already_recorded:
                _scheduled_digest_sent.add(dedup_key)
                continue

            # Query unnotified orders
            unnotified_orders = db.execute(
                select(Order).where(
                    and_(
                        Order.seller_id == seller.id,
                        Order.notified_at.is_(None),
                        Order.status != OrderStatus.CANCELLED,
                    )
                ).order_by(Order.created_at.asc())
            ).scalars().all()

            if not unnotified_orders:
                _scheduled_digest_sent.add(dedup_key)
                _log_audit(
                    db, seller_id, "SCHEDULED_DIGEST_SKIPPED_EMPTY",
                    "scheduled_digest", f"{date_str}:{matched_slot}",
                    payload={"count": 0, "slot": matched_slot}
                )
                db.commit()
                continue

            # Format orders payload for Telegram batch notification
            orders_payload = []
            for ord_item in unnotified_orders:
                price_val = float(ord_item.price or 0.0)
                orders_payload.append({
                    "id": ord_item.id,
                    "name": ord_item.name or ord_item.subject or "—",
                    "article": ord_item.article or "",
                    "brand": ord_item.brand or "—",
                    "subject": ord_item.subject or "—",
                    "price": price_val,
                    "kiz_required": bool(ord_item.kiz_required),
                    "wb_created_at": ord_item.wb_created_at.isoformat() if ord_item.wb_created_at else "",
                })

            bot_token = decrypt(seller.telegram_bot_token_encrypted)
            chat_ids = seller.telegram_chat_ids or []

            async def _send_digest():
                svc = TelegramService(bot_token)
                try:
                    await svc.send_batch_orders_notification(
                        chat_ids=chat_ids,
                        seller_id=seller_id,
                        orders=orders_payload,
                    )
                finally:
                    await svc.close()

            try:
                asyncio.run(_send_digest())
                stamp = datetime.now(timezone.utc)
                for ord_item in unnotified_orders:
                    ord_item.notified_at = stamp

                _log_audit(
                    db, seller_id, "SCHEDULED_DIGEST_SUCCESS",
                    "scheduled_digest", f"{date_str}:{matched_slot}",
                    payload={
                        "count": len(unnotified_orders),
                        "slot": matched_slot,
                        "order_ids": [o.id for o in unnotified_orders],
                    }
                )
                db.commit()
                _scheduled_digest_sent.add(dedup_key)
                results["sent_digests"] += 1
                results["orders_notified"] += len(unnotified_orders)
                logger.info(
                    f"[ScheduledDigest] Sent digest for seller {seller_id}: "
                    f"{len(unnotified_orders)} orders in slot {matched_slot}"
                )
            except Exception as exc:
                db.rollback()
                _log_audit(
                    db, seller_id, "SCHEDULED_DIGEST_FAILED",
                    "scheduled_digest", f"{date_str}:{matched_slot}",
                    error=str(exc)
                )
                db.commit()
                logger.error(f"[ScheduledDigest] Failed sending digest for seller {seller_id}: {exc}")
                if self:
                    raise self.retry(exc=exc, countdown=30)

    return results
