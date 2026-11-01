"""
Unified KIZ Sales Report — WB sales report events for unified_kiz_batch_service.
The report shows sales and returns before the financial report does. It has no KIZ,
so rows are linked by srid: to known KIZ events and to the rid of FBS orders.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kiz_event import KizEvent
from app.models.order import Order
from app.models.seller import Seller
from app.services.encryption import decrypt
from app.services.kiz_service import parse_kiz_code
from app.services.wb_client import WBClient
from app.services.wb_statistics_client import WBStatisticsClient

logger = logging.getLogger(__name__)

SALES_REPORT_SOURCE = "wb_sales_report"
MOSCOW_TZ = timezone(timedelta(hours=3))
RID_WINDOW_DAYS = 30
RID_WINDOWS = 2
SRID_CHUNK = 500


@dataclass
class SalesReportResult:
    rows: int = 0
    events: int = 0
    failed: bool = False


async def fetch_wb_sales_rows(seller: Seller, days: int) -> List[Dict[str, Any]]:
    date_from = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    return await WBStatisticsClient(decrypt(seller.wb_api_token_encrypted)).get_sales(date_from)


async def fetch_wb_order_rids(seller: Seller) -> Dict[int, str]:
    rids: Dict[int, str] = {}
    window_end = datetime.now(timezone.utc)
    async with WBClient(decrypt(seller.wb_api_token_encrypted)) as client:
        for _ in range(RID_WINDOWS):
            window_start = window_end - timedelta(days=RID_WINDOW_DAYS)
            rids.update(await client.get_order_rids(window_start, window_end))
            window_end = window_start
    return rids


async def _backfill_order_rids(seller: Seller, orders: Iterable[Order]) -> None:
    """Заполняет rid заказов, созданных до появления колонки."""
    missing = [o for o in orders if not o.rid]
    if not missing:
        return
    rids = await fetch_wb_order_rids(seller)
    for order in missing:
        if order.id in rids:
            order.rid = rids[order.id]


async def _load_event_srids(db: AsyncSession, seller_id: str, srids: List[str]) -> Dict[str, str]:
    srid_to_cis: Dict[str, str] = {}
    for i in range(0, len(srids), SRID_CHUNK):
        stmt = select(KizEvent.srid, KizEvent.clean_cis).where(
            KizEvent.seller_id == seller_id,
            KizEvent.srid.in_(srids[i:i + SRID_CHUNK]),
        )
        for srid, cis in (await db.execute(stmt)).all():
            srid_to_cis[srid] = cis
    return srid_to_cis


def _order_cis(order: Order) -> str:
    return parse_kiz_code(order.kiz_code).get("clean_cis") or order.kiz_code


def _unlinked_returns(rows: List[Dict[str, Any]], linked: Set[str]) -> bool:
    return any(str(r.get("saleID")).startswith("R") and r["srid"] not in linked for r in rows)


def _row_event(row: Dict[str, Any], order: Any) -> Dict[str, Any]:
    occurred_at = datetime.fromisoformat(str(row["date"])[:19]).replace(tzinfo=MOSCOW_TZ)
    return {
        "source": SALES_REPORT_SOURCE,
        "type": "RETURN" if str(row.get("saleID")).startswith("R") else "SALE",
        "date": occurred_at,
        "srid": row["srid"],
        "price": abs(float(row.get("finishedPrice") or 0.0)),
        "article": str(row.get("nmId") or ""),
        "name": row.get("subject") or "",
        "raw_kiz": order.kiz_code if order else None,
        "order": order,
    }


async def add_sales_report_events(
    seller: Seller,
    db: AsyncSession,
    history_by_cis: Dict[str, List[Dict[str, Any]]],
    orders: List[Order],
    days: int,
) -> SalesReportResult:
    """Добавляет в историю КИЗ продажи и возвраты из отчета продаж WB."""
    if not seller.wb_api_token_encrypted:
        return SalesReportResult()
    try:
        raw_rows = await fetch_wb_sales_rows(seller, days)
    except Exception as e:
        logger.warning(f"Could not load WB sales report for seller {seller.id}: {e}")
        return SalesReportResult(failed=True)
    rows = [r for r in raw_rows if r.get("srid") and r.get("date") and str(r.get("saleID"))[:1] in ("S", "R")]
    if not rows:
        return SalesReportResult(rows=len(raw_rows))

    srid_to_cis = {e["srid"]: cis for cis, events in history_by_cis.items() for e in events if e.get("srid")}
    srid_to_cis.update(await _load_event_srids(db, seller.id, sorted({r["srid"] for r in rows})))
    orders_by_rid = {o.rid: o for o in orders if o.rid}

    if _unlinked_returns(rows, set(srid_to_cis) | set(orders_by_rid)):
        try:
            await _backfill_order_rids(seller, orders)
            await db.flush()
            orders_by_rid = {o.rid: o for o in orders if o.rid}
        except Exception as e:
            logger.warning(f"Could not backfill order rid for seller {seller.id}: {e}")

    result = SalesReportResult(rows=len(raw_rows))
    for row in rows:
        order = orders_by_rid.get(row["srid"])
        cis = srid_to_cis.get(row["srid"]) or (_order_cis(order) if order else None)
        if not cis:
            continue
        linked_order = order if order and _order_cis(order) == cis else None
        try:
            event = _row_event(row, linked_order)
        except (TypeError, ValueError) as e:
            logger.warning(f"Skipping WB sales report row {row.get('saleID')} for seller {seller.id}: {e}")
            continue
        history_by_cis.setdefault(cis, []).append(event)
        result.events += 1
    return result
