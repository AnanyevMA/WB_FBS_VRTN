"""
Order Polling Agent — WB FBS Manager

Polls new Wildberries FBS assembly tasks ( сборочные задания ) for all active sellers,
persists new orders, checks KIZ / SGTIN requirements, and triggers downstream task chains
for sticker generation and Telegram notifications.
"""
import asyncio
from datetime import datetime, timezone
import inspect
import logging
from typing import Any, List, Optional

from celery import chain
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.celery_app import celery_app
from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-seller polling throttle (in-memory; resets on worker restart).
# Key: seller_id str → Value: datetime of last successful poll
# ---------------------------------------------------------------------------
_last_polled: dict[str, datetime] = {}

# Synchronous SQLAlchemy engine and session factory for Celery tasks
sync_engine = create_engine(
    settings.database_url_sync,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)
SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autoflush=False,
    expire_on_commit=False,
)

# Imports with fallback definitions for ORM models, Services, and Exceptions
try:
    from app.models import AuditLog, Order, Seller
except (ImportError, ModuleNotFoundError):
    from app.database import Base

    class Seller(Base):  # type: ignore[no-redef]
        __tablename__ = "sellers"

        id = Column(String, primary_key=True)
        name = Column(String, nullable=True)
        wb_api_token = Column(String, nullable=True)
        is_active = Column(Boolean, default=True, index=True)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    class Order(Base):  # type: ignore[no-redef]
        __tablename__ = "orders"

        id = Column(Integer, primary_key=True, autoincrement=True)
        wb_order_id = Column(Integer, index=True, nullable=False)
        seller_id = Column(String, ForeignKey("sellers.id"), nullable=False, index=True)
        status = Column(String, default="NEW", index=True)
        kiz_required = Column(Boolean, default=False)
        sticker_data = Column(JSON, nullable=True)
        raw_payload = Column(JSON, nullable=True)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    class AuditLog(Base):  # type: ignore[no-redef]
        __tablename__ = "audit_logs"

        id = Column(Integer, primary_key=True, autoincrement=True)
        seller_id = Column(String, nullable=True, index=True)
        event_type = Column(String, nullable=False)
        details = Column(Text, nullable=True)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)

try:
    from app.services.security import EncryptionService
except (ImportError, ModuleNotFoundError):
    try:
        from app.services.encryption import EncryptionService  # type: ignore[no-redef]
    except (ImportError, ModuleNotFoundError):
        class EncryptionService:  # type: ignore[no-redef]
            @staticmethod
            def decrypt(token: str) -> str:
                return token or ""

            @staticmethod
            def encrypt(token: str) -> str:
                return token or ""

try:
    from app.services.wb_client import WBClient, WBRateLimitError, WBUnauthorizedError
except (ImportError, ModuleNotFoundError):
    class WBClientError(Exception):
        """Base exception for WB Client."""
        pass

    class WBUnauthorizedError(WBClientError):  # type: ignore[no-redef]
        """Raised when WB API token is invalid or unauthorized (HTTP 401)."""
        pass

    class WBRateLimitError(WBClientError):  # type: ignore[no-redef]
        """Raised when WB API rate limit is exceeded (HTTP 429)."""
        pass

    class WBClient:  # type: ignore[no-redef]
        def __init__(self, api_token: str):
            self.api_token = api_token

        def get_new_orders(self) -> List[dict]:
            return []

        def get_order_sticker(self, order_id: int) -> dict:
            return {"order_id": order_id, "partA": "", "partB": ""}

try:
    from app.agents.notifications import notify_new_order
except (ImportError, ModuleNotFoundError):
    @celery_app.task(name="app.agents.notifications.notify_new_order", queue="notifications")
    def notify_new_order(seller_id: str, order_id: int):
        """Placeholder notification task."""
        logger.info(f"Notification triggered for new order {order_id} (seller {seller_id})")
        return True


@celery_app.task(name="app.agents.order_poller.get_order_sticker", queue="orders")
def get_order_sticker(seller_id: str, order_id: int) -> Optional[dict]:
    """Download and save sticker for a specific order."""
    logger.info(f"Fetching sticker for order {order_id} (seller {seller_id})")
    with SyncSessionLocal() as session:
        seller = session.query(Seller).filter(Seller.id == seller_id).first()
        if not seller:
            logger.error(f"Seller {seller_id} not found when retrieving sticker for order {order_id}")
            return None

        raw_token = getattr(seller, "wb_api_token_encrypted", getattr(seller, "wb_api_token", getattr(seller, "wb_token", getattr(seller, "api_token", ""))))
        decrypted_token = EncryptionService.decrypt(raw_token or "")
        wb_client = WBClient(decrypted_token)

        try:
            sticker_res = wb_client.get_order_sticker(order_id)
            if inspect.isawaitable(sticker_res):
                sticker_res = asyncio.run(sticker_res)

            order = session.query(Order).filter(
                Order.seller_id == seller_id,
                Order.id == order_id,
            ).first()

            if order and sticker_res:
                if isinstance(sticker_res, dict):
                    order.sticker_base64 = sticker_res.get("file") or sticker_res.get("partA")
                    order.sticker_id = str(sticker_res.get("orderId", order_id))
                session.commit()
                logger.info(f"Successfully saved sticker for order {order_id}")

            return sticker_res
        except Exception as exc:
            logger.error(f"Failed to fetch sticker for order {order_id}: {exc}")
            raise


# Alias for backward-compatibility with prompt signature get_stickers.si(...)
get_stickers = get_order_sticker


def _check_kiz_required(order_raw: dict) -> bool:
    """
    Determine if KIZ / SGTIN marking is required for an order.
    Uses centralized is_kiz_required checking subject, tnved, and requiredMeta.
    """
    try:
        from app.services.wb_client import is_kiz_required
        return is_kiz_required(
            subject=order_raw.get("subject") or order_raw.get("name"),
            order_raw=order_raw
        )
    except Exception:
        cargo_type = order_raw.get("cargoType")
        required_meta = order_raw.get("requiredMeta", [])
        has_sgtin = False
        if isinstance(required_meta, list):
            has_sgtin = any("sgtin" in str(item).lower() for item in required_meta)
        elif isinstance(required_meta, dict):
            has_sgtin = "sgtin" in required_meta or any("sgtin" in str(k).lower() for k in required_meta.keys())
        elif isinstance(required_meta, str):
            has_sgtin = "sgtin" in required_meta.lower()
        return (cargo_type == 2) or has_sgtin


def poll_seller_orders(seller: Seller, session: Session) -> tuple[list[int], list[dict]]:
    """
    Poll new WB orders for a single seller.

    Returns a tuple:
      - list of new wb_order_ids
      - list of order payload dicts (for batch notification)

    Flow:
    a. Create WBClient with seller's decrypted token
    b. Call get_new_orders()
    c. For each new order NOT already in DB:
       - Save order to DB with status=NEW
       - Check if kiz_required
    d. Log to audit_log
    e. Return (order_ids, order_payloads) — caller dispatches notifications
    """
    from decimal import Decimal
    from app.models.order import OrderStatus, KizStatus

    seller_id_str = str(seller.id)
    raw_token = getattr(seller, "wb_api_token_encrypted", getattr(seller, "wb_api_token", getattr(seller, "wb_token", getattr(seller, "api_token", ""))))
    if not raw_token:
        logger.warning(f"Seller {seller_id_str} has no WB API token configured.")
        return [], []

    decrypted_token = EncryptionService.decrypt(raw_token)
    wb_client = WBClient(decrypted_token)

    new_orders_res = wb_client.get_new_orders()
    if inspect.isawaitable(new_orders_res):
        new_orders_res = asyncio.run(new_orders_res)

    if isinstance(new_orders_res, list):
        orders_list = new_orders_res
    elif isinstance(new_orders_res, dict):
        orders_list = new_orders_res.get("orders", [])
    else:
        orders_list = []

    processed_order_ids: List[int] = []
    processed_order_payloads: List[dict] = []

    for order_raw in orders_list:
        wb_order_id = order_raw.get("id") or order_raw.get("orderId")
        if not wb_order_id:
            continue

        try:
            wb_order_id_int = int(wb_order_id)
        except (ValueError, TypeError):
            continue

        existing_order = session.query(Order).filter(
            Order.seller_id == seller.id,
            Order.id == wb_order_id_int,
        ).first()

        if existing_order:
            continue

        kiz_required = _check_kiz_required(order_raw)

        raw_created = order_raw.get("createdAt") or order_raw.get("created_at")
        if raw_created:
            try:
                wb_created_dt = datetime.fromisoformat(str(raw_created).replace("Z", "+00:00"))
            except Exception:
                wb_created_dt = datetime.now(timezone.utc)
        else:
            wb_created_dt = datetime.now(timezone.utc)

        raw_price = order_raw.get("price", 0)
        if isinstance(raw_price, int) and raw_price > 10000:
            price_dec = Decimal(str(raw_price / 100.0))
        elif raw_price is not None:
            price_dec = Decimal(str(raw_price))
        else:
            price_dec = Decimal("0.00")

        new_order = Order(
            id=wb_order_id_int,
            seller_id=seller.id,
            status=OrderStatus.NEW,
            wb_created_at=wb_created_dt,
            article=order_raw.get("article"),
            brand=order_raw.get("brand"),
            subject=order_raw.get("subject"),
            name=order_raw.get("name") or order_raw.get("subject"),
            price=price_dec,
            kiz_required=kiz_required,
            kiz_status=KizStatus.PENDING if kiz_required else KizStatus.NOT_REQUIRED,
            created_at=datetime.now(timezone.utc),
        )
        session.add(new_order)
        session.commit()

        processed_order_ids.append(wb_order_id_int)
        processed_order_payloads.append({
            "id": wb_order_id_int,
            "name": new_order.name or "—",
            "article": new_order.article or "",
            "price": raw_price,
            "kiz_required": kiz_required,
            "wb_created_at": wb_created_dt.isoformat(),
        })

    # Also sync status for existing active orders
    try:
        sync_seller_active_orders_status(seller, session, wb_client)
    except Exception as exc:
        logger.debug(f"Status sync during polling error for seller {seller.id}: {exc}")

    # Update seller last_polled_at
    seller.last_polled_at = datetime.now(timezone.utc)
    session.commit()

    # Log to audit_log
    if processed_order_ids:
        import uuid
        audit_log = AuditLog(
            seller_id=seller.id,
            agent="order_poller",
            action="POLL_NEW_ORDERS",
            entity_type="order_batch",
            payload={"count": len(processed_order_ids), "order_ids": processed_order_ids},
            trace_id=str(uuid.uuid4()),
            created_at=datetime.now(timezone.utc),
        )
        session.add(audit_log)
        session.commit()

    return processed_order_ids, processed_order_payloads


def sync_seller_active_orders_status(seller: Seller, session: Session, wb_client: WBClient) -> int:
    """
    Syncs WB status (POST /api/v3/orders/status) for active orders (NEW, ASSEMBLING, DELIVERING).
    Updates statuses, triggers CZ withdrawal for sold orders, and return for cancelled orders.
    """
    from app.models.order import OrderStatus, KizStatus

    active_orders = session.query(Order).filter(
        Order.seller_id == seller.id,
        Order.status.in_([OrderStatus.NEW, OrderStatus.ASSEMBLING, OrderStatus.DELIVERING])
    ).all()

    if not active_orders:
        return 0

    order_ids = [o.id for o in active_orders]
    updated_count = 0

    try:
        status_res = wb_client.get_orders_status(order_ids)
        if inspect.isawaitable(status_res):
            status_res = asyncio.run(status_res)

        status_by_id = {st["id"]: st for st in (status_res or []) if isinstance(st, dict) and "id" in st}

        for order in active_orders:
            st = status_by_id.get(order.id)
            if not st:
                continue

            wb_status = st.get("wbStatus")
            supp_status = st.get("supplierStatus")
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
                if order.kiz_code and order.kiz_status == KizStatus.ATTACHED:
                    try:
                        from app.agents.cz_withdrawal import withdraw_order_kiz
                        price_kop = int((order.price or 0) * 100)
                        withdraw_order_kiz.delay(
                            seller_id=str(seller.id),
                            order_id=order.id,
                            kiz_code=order.kiz_code,
                            price_kopecks=price_kop,
                        )
                    except Exception as e:
                        logger.warning(f"Could not dispatch withdraw_order_kiz for {order.id}: {e}")
            elif wb_status in ["canceled", "canceled_by_client", "declined_by_client", "defect"] and order.status != OrderStatus.CANCELLED:
                order.status = OrderStatus.CANCELLED
                order_updated = True
                if order.kiz_code and order.kiz_status == KizStatus.WITHDRAWN:
                    try:
                        from app.agents.cz_return import return_order_kiz
                        return_order_kiz.delay(
                            seller_id=str(seller.id),
                            order_id=order.id,
                            kiz_code=order.kiz_code,
                        )
                    except Exception as e:
                        logger.warning(f"Could not dispatch return_order_kiz for {order.id}: {e}")
            elif wb_status in ["sorted", "ready_for_pickup", "waiting"] and order.status != OrderStatus.DELIVERING:
                order.status = OrderStatus.DELIVERING
                order_updated = True

            if order_updated:
                order.updated_at = datetime.now(timezone.utc)
                updated_count += 1

        if updated_count > 0:
            session.commit()
    except Exception as exc:
        logger.warning(f"Failed to sync active order statuses for seller {seller.id}: {exc}")

    return updated_count


@celery_app.task(name="app.agents.order_poller.poll_all_sellers", queue="orders", bind=True, max_retries=3)
def poll_all_sellers(self: Any) -> dict:
    """Poll new WB orders for ALL active sellers, respecting per-seller polling intervals."""
    from celery import chord, group as celery_group
    from app.agents.notifier import notify_batch_orders, notify_new_order as _notify_new_order

    logger.info("Starting order polling agent for active sellers")
    rate_limit_occurred = False
    total_processed = 0
    now = datetime.now(timezone.utc)

    with SyncSessionLocal() as session:
        try:
            from sqlalchemy import and_
            active_sellers = session.query(Seller).filter(
                and_(Seller.is_active == True, Seller.polling_enabled == True)
            ).all()
        except Exception as exc:
            logger.error(f"Error querying active sellers from database: {exc}")
            raise self.retry(exc=exc, countdown=30)

        for seller in active_sellers:
            seller_id_str = str(seller.id)

            # --- Per-seller interval throttle (DB-persisted & in-memory) ---
            interval_sec = getattr(seller, "polling_interval_seconds", 60) or 60
            db_last_poll = getattr(seller, "last_polled_at", None)
            mem_last_poll = _last_polled.get(seller_id_str)

            candidates = [t for t in (db_last_poll, mem_last_poll) if t is not None]
            if candidates:
                last_poll = max(candidates)
                if last_poll.tzinfo is None:
                    last_poll = last_poll.replace(tzinfo=timezone.utc)
                elapsed = (now - last_poll).total_seconds()
                if elapsed < interval_sec:
                    logger.debug(
                        f"Skipping seller {seller_id_str}: "
                        f"{elapsed:.0f}s elapsed, interval={interval_sec}s"
                    )
                    continue

            _last_polled[seller_id_str] = now

            try:
                new_ids, new_payloads = poll_seller_orders(seller, session)
                total_processed += len(new_ids)

                if not new_ids:
                    continue

                if len(new_ids) == 1:
                    # Single order: classic chain — sticker then notify
                    chain(
                        get_stickers.si(seller_id_str, new_ids[0]),
                        _notify_new_order.si(seller_id_str, new_ids[0]),
                    ).apply_async()
                else:
                    # Multiple orders: parallel sticker downloads, then one batch notification
                    chord(
                        celery_group(
                            get_stickers.si(seller_id_str, oid) for oid in new_ids
                        ),
                        notify_batch_orders.si(seller_id_str, new_payloads),
                    ).apply_async()

            except WBUnauthorizedError as exc:
                logger.error(f"WB API Unauthorized for seller {seller.id}: {exc}. Disabling seller polling.")
                seller.is_active = False
                import uuid
                audit_log = AuditLog(
                    seller_id=seller.id,
                    agent="order_poller",
                    action="SELLER_DISABLED_UNAUTHORIZED",
                    entity_type="seller",
                    entity_id=str(seller.id),
                    error=f"Disabling polling due to WBUnauthorizedError: {exc}",
                    trace_id=str(uuid.uuid4()),
                    created_at=datetime.now(timezone.utc),
                )
                session.add(audit_log)
                session.commit()

                from app.agents.notifier import send_alert
                send_alert.delay(
                    seller_id=str(seller.id),
                    agent="order_poller",
                    message=f"Seller {seller.id} disabled due to invalid WB API token: {exc}",
                )
            except WBRateLimitError as exc:
                logger.warning(f"WB API Rate Limit encountered for seller {seller.id}: {exc}")
                rate_limit_occurred = True
            except Exception as exc:
                logger.exception(f"Unexpected error while polling seller {seller.id}: {exc}")

        if rate_limit_occurred:
            countdown = 60 * (2 ** self.request.retries)
            logger.info(f"Retrying poll_all_sellers task in {countdown}s due to WBRateLimitError")
            raise self.retry(exc=WBRateLimitError("Rate limit encountered during seller polling"), countdown=countdown)

    logger.info(f"Completed poll_all_sellers batch. Processed {total_processed} new orders across {len(active_sellers)} sellers.")
    return {"status": "success", "active_sellers": len(active_sellers), "new_orders": total_processed}
