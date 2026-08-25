"""
CZ Withdrawal Celery Agent — Вывод КИЗ из оборота
Запускается по завершении продажи через архив WB
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import create_engine, select, and_
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.models.order import Order, KizStatus
from app.models.kiz import KizOperation, KizOperationType
from app.models.seller import Seller
from app.models.audit import AuditLog
from app.services.encryption import decrypt
from app.agents.notifier import send_cz_status_notification

logger = logging.getLogger(__name__)

# Synchronous DB engine for Celery tasks
sync_engine = create_engine(settings.database_url_sync)


@celery_app.task(
    name="app.agents.cz_withdrawal.withdraw_order_kiz",
    queue="cz_operations",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def withdraw_order_kiz(
    self,
    seller_id: str,
    order_id: int,
    kiz_code: str,
    price_kopecks: int,
    receipt_number: Optional[str] = None,
    receipt_date: Optional[str] = None,
    wb_order_data: Optional[dict] = None,
):
    """
    Вывод КИЗ из оборота для конкретного заказа.
    Вызывается после подтверждения продажи (из архива WB).

    Args:
        seller_id: UUID продавца
        order_id: WB order ID
        kiz_code: SGTIN код для вывода
        price_kopecks: цена в копейках
        receipt_number: номер кассового чека WB
        receipt_date: дата чека (YYYY-MM-DD)
        wb_order_data: дополнительные данные заказа из WB
    """
    import asyncio
    from app.services.cz_client import CZClient, CZAPIError

    logger.info(f"[CZ Withdrawal] Starting for order {order_id}, KIZ: {kiz_code[:20]}..., Receipt: {receipt_number}")

    with Session(sync_engine) as db:
        # Get seller
        seller = db.execute(select(Seller).where(Seller.id == seller_id)).scalar_one_or_none()
        if not seller:
            logger.error(f"Seller {seller_id} not found")
            return

        if not seller.cz_token_encrypted:
            logger.error(f"Seller {seller_id} has no CZ token")
            _log_audit(db, seller_id, "cz_withdrawal", "FAILED",
                       "order", str(order_id), error="No CZ token configured")
            return

        # Get order
        order = db.execute(select(Order).where(
            and_(Order.id == order_id, Order.seller_id == seller_id)
        )).scalar_one_or_none()

        if not order:
            logger.error(f"Order {order_id} not found for seller {seller_id}")
            return

        # Check if already withdrawn
        if order.kiz_status == KizStatus.WITHDRAWN:
            logger.info(f"Order {order_id} KIZ already withdrawn, skipping")
            return

        # Create KIZ operation record
        kiz_op = KizOperation(
            seller_id=seller_id,
            order_id=order_id,
            kiz_code=kiz_code,
            operation=KizOperationType.WITHDRAWAL,
            status="PENDING",
            retries=self.request.retries,
        )
        db.add(kiz_op)
        db.flush()
        kiz_op_id = str(kiz_op.id)

        # Decrypt credentials
        cz_token = decrypt(seller.cz_token_encrypted)
        cz_inn = seller.cz_inn

        # Run async code safely in both sync and async environments
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
                        _do_withdrawal(
                            inn=cz_inn,
                            token=cz_token,
                            cert_thumbprint=seller.cz_cert_path,
                            kiz_codes=[kiz_code],
                            price_kopecks=price_kopecks,
                            mod_fias=seller.mod_fias,
                            mod_kpp=seller.mod_kpp,
                            wb_order_id=order_id,
                            receipt_number=receipt_number,
                            receipt_date=receipt_date,
                        )
                    ).result()
            else:
                doc_id = asyncio.run(
                    _do_withdrawal(
                        inn=cz_inn,
                        token=cz_token,
                        cert_thumbprint=seller.cz_cert_path,
                        kiz_codes=[kiz_code],
                        price_kopecks=price_kopecks,
                        mod_fias=seller.mod_fias,
                        mod_kpp=seller.mod_kpp,
                        wb_order_id=order_id,
                        receipt_number=receipt_number,
                        receipt_date=receipt_date,
                    )
                )

            # Update order
            order.kiz_status = KizStatus.WITHDRAWN
            order.kiz_cz_status = "RETIRED"
            order.kiz_cz_status_updated_at = datetime.now(timezone.utc)
            order.cz_withdrawal_doc_id = doc_id
            order.updated_at = datetime.now(timezone.utc)

            # Update KIZ operation
            kiz_op.status = "SUCCESS"
            kiz_op.cz_doc_id = doc_id
            kiz_op.updated_at = datetime.now(timezone.utc)

            _log_audit(db, seller_id, "cz_withdrawal", "SUCCESS",
                       "order", str(order_id),
                       payload={"doc_id": doc_id, "kiz_code": kiz_code[:20], "receipt_number": receipt_number})

            db.commit()
            logger.info(f"[CZ Withdrawal] SUCCESS for order {order_id}, doc_id: {doc_id}")

            # Notify manager
            if seller.telegram_bot_token_encrypted and seller.telegram_chat_ids:
                send_cz_status_notification.delay(
                    seller_id=seller_id,
                    order_id=order_id,
                    success=True,
                    doc_id=doc_id,
                )

        except Exception as exc:
            error_msg = str(exc)
            logger.error(f"[CZ Withdrawal] FAILED for order {order_id}: {error_msg}")

            kiz_op.status = "FAILED"
            kiz_op.error_message = error_msg
            kiz_op.retries = self.request.retries
            kiz_op.updated_at = datetime.now(timezone.utc)

            order.kiz_status = KizStatus.ERROR
            order.updated_at = datetime.now(timezone.utc)

            _log_audit(db, seller_id, "cz_withdrawal", "FAILED",
                       "order", str(order_id), error=error_msg)

            db.commit()

            # Retry if possible
            if self.request.retries < self.max_retries:
                raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))

            # Max retries exceeded — notify admin
            if seller.telegram_bot_token_encrypted and seller.telegram_chat_ids:
                send_cz_status_notification.delay(
                    seller_id=seller_id,
                    order_id=order_id,
                    success=False,
                    error=error_msg,
                )


async def _do_withdrawal(
    inn: str,
    token: str,
    cert_thumbprint: Optional[str],
    kiz_codes: list[str],
    price_kopecks: int,
    mod_fias: Optional[str],
    mod_kpp: Optional[str],
    wb_order_id: int,
    receipt_number: Optional[str] = None,
    receipt_date: Optional[str] = None,
) -> str:
    """Async withdrawal using CZClient."""
    from app.services.cz_client import CZClient

    async with CZClient(inn=inn, token=token, cert_thumbprint=cert_thumbprint) as client:
        doc_id = await client.withdraw_from_circulation(
            kiz_codes=kiz_codes,
            price_kopecks=price_kopecks,
            mod_fias=mod_fias,
            mod_kpp=mod_kpp,
            wb_order_id=wb_order_id,
            receipt_number=receipt_number,
            receipt_date=receipt_date,
            wait_for_result=True,  # Wait for GIS MT processing
        )
    return doc_id


@celery_app.task(
    name="app.agents.cz_withdrawal.process_seller_archive",
    queue="cz_operations",
    bind=True,
    max_retries=2,
)
def process_seller_archive(self, seller_id: str, archive_data: list[dict]):
    """
    Process WB archive data for batch CZ withdrawal.
    Called by archive_processor after downloading WB sales archive.

    archive_data: list of dicts with keys:
        - order_id: int
        - kiz_code: str
        - price: float (in rubles)
        - sale_date: str (ISO)
        - status: 'sold' or 'returned'
    """
    logger.info(f"[CZ Archive] Processing {len(archive_data)} records for seller {seller_id}")

    for record in archive_data:
        order_id = record.get("order_id")
        kiz_code = record.get("kiz_code")
        status = record.get("status", "sold")
        price_rubles = record.get("price", 0)
        price_kopecks = record.get("price_kopecks") or int(price_rubles * 100)
        receipt_number = record.get("receipt_number")
        receipt_date = record.get("receipt_date") or record.get("sale_date")

        if not kiz_code or not order_id:
            continue

        if status in ("sold", "sale", "Продажа", "Продано"):
            withdraw_order_kiz.apply_async(
                kwargs={
                    "seller_id": seller_id,
                    "order_id": order_id,
                    "kiz_code": kiz_code,
                    "price_kopecks": price_kopecks,
                    "receipt_number": receipt_number,
                    "receipt_date": receipt_date,
                },
                queue="cz_operations",
                countdown=2,  # Small delay to avoid overwhelming ГИС МТ
            )
        elif status in ("returned", "return", "Возврат", "Отказ покупателем"):
            from app.agents.cz_return import return_order_kiz
            return_order_kiz.apply_async(
                kwargs={
                    "seller_id": seller_id,
                    "order_id": order_id,
                    "kiz_code": kiz_code,
                },
                queue="cz_operations",
                countdown=2,
            )

    logger.info(f"[CZ Archive] Queued {len(archive_data)} CZ operations for seller {seller_id}")


def _log_audit(
    db: Session,
    seller_id: str,
    agent: str,
    action: str,
    entity_type: str,
    entity_id: str,
    payload: Optional[dict] = None,
    error: Optional[str] = None,
):
    """Write audit log entry."""
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
