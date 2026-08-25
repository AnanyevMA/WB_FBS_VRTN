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
from app.services.cz_client import CZClient, CZAPIError

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
