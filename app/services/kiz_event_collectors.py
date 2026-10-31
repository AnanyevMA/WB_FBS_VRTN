"""
KIZ Event Collectors — WB FBS Manager
Сборщики событий о КИЗ из источников WB для журнала kiz_events.
Сборщики ничего не решают и не создают пакеты. Выбираются только нужные колонки: у воркера мало памяти.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kiz_event import (
    EVENT_RETURN,
    EVENT_SALE,
    SOURCE_ARCHIVE_FILE,
    SOURCE_EXCISE,
    SOURCE_FINANCE,
    SOURCE_ITEM_RETURNS,
    SOURCE_ORDERS,
)
from app.models.order import KizStatus, Order, OrderStatus
from app.models.seller import Seller
from app.models.wb_finance import WbSalesReportRow
from app.services.encryption import decrypt
from app.services.kiz_event_service import save_events
from app.services.kiz_service import parse_kiz_code
from app.services.wb_analytics_client import WBAnalyticsClient
from app.services.wb_warehouse_sales_service import fetch_wb_excise_data

logger = logging.getLogger(__name__)

SOURCE_ORDER_STATUS = "wb_order_status"

FINANCE_SALE_OPERATION = "Продажа"
FINANCE_RETURN_OPERATION = "Возврат"
EXCISE_SALE_OPERATION_TYPE = 1
ITEM_RETURN_DELIVERED_STATUS = "Выдано"
WB_CANCELLED_STATUSES = ("canceled", "canceled_by_client", "declined_by_client", "defect")

EXCISE_DAYS = 30
ITEM_RETURNS_DAYS = 90
ORDER_STATUS_DAYS = 90
ORDER_STATUS_CHUNK = 1000


@dataclass
class CollectResult:
    seen: int = 0
    drafts: List[Dict[str, Any]] = field(default_factory=list)
    skipped: Dict[str, int] = field(default_factory=dict)
    changed: int = 0

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def clean_cis_of(raw_kiz: Any) -> Optional[str]:
    raw = str(raw_kiz or "").strip()
    if not raw:
        return None
    return parse_kiz_code(raw).get("clean_cis") or raw


def parse_event_datetime(value: Any) -> Optional[datetime]:
    """Accepts datetime, date and ISO strings with or without a timezone. WB dates without one count as UTC."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _price(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _text(value: Any, limit: int) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text[:limit] or None


# --- Finance reports -------------------------------------------------------------------------------------------------

def finance_row_to_draft(row: Any) -> Optional[Dict[str, Any]]:
    """Only real sales and returns: logistics, delivery and compensations do not change the state of a KIZ."""
    if row.seller_oper_name == FINANCE_SALE_OPERATION:
        event_type = EVENT_SALE
    elif row.seller_oper_name == FINANCE_RETURN_OPERATION:
        event_type = EVENT_RETURN
    else:
        return None
    occurred_at = parse_event_datetime(row.sale_dt) or parse_event_datetime(row.rr_date)
    if not row.clean_cis or not occurred_at:
        return None
    return {
        "source": SOURCE_FINANCE, "external_id": str(row.rrd_id), "clean_cis": row.clean_cis, "raw_kiz": row.kiz,
        "event_type": event_type, "occurred_at": occurred_at, "srid": _text(row.srid, 100),
        "price": _price(row.retail_amount or row.retail_price),
        "article": _text(row.vendor_code or row.nm_id, 255), "name": _text(row.subject_name, 255),
    }


async def collect_finance(db: AsyncSession, seller: Seller) -> CollectResult:
    rows = await db.execute(
        select(
            WbSalesReportRow.rrd_id, WbSalesReportRow.clean_cis, WbSalesReportRow.kiz,
            WbSalesReportRow.seller_oper_name, WbSalesReportRow.sale_dt, WbSalesReportRow.rr_date,
            WbSalesReportRow.srid, WbSalesReportRow.retail_amount, WbSalesReportRow.retail_price,
            WbSalesReportRow.vendor_code, WbSalesReportRow.nm_id, WbSalesReportRow.subject_name,
        ).where(
            WbSalesReportRow.seller_id == seller.id,
            WbSalesReportRow.clean_cis.isnot(None),
            WbSalesReportRow.seller_oper_name.in_([FINANCE_SALE_OPERATION, FINANCE_RETURN_OPERATION]),
        )
    )
    result = CollectResult()
    for row in rows.all():
        result.seen += 1
        draft = finance_row_to_draft(row)
        if draft:
            result.drafts.append(draft)
        else:
            result.skip("no_date_or_kiz")
    return result


# --- Excise (marking) report -----------------------------------------------------------------------------------------

def excise_row_to_draft(row: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
    raw_kiz = str(row.get("excise_short") or "").strip()
    clean_cis = clean_cis_of(raw_kiz)
    if not clean_cis:
        return None
    srid = _text(row.get("srid"), 100)
    fiscal_doc = _text(row.get("fiscal_doc_number"), 64)
    fiscal_dt = str(row.get("fiscal_dt") or "").strip()
    return {
        "source": SOURCE_EXCISE, "external_id": f"{srid or fiscal_doc or 'no-id'}:{clean_cis}"[:255],
        "clean_cis": clean_cis, "raw_kiz": raw_kiz, "event_type": EVENT_SALE,
        "occurred_at": parse_event_datetime(fiscal_dt) or now, "srid": srid,
        "price": _price(row.get("price")), "article": _text(row.get("nm_id"), 255), "name": _text(row.get("name"), 255),
        "receipt_number": fiscal_doc, "fn_number": _text(row.get("fiscal_drive_number"), 64),
        "receipt_date": fiscal_dt[:10] or None,
    }


async def collect_excise(db: AsyncSession, seller: Seller) -> CollectResult:
    rows = await fetch_wb_excise_data(seller=seller, days=EXCISE_DAYS)
    result = CollectResult(seen=len(rows))
    now = datetime.now(timezone.utc)
    for row in rows:
        operation = row.get("operation_type_id")
        if operation != EXCISE_SALE_OPERATION_TYPE:
            # WB documentation does not describe the other values, so they are not treated as returns
            logger.warning(f"[KIZ Events] Unknown excise operation_type_id={operation} for seller {seller.id}")
            result.skip(f"operation_type_{operation}")
            continue
        draft = excise_row_to_draft(row, now)
        if draft:
            result.drafts.append(draft)
        else:
            result.skip("no_kiz")
    return result


# --- Item returns report ---------------------------------------------------------------------------------------------

def item_return_to_draft(row: Dict[str, Any], srid_to_cis: Dict[str, str], order_to_kiz: Dict[int, str]) -> Optional[Dict[str, Any]]:
    """The KIZ comes from the row, its srid or its order. An article alone never identifies a KIZ."""
    srid = _text(row.get("srid"), 100)
    order_id = row.get("orderId") if isinstance(row.get("orderId"), int) and row.get("orderId") > 0 else None
    raw_kiz = str(row.get("kiz") or "").strip() or order_to_kiz.get(order_id or 0)
    clean_cis = clean_cis_of(raw_kiz) or srid_to_cis.get(srid or "")
    occurred_at = (
        parse_event_datetime(row.get("completedDt"))
        or parse_event_datetime(row.get("readyToReturnDt"))
        or parse_event_datetime(row.get("orderDt"))
    )
    if not clean_cis or not occurred_at:
        return None
    return {
        "source": SOURCE_ITEM_RETURNS, "external_id": f"{row.get('shkId') or srid or order_id}:{clean_cis}"[:255],
        "clean_cis": clean_cis, "raw_kiz": raw_kiz or clean_cis, "event_type": EVENT_RETURN, "occurred_at": occurred_at,
        "srid": srid, "order_id": order_id, "article": _text(row.get("nmId"), 255), "name": _text(row.get("subjectName"), 255),
    }


async def _item_return_lookups(db: AsyncSession, seller: Seller, rows: List[Dict[str, Any]]) -> tuple[Dict[str, str], Dict[int, str]]:
    srids = [str(r["srid"]) for r in rows if r.get("srid")]
    order_ids = [r["orderId"] for r in rows if isinstance(r.get("orderId"), int) and r["orderId"] > 0]
    srid_to_cis: Dict[str, str] = {}
    order_to_kiz: Dict[int, str] = {}
    if srids:
        res = await db.execute(
            select(WbSalesReportRow.srid, WbSalesReportRow.clean_cis).where(
                WbSalesReportRow.seller_id == seller.id,
                WbSalesReportRow.srid.in_(srids),
                WbSalesReportRow.clean_cis.isnot(None),
            )
        )
        srid_to_cis = {srid: cis for srid, cis in res.all()}
    if order_ids:
        res = await db.execute(
            select(Order.id, Order.kiz_code).where(
                Order.seller_id == seller.id, Order.id.in_(order_ids), Order.kiz_code.isnot(None),
            )
        )
        order_to_kiz = {oid: code for oid, code in res.all()}
    return srid_to_cis, order_to_kiz


async def collect_item_returns(db: AsyncSession, seller: Seller) -> CollectResult:
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=ITEM_RETURNS_DAYS)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")
    async with WBAnalyticsClient(decrypt(seller.wb_api_token_encrypted)) as client:
        rows = await client.get_item_returns_report(date_from, date_to, "archive")
        rows += await client.get_item_returns_report(date_from, date_to, "active")

    result = CollectResult(seen=len(rows))
    delivered = []
    for row in rows:
        if row.get("returnStatus") == ITEM_RETURN_DELIVERED_STATUS:
            delivered.append(row)
        else:
            result.skip("not_delivered_yet")

    srid_to_cis, order_to_kiz = await _item_return_lookups(db, seller, delivered)
    for row in delivered:
        draft = item_return_to_draft(row, srid_to_cis, order_to_kiz)
        if draft:
            result.drafts.append(draft)
        else:
            result.skip("unmatched")
    return result


# --- FBS orders ------------------------------------------------------------------------------------------------------

def order_to_drafts(order: Any, now: datetime) -> List[Dict[str, Any]]:
    clean_cis = clean_cis_of(order.kiz_code)
    if not clean_cis:
        return []
    base_date = parse_event_datetime(order.wb_created_at) or parse_event_datetime(order.created_at) or now

    def draft(event_type: str, occurred_at: datetime) -> Dict[str, Any]:
        return {
            "source": SOURCE_ORDERS, "external_id": f"{order.id}:{clean_cis}:{event_type}"[:255],
            "clean_cis": clean_cis, "raw_kiz": order.kiz_code, "event_type": event_type, "occurred_at": occurred_at,
            "order_id": order.id, "price": _price(order.price), "article": _text(order.article, 255), "name": _text(order.name, 255),
        }

    withdrawn = order.kiz_status == KizStatus.WITHDRAWN or bool(order.cz_withdrawal_doc_id)
    cancelled = (
        order.status == OrderStatus.CANCELLED
        or order.kiz_status == KizStatus.RETURNED
        or order.wb_status in WB_CANCELLED_STATUSES
    )
    if cancelled:
        # WB does not report when an order was cancelled. A second after the order date keeps the return
        # after its own sale, and any later real sale of the same KIZ still wins
        drafts = [draft(EVENT_RETURN, base_date + timedelta(seconds=1))]
        if withdrawn:
            drafts.append(draft(EVENT_SALE, base_date))
        return drafts
    if order.status == OrderStatus.DELIVERED or withdrawn or order.wb_status == "sold":
        return [draft(EVENT_SALE, base_date)]
    return []


async def collect_orders(db: AsyncSession, seller: Seller) -> CollectResult:
    rows = await db.execute(
        select(
            Order.id, Order.kiz_code, Order.status, Order.kiz_status, Order.wb_status, Order.cz_withdrawal_doc_id,
            Order.wb_created_at, Order.created_at, Order.price, Order.article, Order.name,
        ).where(Order.seller_id == seller.id, Order.kiz_code.isnot(None))
    )
    result = CollectResult()
    now = datetime.now(timezone.utc)
    for row in rows.all():
        result.seen += 1
        result.drafts.extend(order_to_drafts(row, now))
    return result


async def refresh_delivered_order_statuses(db: AsyncSession, seller: Seller) -> CollectResult:
    """A cancellation after delivery is otherwise invisible: the poller only follows orders in progress."""
    from app.services.wb_archive_service import resolve_order_status
    from app.services.wb_client import WBClient

    since = datetime.now(timezone.utc) - timedelta(days=ORDER_STATUS_DAYS)
    rows = await db.execute(
        select(Order).where(
            Order.seller_id == seller.id,
            Order.kiz_code.isnot(None),
            Order.status == OrderStatus.DELIVERED,
            Order.wb_created_at >= since,
        )
    )
    orders = {order.id: order for order in rows.scalars().all()}
    result = CollectResult(seen=len(orders))
    ids = list(orders)
    async with WBClient(decrypt(seller.wb_api_token_encrypted)) as client:
        for start in range(0, len(ids), ORDER_STATUS_CHUNK):
            for status in await client.get_orders_status(ids[start:start + ORDER_STATUS_CHUNK]):
                order = orders.get(status.get("id"))
                # A withdrawn order stays as it is: the KIZ may have been resold, finance reports show real returns
                if not order or order.kiz_status == KizStatus.WITHDRAWN or order.cz_withdrawal_doc_id:
                    continue
                wb_status = str(status.get("wbStatus") or "").strip()
                new_status = resolve_order_status(wb_status, status.get("supplierStatus"), order.status)
                if wb_status and (new_status != order.status or wb_status != order.wb_status):
                    order.status, order.wb_status = new_status, wb_status
                    result.changed += 1
    return result


# --- Archive file ----------------------------------------------------------------------------------------------------

def archive_analysis_to_drafts(analysis: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
    drafts = []
    for items, event_type in ((analysis.get("withdrawals", []), EVENT_SALE), (analysis.get("returns", []), EVENT_RETURN)):
        for item in items:
            clean_cis = clean_cis_of(item.get("kiz_code"))
            if not clean_cis:
                continue
            order_key = item.get("order_id") or item.get("sticker_id") or "no-order"
            receipt_date = _text(item.get("receipt_date"), 32)
            drafts.append({
                "source": SOURCE_ARCHIVE_FILE, "external_id": f"{order_key}:{clean_cis}:{event_type}"[:255],
                "clean_cis": clean_cis, "raw_kiz": item.get("kiz_code"), "event_type": event_type,
                "occurred_at": parse_event_datetime(receipt_date) or now, "order_id": item.get("order_id"),
                "price": _price(item.get("price")), "article": _text(item.get("article"), 255), "name": _text(item.get("name"), 255),
                "receipt_number": _text(item.get("receipt_number"), 64), "fn_number": _text(item.get("fn_number"), 64),
                "receipt_date": receipt_date,
            })
    return drafts


async def record_archive_file_events(db: AsyncSession, seller_id: str, analysis: Dict[str, Any]) -> Optional[Dict[str, int]]:
    """Saves events from an uploaded archive. A journal failure must not break the upload itself."""
    try:
        async with db.begin_nested():
            return await save_events(db, str(seller_id), archive_analysis_to_drafts(analysis, datetime.now(timezone.utc)))
    except Exception as exc:
        logger.error(f"[KIZ Events] Could not record archive events for seller {seller_id}: {exc}")
        return None
