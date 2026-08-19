"""
Morning Digest Agent — WB FBS Manager

Runs on a frequent schedule (every 30 min) and checks each seller's configured
digest time in their local timezone. Sends a morning summary of all pending
(NEW / ASSEMBLING, no supply yet) orders with a one-tap "Create Supply" button.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import create_engine, select, and_
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings

logger = logging.getLogger(__name__)
sync_engine = create_engine(settings.database_url_sync)


def _seller_digest_due(seller, now_utc: datetime) -> bool:
    """
    Return True if the seller's digest should fire right now.

    Logic:
    - Convert now_utc to seller's local timezone.
    - Check if (local_hour, local_minute // 30) matches (digest_hour, digest_minute // 30).
      Using 30-min buckets so the task (running every 30 min) doesn't miss the window.
    - Use a 'digest_sent_today' guard stored in-memory to avoid double-sends.
    """
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(seller.digest_timezone or "Europe/Moscow")
    except Exception:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/Moscow")

    local_now = now_utc.astimezone(tz)
    target_hour = getattr(seller, "digest_hour", 8)
    target_minute = getattr(seller, "digest_minute", 0)

    # Match within the same 30-min bucket
    same_hour = local_now.hour == target_hour
    same_bucket = (local_now.minute // 30) == (target_minute // 30)
    return same_hour and same_bucket


# Guard: tracks (seller_id, date) pairs where digest was already sent today
_digest_sent: dict[str, str] = {}  # seller_id → "YYYY-MM-DD" (local date)


@celery_app.task(
    name="app.agents.morning_digest.send_morning_digest",
    queue="notifications",
    bind=True,
    max_retries=3,
)
def send_morning_digest(self) -> dict:
    """
    Checks all active sellers and sends a morning digest to those whose
    configured time (in their timezone) matches the current time window.
    Runs every 30 minutes via Celery Beat.
    """
    import asyncio

    now_utc = datetime.now(timezone.utc)
    sent_count = 0

    with Session(sync_engine) as db:
        try:
            from app.models.seller import Seller
            sellers = db.execute(
                select(Seller).where(
                    and_(Seller.is_active.is_(True), Seller.digest_enabled.is_(True))
                )
            ).scalars().all()
        except Exception as exc:
            logger.error(f"[MorningDigest] Failed to query sellers: {exc}")
            raise self.retry(exc=exc, countdown=60)

        for seller in sellers:
            seller_id = str(seller.id)

            # --- Skip if already sent today (in seller's local tz) ---
            try:
                import zoneinfo
                tz = zoneinfo.ZoneInfo(seller.digest_timezone or "Europe/Moscow")
            except Exception:
                import zoneinfo
                tz = zoneinfo.ZoneInfo("Europe/Moscow")
            local_today = now_utc.astimezone(tz).strftime("%Y-%m-%d")

            if _digest_sent.get(seller_id) == local_today:
                continue

            # --- Check if it's time ---
            if not _seller_digest_due(seller, now_utc):
                continue

            # --- Fetch pending orders ---
            try:
                from app.models.order import Order, OrderStatus
                pending_statuses = [OrderStatus.NEW, OrderStatus.ASSEMBLING]
                orders_result = db.execute(
                    select(Order).where(
                        and_(
                            Order.seller_id == seller.id,
                            Order.status.in_(pending_statuses),
                            Order.supply_id.is_(None),
                        )
                    ).order_by(Order.wb_created_at.asc())
                ).scalars().all()

                pending_payload = [
                    {
                        "id": o.id,
                        "name": o.name or o.article or f"Заказ #{o.id}",
                        "article": o.article or "",
                        "price": float(o.price) if o.price is not None else 0.0,
                        "kiz_required": o.kiz_required,
                        "wb_created_at": (
                            o.wb_created_at.isoformat()
                            if o.wb_created_at else ""
                        ),
                    }
                    for o in orders_result
                ]
            except Exception as exc:
                logger.error(f"[MorningDigest] Failed to fetch orders for seller {seller_id}: {exc}")
                continue

            # --- Send digest via Telegram ---
            if not seller.telegram_bot_token_encrypted:
                logger.debug(f"[MorningDigest] Seller {seller_id} has no Telegram token, skipping.")
                continue

            chat_ids = seller.telegram_chat_ids or []
            if not chat_ids:
                logger.debug(f"[MorningDigest] Seller {seller_id} has no Telegram chat_ids, skipping.")
                continue

            try:
                from app.services.encryption import decrypt
                bot_token = decrypt(seller.telegram_bot_token_encrypted)

                digest_time_str = (
                    f"{seller.digest_hour:02d}:{seller.digest_minute:02d} "
                    f"{seller.digest_timezone or 'Europe/Moscow'}"
                )

                from app.services.telegram_service import TelegramService

                async def _send():
                    svc = TelegramService(bot_token)
                    try:
                        await svc.send_morning_digest(
                            chat_ids=chat_ids,
                            seller_id=seller_id,
                            pending_orders=pending_payload,
                            digest_time_str=digest_time_str,
                        )
                    finally:
                        await svc.close()

                asyncio.run(_send())

                # Mark as sent for today
                _digest_sent[seller_id] = local_today
                sent_count += 1

                # Audit log
                _log_audit(
                    db, seller_id,
                    action="MORNING_DIGEST_SENT",
                    entity_type="seller",
                    entity_id=seller_id,
                    payload={
                        "pending_count": len(pending_payload),
                        "digest_time": digest_time_str,
                    },
                )
                db.commit()

            except Exception as exc:
                logger.error(f"[MorningDigest] Failed to send digest for seller {seller_id}: {exc}")
                _log_audit(
                    db, seller_id,
                    action="MORNING_DIGEST_FAILED",
                    entity_type="seller",
                    entity_id=seller_id,
                    error=str(exc),
                )
                db.commit()

    logger.info(f"[MorningDigest] Cycle complete. Sent digests to {sent_count} seller(s).")
    return {"sent": sent_count}


def _log_audit(db, seller_id, action, entity_type, entity_id,
               payload=None, error=None):
    import uuid
    from datetime import datetime, timezone
    from app.models.audit import AuditLog
    log = AuditLog(
        seller_id=seller_id,
        agent="morning_digest",
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=payload,
        error=error,
        trace_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc),
    )
    db.add(log)
