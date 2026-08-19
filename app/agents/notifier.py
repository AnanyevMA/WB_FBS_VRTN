"""
Notifier Agent — отправка уведомлений через Telegram

Delegated agent responsible for dispatching real-time alerts, new order notifications,
supply delivery updates, and CZ operation status updates to managers via Telegram.
"""
from datetime import datetime, timezone
import logging
from typing import Optional, Dict, Any

from sqlalchemy import create_engine, select
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

    from app.services.telegram_service import TelegramService
    async def _send():
        svc = TelegramService(bot_token)
        try:
            await svc.send_new_order_notification(chat_ids, order_id, order_data)
            with Session(sync_engine) as db:
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
