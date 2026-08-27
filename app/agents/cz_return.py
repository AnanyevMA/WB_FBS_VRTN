"""
CZ Return Agent — Возврат КИЗ в оборот при возврате товара
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import create_engine, select, and_
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.models.order import Order, KizStatus
from app.models.kiz import KizOperation, KizOperationType, KizProductInfo
from app.models.seller import Seller
from app.models.audit import AuditLog
from app.services.encryption import decrypt

logger = logging.getLogger(__name__)
sync_engine = create_engine(settings.database_url_sync)


@celery_app.task(
    name="app.agents.cz_return.return_order_kiz",
    queue="cz_operations",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def return_order_kiz(
    self,
    seller_id: str,
    order_id: int,
    kiz_code: str,
):
    """
    Возврат КИЗ в оборот при возврате товара покупателем.
    Требует: статус КИЗ = ВЫБЫЛ (причина: дистанционная продажа с чеком)
    """
    import asyncio
    from app.services.cz_client import CZClient

    logger.info(f"[CZ Return] Starting for order {order_id}, KIZ: {kiz_code[:20]}...")

    with Session(sync_engine) as db:
        seller = db.execute(select(Seller).where(Seller.id == seller_id)).scalar_one_or_none()
        if not seller or not seller.cz_token_encrypted:
            logger.error(f"Seller {seller_id} not found or no CZ token")
            return

        order = db.execute(select(Order).where(
            and_(Order.id == order_id, Order.seller_id == seller_id)
        )).scalar_one_or_none()

        if not order:
            logger.error(f"Order {order_id} not found")
            return

        if order.kiz_status == KizStatus.RETURNED:
            logger.info(f"Order {order_id} KIZ already returned, skipping")
            return

        kiz_op = KizOperation(
            seller_id=seller_id,
            order_id=order_id,
            kiz_code=kiz_code,
            operation=KizOperationType.RETURN,
            status="PENDING",
            retries=self.request.retries,
        )
        db.add(kiz_op)
        db.flush()

        cz_token = decrypt(seller.cz_token_encrypted)
        cz_inn = seller.cz_inn

        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    doc_id = executor.submit(
                        asyncio.run,
                        _do_return(
                            inn=cz_inn,
                            token=cz_token,
                            cert_thumbprint=seller.cz_cert_path,
                            kiz_codes=[kiz_code],
                            wb_order_id=order_id,
                        )
                    ).result()
            else:
                doc_id = asyncio.run(_do_return(
                    inn=cz_inn,
                    token=cz_token,
                    cert_thumbprint=seller.cz_cert_path,
                    kiz_codes=[kiz_code],
                    wb_order_id=order_id,
                ))

            order.kiz_status = KizStatus.RETURNED
            order.kiz_cz_status = "INTRODUCED"
            order.kiz_cz_status_updated_at = datetime.now(timezone.utc)
            order.cz_return_doc_id = doc_id
            order.updated_at = datetime.now(timezone.utc)

            # Update KIZ operation
            kiz_op.status = "SUCCESS"
            kiz_op.cz_doc_id = doc_id
            kiz_op.updated_at = datetime.now(timezone.utc)

            # Sync KizProductInfo (Single Source of Truth)
            if kiz_code:
                kiz_info_row = db.query(KizProductInfo).filter(
                    (KizProductInfo.kiz_code == kiz_code) | (KizProductInfo.clean_cis == kiz_code)
                ).first()
                if kiz_info_row:
                    kiz_info_row.cz_status = "INTRODUCED"
                    kiz_info_row.checked_at = datetime.now(timezone.utc)

            _log_audit(db, seller_id, "cz_return", "SUCCESS", "order", str(order_id),
                       payload={"doc_id": doc_id})
            db.commit()
            logger.info(f"[CZ Return] SUCCESS for order {order_id}, doc_id: {doc_id}")

        except Exception as exc:
            error_msg = str(exc)
            logger.error(f"[CZ Return] FAILED for order {order_id}: {error_msg}")
            kiz_op.status = "FAILED"
            kiz_op.error_message = error_msg
            kiz_op.updated_at = datetime.now(timezone.utc)
            order.kiz_status = KizStatus.ERROR
            _log_audit(db, seller_id, "cz_return", "FAILED", "order", str(order_id),
                       error=error_msg)
            db.commit()
            if self.request.retries < self.max_retries:
                raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


async def _do_return(
    inn: str, token: str, cert_thumbprint: Optional[str],
    kiz_codes: list[str], wb_order_id: int,
) -> str:
    from app.services.cz_client import CZClient
    async with CZClient(inn=inn, token=token, cert_thumbprint=cert_thumbprint) as client:
        return await client.return_to_circulation(
            kiz_codes=kiz_codes,
            wb_order_id=wb_order_id,
            wait_for_result=True,
        )


def _log_audit(db, seller_id, agent, action, entity_type, entity_id,
               payload=None, error=None):
    import uuid
    log = AuditLog(
        seller_id=seller_id, agent=agent, action=action,
        entity_type=entity_type, entity_id=entity_id,
        payload=payload, error=error, trace_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc),
    )
    db.add(log)
