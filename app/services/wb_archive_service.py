"""
WB Archive API Service — синхронизация архивных сборочных заданий WB (>3 дней).
Получает данные напрямую из GET /api/marketplace/v3/fbs/orders/archive:
- Выявляет отмененные заказы (canceled_by_client, declined_by_client, canceled, defect).
- Фиксирует прикрепленные КИЗ (metaDetails.sgtin) в Order и KizProductInfo.
- Поддерживает скользящее окно опроса (по умолчанию за последние 3 месяца).
"""
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.kiz import KizProductInfo
from app.models.order import KizStatus, Order, OrderStatus
from app.models.seller import Seller
from app.services.encryption import decrypt
from app.services.kiz_service import parse_kiz_code
from app.services.wb_client import WBClient

logger = logging.getLogger(__name__)

CANCELLED_WB_STATUSES = {
    "canceled",
    "canceled_by_client",
    "declined_by_client",
    "defect",
}
DELIVERED_WB_STATUSES = {"sold", "delivered"}


def get_recent_months(
    months_count: int = 3,
    base_date: Optional[datetime] = None,
) -> List[Tuple[int, int]]:
    """
    Возвращает список кортежей (year, month) за последние months_count месяцев в убывающем порядке.
    Корректно обрабатывает переход через границу года (например: Jan 2026 -> Dec 2025, Nov 2025).
    """
    if base_date is None:
        base_date = datetime.now(timezone.utc)
    
    cur_year = base_date.year
    cur_month = base_date.month
    result: List[Tuple[int, int]] = []

    for _ in range(max(1, months_count)):
        result.append((cur_year, cur_month))
        cur_month -= 1
        if cur_month < 1:
            cur_month = 12
            cur_year -= 1

    return result


def parse_wb_iso_datetime(val: Any) -> datetime:
    """Парсит ISO timestamp от Wildberries в timezone-aware UTC datetime."""
    if not val:
        return datetime.now(timezone.utc)
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    val_str = str(val).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(val_str)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def resolve_order_status(
    wb_status: Optional[str],
    supplier_status: Optional[str],
    current_status: Optional[OrderStatus] = None,
) -> OrderStatus:
    """Определяет актуальный статус сборочного задания на основе статусов WB."""
    wb_st_lower = str(wb_status or "").strip().lower()
    supp_st_lower = str(supplier_status or "").strip().lower()

    if wb_st_lower in CANCELLED_WB_STATUSES or supp_st_lower == "cancel":
        return OrderStatus.CANCELLED
    if wb_st_lower in DELIVERED_WB_STATUSES:
        return OrderStatus.DELIVERED
    if current_status:
        return current_status
    return OrderStatus.DELIVERED if supp_st_lower == "complete" else OrderStatus.SORTED


async def _upsert_archive_order(
    db: AsyncSession,
    seller: Seller,
    raw_order: Dict[str, Any],
    existing_order: Optional[Order],
    existing_kinfo: Optional[KizProductInfo],
    now_dt: datetime,
    existing_kinfo_map: Optional[Dict[str, KizProductInfo]] = None,
) -> Tuple[Order, bool, bool, bool]:
    """
    Внутренний хелпер: создает или обновляет Order и KizProductInfo по записи из архива.
    Возвращает: (order, is_created, is_cancelled, kiz_linked)
    """
    order_id = int(raw_order["id"])
    status_block = raw_order.get("status") or {}
    wb_status = str(status_block.get("wbStatus") or "").strip()
    supplier_status = str(status_block.get("supplierStatus") or "").strip()
    
    meta_details = raw_order.get("metaDetails") or {}
    kiz_code = str(meta_details.get("sgtin") or "").strip()
    clean_cis = parse_kiz_code(kiz_code).get("clean_cis") if kiz_code else None

    price_kop = (raw_order.get("priceInfo") or {}).get("price") or 0
    price_rub = Decimal(str(round(float(price_kop) / 100.0, 2))) if price_kop else Decimal("0.00")

    product = raw_order.get("product") or {}
    article = str(product.get("article") or "").strip() or None
    name = str(product.get("name") or "").strip() or None
    nm_id = product.get("nmId")
    chrt_id = product.get("chrtId")
    sticker_id = str(raw_order.get("stickerId") or "").strip() or None
    wb_created = parse_wb_iso_datetime(raw_order.get("createdAt"))

    is_created = False
    kiz_linked = False

    if existing_order:
        ord_obj = existing_order
        target_status = resolve_order_status(wb_status, supplier_status, ord_obj.status)
        ord_obj.status = target_status
        if wb_status:
            ord_obj.wb_status = wb_status
        if supplier_status:
            ord_obj.supplier_status = supplier_status
        if kiz_code and not ord_obj.kiz_code:
            ord_obj.kiz_code = kiz_code
            ord_obj.kiz_required = True
            kiz_linked = True
        if article and not ord_obj.article:
            ord_obj.article = article
        if price_rub and not ord_obj.price:
            ord_obj.price = price_rub
        ord_obj.updated_at = now_dt
    else:
        is_created = True
        target_status = resolve_order_status(wb_status, supplier_status, None)
        ord_obj = Order(
            id=order_id,
            seller_id=str(seller.id),
            status=target_status,
            wb_status=wb_status or None,
            supplier_status=supplier_status or None,
            wb_created_at=wb_created,
            chrt_id=chrt_id,
            nm_id=nm_id,
            article=article,
            name=name,
            price=price_rub,
            sticker_id=sticker_id,
            kiz_required=bool(kiz_code),
            kiz_code=kiz_code or None,
            kiz_status=KizStatus.ATTACHED if kiz_code else KizStatus.NOT_REQUIRED,
            created_at=wb_created,
            updated_at=now_dt,
        )
        db.add(ord_obj)
        if kiz_code:
            kiz_linked = True

    # Link/Register KizProductInfo
    if kiz_code:
        if not existing_kinfo:
            gtin_val = parse_kiz_code(kiz_code).get("gtin") or str(meta_details.get("gtin") or "") or "00000000000000"
            new_kinfo = KizProductInfo(
                id=str(uuid.uuid4()),
                seller_id=str(seller.id),
                order_id=order_id,
                kiz_code=kiz_code,
                clean_cis=clean_cis or kiz_code,
                gtin=gtin_val,
                serial_number=parse_kiz_code(kiz_code).get("serial"),
                article=article,
                product_name=name,
                created_at=now_dt,
                updated_at=now_dt,
            )
            db.add(new_kinfo)
            if existing_kinfo_map is not None:
                existing_kinfo_map[kiz_code] = new_kinfo
        elif existing_kinfo.order_id != order_id or not existing_kinfo.seller_id:
            existing_kinfo.order_id = order_id
            if not existing_kinfo.seller_id:
                existing_kinfo.seller_id = str(seller.id)
            existing_kinfo.updated_at = now_dt

    is_cancelled = ord_obj.status == OrderStatus.CANCELLED
    return ord_obj, is_created, is_cancelled, kiz_linked


async def sync_seller_archive_orders_recent_months(
    seller: Seller,
    db: AsyncSession,
    months_count: int = 3,
    target_months: Optional[List[Tuple[int, int]]] = None,
) -> Dict[str, Any]:
    """
    Синхронизирует архивные заказы продавца через WB API за последние months_count месяцев
    или за конкретный переданный список target_months.
    Обновляет заказы в БД, проставляет статус CANCELLED/DELIVERED и регистрирует КИЗ.
    """
    if not seller.wb_api_token_encrypted:
        raise ValueError(f"Seller {seller.name} (ID: {seller.id}) has no WB API token configured.")

    wb_token = decrypt(seller.wb_api_token_encrypted)
    if not target_months:
        target_months = get_recent_months(months_count=months_count)
    now_dt = datetime.now(timezone.utc)

    total_fetched = 0
    orders_created = 0
    orders_updated = 0
    cancelled_count = 0
    delivered_count = 0
    kiz_linked_count = 0
    months_processed: List[str] = []

    async with WBClient(wb_token) as client:
        for year, month in target_months:
            month_label = f"{year}-{month:02d}"
            months_processed.append(month_label)
            logger.info(f"[WB Archive Sync] Fetching archive orders for {seller.name} ({month_label})...")
            
            archive_orders = await client.get_all_archive_orders(year=year, month=month)
            if not archive_orders:
                continue

            total_fetched += len(archive_orders)
            order_ids = [int(o["id"]) for o in archive_orders if "id" in o]

            # Bulk load existing Orders and KizProductInfo
            existing_orders_map: Dict[int, Order] = {}
            if order_ids:
                stmt_orders = select(Order).where(
                    Order.seller_id == str(seller.id),
                    Order.id.in_(order_ids),
                )
                res_orders = await db.execute(stmt_orders)
                for o in res_orders.scalars().all():
                    existing_orders_map[o.id] = o

            kiz_codes = [
                str((o.get("metaDetails") or {}).get("sgtin") or "").strip()
                for o in archive_orders
                if (o.get("metaDetails") or {}).get("sgtin")
            ]
            existing_kinfo_map: Dict[str, KizProductInfo] = {}
            if kiz_codes:
                stmt_kinfo = select(KizProductInfo).where(
                    KizProductInfo.kiz_code.in_(kiz_codes),
                )
                res_kinfo = await db.execute(stmt_kinfo)
                for k in res_kinfo.scalars().all():
                    existing_kinfo_map[k.kiz_code] = k

            for raw in archive_orders:
                if not raw.get("id"):
                    continue
                oid = int(raw["id"])
                kiz = str((raw.get("metaDetails") or {}).get("sgtin") or "").strip()
                
                existing_ord = existing_orders_map.get(oid)
                existing_k = existing_kinfo_map.get(kiz)

                ord_obj, created, is_cancelled, linked = await _upsert_archive_order(
                    db=db,
                    seller=seller,
                    raw_order=raw,
                    existing_order=existing_ord,
                    existing_kinfo=existing_k,
                    now_dt=now_dt,
                    existing_kinfo_map=existing_kinfo_map,
                )

                if created:
                    orders_created += 1
                else:
                    orders_updated += 1
                if is_cancelled:
                    cancelled_count += 1
                elif ord_obj.status == OrderStatus.DELIVERED:
                    delivered_count += 1
                if linked:
                    kiz_linked_count += 1

    summary = {
        "seller_id": str(seller.id),
        "seller_name": seller.name,
        "months_processed": months_processed,
        "total_fetched": total_fetched,
        "orders_created": orders_created,
        "orders_updated": orders_updated,
        "cancelled_orders": cancelled_count,
        "delivered_orders": delivered_count,
        "kiz_linked": kiz_linked_count,
    }

    # Audit logging
    audit = AuditLog(
        seller_id=str(seller.id),
        agent="wb_archive_api",
        action="WB_ARCHIVE_API_SYNC",
        entity_type="archive_sync",
        entity_id=f"{months_processed[0]}_to_{months_processed[-1]}" if months_processed else "empty",
        payload=summary,
    )
    db.add(audit)
    await db.commit()

    logger.info(
        f"[WB Archive Sync] Completed for {seller.name}: {total_fetched} fetched, "
        f"{orders_created} created, {orders_updated} updated, {cancelled_count} cancelled, "
        f"{kiz_linked_count} KIZ linked."
    )
    return summary
