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
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, create_engine
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
        notification_mode = Column(String, default="instant")
        notification_schedule = Column(JSON, default=list)
        timezone = Column(String, default="Europe/Moscow")
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    class Order(Base):  # type: ignore[no-redef]
        __tablename__ = "orders"

        id = Column(Integer, primary_key=True, autoincrement=True)
        wb_order_id = Column(Integer, index=True, nullable=True)
        seller_id = Column(String, ForeignKey("sellers.id"), nullable=False, index=True)
        status = Column(String, default="NEW", index=True)
        notified_at = Column(DateTime(timezone=True), nullable=True)
        chrt_id = Column(Integer, nullable=True)
        nm_id = Column(Integer, nullable=True)
        article = Column(String, nullable=True)
        brand = Column(String, nullable=True)
        subject = Column(String, nullable=True)
        name = Column(String, nullable=True)
        tech_size = Column(String, nullable=True)
        wb_size = Column(String, nullable=True)
        price = Column(Numeric(10, 2), nullable=True)
        kiz_required = Column(Boolean, default=False)
        kiz_status = Column(String, default="NOT_REQUIRED")
        wb_created_at = Column(DateTime(timezone=True), nullable=True)
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

        def get_cards_catalog(self, limit: int = 100) -> dict:
            return {"by_vendor_code": {}, "by_nm_id": {}, "by_chrt_id": {}}

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
            return None


# Alias for backward-compatibility with prompt signature get_stickers.si(...)
get_stickers = get_order_sticker


def _check_kiz_required(
    order_raw: dict,
    subject: Optional[str] = None,
    tnved: Optional[str] = None,
) -> bool:
    """
    Determine if KIZ / SGTIN marking is required for an order.

    Делегирует в is_kiz_required (wb_client), который проверяет:
    1. Наличие "sgtin" или "kiz" в requiredMeta.
    2. При отсутствии "sgtin" в requiredMeta (включая пустой список requiredMeta: []) —
       проверяет код ТН ВЭД и категорию/название товара по перечню маркируемых товаров.
    """
    try:
        from app.services.wb_client import is_kiz_required
        subj = subject or (order_raw.get("subject") if isinstance(order_raw, dict) else None) or (order_raw.get("name") if isinstance(order_raw, dict) else None)
        return is_kiz_required(
            subject=subj,
            tnved=tnved,
            order_raw=order_raw,
        )
    except Exception as exc:
        logger.warning(f"Error in is_kiz_required, applying resilient fallback: {exc}")
        if order_raw and isinstance(order_raw, dict):
            required_meta = order_raw.get("requiredMeta")
            if required_meta:
                if isinstance(required_meta, list):
                    if any(str(item).lower() in ("sgtin", "kiz") for item in required_meta):
                        return True
                elif isinstance(required_meta, str):
                    if "sgtin" in required_meta.lower() or "kiz" in required_meta.lower():
                        return True

        clean_tnved = str(tnved or (order_raw.get("tnved") if isinstance(order_raw, dict) else "") or "").replace(" ", "").replace(".", "").replace("-", "").strip()
        marked_tnved_prefixes = (
            "61", "62", "64",
            "6301", "6302", "6303", "6304",
            "6504", "6505",
            "4203", "4303",
            "3303",
            "4011",
            "9004", "9006",
        )
        if clean_tnved and any(clean_tnved.startswith(p) for p in marked_tnved_prefixes):
            return True

        subj = str(subject or (order_raw.get("subject") if isinstance(order_raw, dict) else "") or (order_raw.get("name") if isinstance(order_raw, dict) else "") or "").lower().strip()
        marked_subjects = (
            "капор", "капоры", "юбк", "брюк", "джинс", "худи", "свитшот", "толстовк", "свитер",
            "кофт", "кардиган", "рубашк", "блузк", "футболк", "поло", "топ", "лонгслив", "куртк",
            "пальто", "пуховик", "ветровк", "плащ", "жакет", "пиджак", "костюм", "плать",
            "сарафан", "комбинезон", "шорт", "пижам", "халат", "варежк", "перчатк", "шарф",
            "манишк", "платок", "панам", "кепк", "шапк", "головн", "одежд", "трикотаж", "обув",
            "ботинк", "туфл", "кроссовк", "сапог", "сандал", "белье постельн", "постельн", "полотенц",
            "текстиль", "духи", "туалетная вода", "парфюм", "парфюмер",
        )
        if subj and any(kw in subj for kw in marked_subjects):
            return True

        return False


def _resolve_order_metadata(
    order_raw: dict,
    seller: Seller,
    session: Session,
    wb_client: WBClient,
    catalog_cache: dict,
    wb_order_id_int: int,
) -> tuple[dict, dict]:
    """
    Resolve product metadata (chrt_id, nm_id, name, brand, subject, sizes, tnved) hierarchically:
    1. Local database cache (existing orders for this seller matching chrt_id, nm_id, or article)
    2. WB Content API (wb_client.get_cards_catalog())
    3. Fallback defaults (article name / order ID placeholder, default category/brand)

    Returns:
      (resolved_meta_dict, updated_catalog_cache)
    """
    chrt_raw = order_raw.get("chrtId") or order_raw.get("chrt_id")
    nm_raw = order_raw.get("nmId") or order_raw.get("nm_id")

    chrt_id: Optional[int] = None
    if chrt_raw is not None:
        try:
            chrt_id = int(chrt_raw)
        except (ValueError, TypeError):
            chrt_id = None

    nm_id: Optional[int] = None
    if nm_raw is not None:
        try:
            nm_id = int(nm_raw)
        except (ValueError, TypeError):
            nm_id = None

    article = str(order_raw.get("article") or "").strip()

    prod_name = order_raw.get("name") or None
    prod_subj = order_raw.get("subject") or None
    prod_brand = order_raw.get("brand") or None
    tech_size = order_raw.get("techSize") or order_raw.get("tech_size") or None
    wb_size = order_raw.get("wbSize") or order_raw.get("wb_size") or None
    prod_tnved = order_raw.get("tnved") or None

    # Step 1: Query local DB cache if any core metadata field is missing
    if not (prod_name and prod_brand and prod_subj):
        cached_order = None
        if chrt_id:
            cached_order = session.query(Order).filter(
                Order.seller_id == seller.id,
                Order.chrt_id == chrt_id,
                Order.name.isnot(None),
                Order.name != "",
            ).order_by(Order.id.desc()).first()

        if not cached_order and nm_id:
            cached_order = session.query(Order).filter(
                Order.seller_id == seller.id,
                Order.nm_id == nm_id,
                Order.name.isnot(None),
                Order.name != "",
            ).order_by(Order.id.desc()).first()

        if not cached_order and article:
            cached_order = session.query(Order).filter(
                Order.seller_id == seller.id,
                Order.article == article,
                Order.name.isnot(None),
                Order.name != "",
            ).order_by(Order.id.desc()).first()

        if cached_order:
            prod_name = prod_name or cached_order.name
            prod_subj = prod_subj or cached_order.subject
            prod_brand = prod_brand or cached_order.brand
            tech_size = tech_size or cached_order.tech_size
            wb_size = wb_size or cached_order.wb_size
            if chrt_id is None and cached_order.chrt_id:
                chrt_id = cached_order.chrt_id
            if nm_id is None and cached_order.nm_id:
                nm_id = cached_order.nm_id

    # Step 2: Query WB Content API Catalog if still missing
    if not (prod_name and prod_brand and prod_subj):
        if "by_vendor_code" not in catalog_cache:
            try:
                cat_res = wb_client.get_cards_catalog()
                if inspect.isawaitable(cat_res):
                    cat_res = asyncio.run(cat_res)
                if isinstance(cat_res, dict):
                    if "by_vendor_code" in cat_res or "by_nm_id" in cat_res or "by_chrt_id" in cat_res:
                        catalog_cache.update(cat_res)
                    else:
                        catalog_cache.setdefault("by_vendor_code", {})
                        catalog_cache.setdefault("by_nm_id", {})
                        catalog_cache.setdefault("by_chrt_id", {})
                        for k, v in cat_res.items():
                            if isinstance(v, dict):
                                if isinstance(k, int) or str(k).isdigit():
                                    catalog_cache["by_nm_id"][int(k)] = v
                                    catalog_cache["by_chrt_id"][int(k)] = v
                                if isinstance(k, str):
                                    catalog_cache["by_vendor_code"][k] = v
                                    catalog_cache["by_vendor_code"][k.lower()] = v
                else:
                    catalog_cache.update({"by_vendor_code": {}, "by_nm_id": {}, "by_chrt_id": {}})
            except Exception as exc:
                logger.warning(f"Failed to fetch cards catalog for seller {seller.id}: {exc}")
                catalog_cache.update({"by_vendor_code": {}, "by_nm_id": {}, "by_chrt_id": {}})

        by_chrt = catalog_cache.get("by_chrt_id") if isinstance(catalog_cache.get("by_chrt_id"), dict) else {}
        by_vendor = catalog_cache.get("by_vendor_code") if isinstance(catalog_cache.get("by_vendor_code"), dict) else {}
        by_nm = catalog_cache.get("by_nm_id") if isinstance(catalog_cache.get("by_nm_id"), dict) else {}

        if chrt_id and chrt_id in by_chrt:
            cinfo = by_chrt[chrt_id]
            if isinstance(cinfo, dict):
                prod_name = prod_name or cinfo.get("title")
                prod_subj = prod_subj or cinfo.get("subjectName")
                prod_brand = prod_brand or cinfo.get("brand")
                prod_tnved = prod_tnved or cinfo.get("tnved")
                tech_size = tech_size or cinfo.get("techSize")
                wb_size = wb_size or cinfo.get("wbSize")
                if nm_id is None and cinfo.get("nmID"):
                    try:
                        nm_id = int(cinfo.get("nmID"))
                    except (ValueError, TypeError):
                        pass
        elif article and (article in by_vendor or article.lower() in by_vendor):
            cinfo = by_vendor.get(article) or by_vendor.get(article.lower())
            if isinstance(cinfo, dict):
                prod_name = prod_name or cinfo.get("title")
                prod_subj = prod_subj or cinfo.get("subjectName")
                prod_brand = prod_brand or cinfo.get("brand")
                prod_tnved = prod_tnved or cinfo.get("tnved")
                if nm_id is None and cinfo.get("nmID"):
                    try:
                        nm_id = int(cinfo.get("nmID"))
                    except (ValueError, TypeError):
                        pass
                sizes = cinfo.get("sizes") or []
                matched_size = None
                if chrt_id and isinstance(sizes, list):
                    for s in sizes:
                        if isinstance(s, dict) and s.get("chrtID") == chrt_id:
                            matched_size = s
                            break
                if not matched_size and isinstance(sizes, list) and sizes:
                    for s in sizes:
                        if isinstance(s, dict):
                            matched_size = s
                            break
                if matched_size and isinstance(matched_size, dict):
                    tech_size = tech_size or matched_size.get("techSize")
                    wb_size = wb_size or matched_size.get("wbSize")
                    if chrt_id is None and matched_size.get("chrtID"):
                        try:
                            chrt_id = int(matched_size.get("chrtID"))
                        except (ValueError, TypeError):
                            pass
        elif nm_id and nm_id in by_nm:
            cinfo = by_nm[nm_id]
            if isinstance(cinfo, dict):
                prod_name = prod_name or cinfo.get("title")
                prod_subj = prod_subj or cinfo.get("subjectName")
                prod_brand = prod_brand or cinfo.get("brand")
                prod_tnved = prod_tnved or cinfo.get("tnved")
                sizes = cinfo.get("sizes") or []
                matched_size = None
                if chrt_id and isinstance(sizes, list):
                    for s in sizes:
                        if isinstance(s, dict) and s.get("chrtID") == chrt_id:
                            matched_size = s
                            break
                if not matched_size and isinstance(sizes, list) and sizes:
                    for s in sizes:
                        if isinstance(s, dict):
                            matched_size = s
                            break
                if matched_size and isinstance(matched_size, dict):
                    tech_size = tech_size or matched_size.get("techSize")
                    wb_size = wb_size or matched_size.get("wbSize")
                    if chrt_id is None and matched_size.get("chrtID"):
                        try:
                            chrt_id = int(matched_size.get("chrtID"))
                        except (ValueError, TypeError):
                            pass

    # Step 3: Graceful Defaults / Fallbacks
    final_name = prod_name or (f"{article} (WB #{wb_order_id_int})" if article else f"Заказ #{wb_order_id_int}")
    final_subj = prod_subj or "Товар"
    final_brand = prod_brand or getattr(seller, "name", None) or "WB"

    return {
        "chrt_id": chrt_id,
        "nm_id": nm_id,
        "article": article,
        "name": final_name,
        "brand": final_brand,
        "subject": final_subj,
        "tech_size": tech_size,
        "wb_size": wb_size,
        "tnved": prod_tnved,
    }, catalog_cache


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
       - Enrich metadata (hierarchical DB cache -> WB Content API catalog -> fallbacks)
       - Check if kiz_required (using category, TN VED, or requiredMeta)
       - Save order to DB with status=NEW and full product details
    d. Log to audit_log
    e. Return (order_ids, order_payloads) — caller dispatches notifications
    """
    from decimal import Decimal, InvalidOperation
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

    catalog_cache: dict = {}
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

        meta, catalog_cache = _resolve_order_metadata(
            order_raw=order_raw,
            seller=seller,
            session=session,
            wb_client=wb_client,
            catalog_cache=catalog_cache,
            wb_order_id_int=wb_order_id_int,
        )

        kiz_required = _check_kiz_required(
            order_raw=order_raw,
            subject=meta.get("subject") or meta.get("name"),
            tnved=meta.get("tnved"),
        )

        raw_created = order_raw.get("createdAt") or order_raw.get("created_at")
        if raw_created:
            try:
                wb_created_dt = datetime.fromisoformat(str(raw_created).replace("Z", "+00:00"))
            except Exception:
                wb_created_dt = datetime.now(timezone.utc)
        else:
            wb_created_dt = datetime.now(timezone.utc)

        raw_price = order_raw.get("price", 0)
        try:
            if isinstance(raw_price, int) and raw_price > 10000:
                price_dec = Decimal(str(raw_price / 100.0))
            elif raw_price is not None:
                price_dec = Decimal(str(raw_price))
            else:
                price_dec = Decimal("0.00")
        except (InvalidOperation, TypeError, ValueError):
            price_dec = Decimal("0.00")

        new_order = Order(
            id=wb_order_id_int,
            seller_id=seller.id,
            status=OrderStatus.NEW,
            wb_created_at=wb_created_dt,
            chrt_id=meta.get("chrt_id"),
            nm_id=meta.get("nm_id"),
            article=meta.get("article") or None,
            brand=meta.get("brand"),
            subject=meta.get("subject"),
            name=meta.get("name"),
            tech_size=meta.get("tech_size"),
            wb_size=meta.get("wb_size"),
            price=price_dec,
            kiz_required=kiz_required,
            kiz_status=KizStatus.PENDING if kiz_required else KizStatus.NOT_REQUIRED,
            notified_at=None,
            created_at=datetime.now(timezone.utc),
        )
        session.add(new_order)
        session.commit()

        processed_order_ids.append(wb_order_id_int)
        processed_order_payloads.append({
            "id": wb_order_id_int,
            "name": meta.get("name") or "—",
            "brand": meta.get("brand") or "—",
            "subject": meta.get("subject") or "—",
            "article": meta.get("article") or "",
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

                notification_mode = getattr(seller, "notification_mode", "instant") or "instant"
                if notification_mode == "scheduled":
                    logger.info(
                        f"Seller {seller_id_str} is in 'scheduled' notification mode. "
                        f"Suppressing instant Telegram push for {len(new_ids)} order(s). "
                        f"Pre-generating stickers in background."
                    )
                    for oid in new_ids:
                        get_stickers.delay(seller_id_str, oid)
                else:
                    if len(new_ids) == 1:
                        # 1 заказ: немедленно отправляем уведомление в Telegram и фоном качаем стикер
                        order_payload = new_payloads[0] if new_payloads else None
                        _notify_new_order.delay(seller_id_str, new_ids[0], order_payload)
                        get_stickers.delay(seller_id_str, new_ids[0])
                    else:
                        # Несколько заказов: немедленный пакетный алерт в Telegram и фоновое скачивание стикеров
                        notify_batch_orders.delay(seller_id_str, new_payloads)
                        for oid in new_ids:
                            get_stickers.delay(seller_id_str, oid)

            except WBUnauthorizedError as exc:
                session.rollback()
                logger.error(f"WB API Unauthorized for seller {seller_id_str}: {exc}. Disabling seller polling.")
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
                    message=f"Seller {seller_id_str} disabled due to invalid WB API token: {exc}",
                )
            except WBRateLimitError as exc:
                session.rollback()
                logger.warning(f"WB API Rate Limit encountered for seller {seller_id_str}: {exc}")
                rate_limit_occurred = True
            except Exception as exc:
                session.rollback()
                logger.exception(f"Unexpected error while polling seller {seller_id_str}: {exc}")

        if rate_limit_occurred:
            countdown = 60 * (2 ** self.request.retries)
            logger.info(f"Retrying poll_all_sellers task in {countdown}s due to WBRateLimitError")
            raise self.retry(exc=WBRateLimitError("Rate limit encountered during seller polling"), countdown=countdown)

    logger.info(f"Completed poll_all_sellers batch. Processed {total_processed} new orders across {len(active_sellers)} sellers.")
    return {"status": "success", "active_sellers": len(active_sellers), "new_orders": total_processed}
