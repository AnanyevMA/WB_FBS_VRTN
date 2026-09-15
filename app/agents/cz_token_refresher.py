"""
Chestny Znak Token Refresher Agent — WB FBS Manager

Refreshes GIS MT (Chestny Znak / True API) authentication tokens for all active sellers
before expiration using UKEP signature authentication and updates DB records & audit trail.
"""
import asyncio
from datetime import datetime, timezone
import logging
from typing import Dict, Any, List

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.models.seller import Seller
from app.models.audit import AuditLog
from app.services.encryption import encrypt, decrypt
from app.services.cz_client import CZClient, CZAPIError, CZUnauthorizedError
from app.services.kiz_service import batch_verify_and_sync_cises

logger = logging.getLogger(__name__)
sync_engine = create_engine(settings.database_url_sync)


@celery_app.task(
    name="app.agents.cz_token_refresher.refresh_all_tokens",
    queue="cz_operations",
    bind=True,
    max_retries=3,
    default_retry_delay=120,
)
def refresh_all_tokens(self) -> Dict[str, Any]:
    """Refresh CZ tokens for all active sellers before expiration."""
    logger.info("[CZ Token Refresher] Starting token refresh cycle for all sellers")
    refreshed_count = 0
    failed_count = 0
    errors: List[str] = []

    with Session(sync_engine) as db:
        sellers = db.execute(
            select(Seller).where(Seller.is_active == True)
        ).scalars().all()

        for seller in sellers:
            if not seller.cz_inn:
                continue

            thumbprint = seller.cryptopro_cert_thumbprint or seller.cz_cert_path
            from app.services.crypto_service import is_cryptopro_available
            if not is_cryptopro_available() or not thumbprint:
                logger.debug(
                    f"[CZ Token Refresher] Server-side CryptoPro or certificate not configured for seller {seller.id}. "
                    f"Skipping background token refresh (token is managed via UI or browser UKEP)."
                )
                continue

            try:
                new_token = asyncio.run(
                    _refresh_seller_cz_token(
                        inn=seller.cz_inn,
                        cert_thumbprint=thumbprint,
                    )
                )

                if new_token:
                    seller.cz_token_encrypted = encrypt(new_token)
                    seller.updated_at = datetime.now(timezone.utc)

                    _log_audit(
                        db,
                        seller_id=str(seller.id),
                        agent="cz_token_refresher",
                        action="TOKEN_REFRESH_SUCCESS",
                        entity_type="seller",
                        entity_id=str(seller.id),
                        payload={"inn": seller.cz_inn},
                    )
                    db.commit()
                    refreshed_count += 1
                    logger.info(f"[CZ Token Refresher] Successfully refreshed token for seller {seller.id}")
            except Exception as exc:
                err_msg = str(exc)
                logger.error(f"[CZ Token Refresher] Failed to refresh token for seller {seller.id}: {err_msg}")
                _log_audit(
                    db,
                    seller_id=str(seller.id),
                    agent="cz_token_refresher",
                    action="TOKEN_REFRESH_FAILED",
                    entity_type="seller",
                    entity_id=str(seller.id),
                    error=err_msg,
                )
                db.commit()
                failed_count += 1
                errors.append(f"Seller {seller.id}: {err_msg}")

                # Notify admin via Telegram
                if seller.telegram_bot_token_encrypted and seller.telegram_chat_ids:
                    from app.agents.notifier import send_alert
                    send_alert.delay(
                        seller_id=str(seller.id),
                        agent="cz_token_refresher",
                        message=f"Failed to refresh CZ auth token: {err_msg}",
                    )

    return {
        "status": "success" if failed_count == 0 else "partial_failure",
        "refreshed": refreshed_count,
        "failed": failed_count,
        "errors": errors,
    }


async def _refresh_seller_cz_token(inn: str, cert_thumbprint: str = None) -> str:
    """Execute auth flow with GIS MT True API to receive fresh token."""
    async with CZClient(inn=inn, cert_thumbprint=cert_thumbprint) as client:
        return await client.authenticate()


def _log_audit(
    db: Session,
    seller_id: str,
    agent: str,
    action: str,
    entity_type: str,
    entity_id: str,
    payload: Dict[str, Any] = None,
    error: str = None,
):
    """Write structured audit log entry."""
    import uuid
    log = AuditLog(
        seller_id=seller_id,
        agent=agent,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=payload,
        error=error,
        trace_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc),
    )
    db.add(log)


async def sync_active_orders_cz_status_async() -> Dict[str, Any]:
    """
    Асинхронная логика фоновой синхронизации статусов КИЗ Честного Знака для активных заказов.
    Запрашивает актуальный статус в True API без необходимости ручного нажатия «Сверить ЧЗ».
    """
    from app.database import AsyncSessionLocal
    from app.models.order import Order, OrderStatus, KizStatus

    results_summary: Dict[str, Any] = {
        "status": "success",
        "sellers_checked": 0,
        "orders_checked": 0,
        "kiz_synced": 0,
        "expired_tokens": 0,
        "errors": [],
    }

    async with AsyncSessionLocal() as db:
        sellers = (
            await db.execute(
                select(Seller).where(
                    Seller.is_active == True,
                    Seller.cz_inn.isnot(None),
                    Seller.cz_token_encrypted.isnot(None),
                )
            )
        ).scalars().all()

        for seller in sellers:
            if not seller.cz_inn or not seller.cz_token_encrypted:
                continue

            order_stmt = (
                select(Order.kiz_code)
                .where(
                    Order.seller_id == str(seller.id),
                    Order.kiz_code.isnot(None),
                    Order.kiz_code != "",
                    Order.status.in_([OrderStatus.NEW, OrderStatus.ASSEMBLING, OrderStatus.DELIVERING]),
                    Order.kiz_status != KizStatus.WITHDRAWN,
                )
                .distinct()
            )
            kiz_rows = (await db.execute(order_stmt)).scalars().all()
            all_codes = list(set([str(k).strip() for k in kiz_rows if k and str(k).strip()]))

            if not all_codes:
                continue

            results_summary["sellers_checked"] += 1
            results_summary["orders_checked"] += len(all_codes)

            try:
                synced_map = await batch_verify_and_sync_cises(
                    seller=seller,
                    kiz_codes=all_codes,
                    db=db,
                    force_refresh=True,
                )
                await db.commit()
                count = len(synced_map) if synced_map else 0
                results_summary["kiz_synced"] += count
                logger.info(
                    f"[CZ Background Sync] Successfully synced {count} KIZ codes "
                    f"for seller {seller.name or seller.id} ({seller.cz_inn})"
                )
            except CZUnauthorizedError:
                results_summary["expired_tokens"] += 1
                logger.warning(
                    f"[CZ Background Sync] Session token expired (401) for seller {seller.name or seller.id}. "
                    f"Awaiting browser keep-alive refresh or manual UKEP signin."
                )
            except Exception as exc:
                err_text = f"Seller {seller.id}: {exc}"
                results_summary["errors"].append(err_text)
                logger.error(f"[CZ Background Sync] Error syncing KIZ: {err_text}")

    return results_summary


@celery_app.task(
    name="app.agents.cz_token_refresher.sync_active_orders_cz_status",
    queue="cz_operations",
    bind=True,
    max_retries=1,
)
def sync_active_orders_cz_status(self=None) -> Dict[str, Any]:
    """
    Периодическая задача Celery Beat (каждые 30 минут):
    Автоматическая фоновая синхронизация статусов КИЗ Честного Знака для активных заказов.
    """
    try:
        return asyncio.run(sync_active_orders_cz_status_async())
    except Exception as exc:
        logger.error(f"[CZ Background Sync] Fatal error in sync_active_orders_cz_status: {exc}")
        return {"status": "error", "error": str(exc)}

