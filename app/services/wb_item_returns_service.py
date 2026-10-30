"""
WB Item Returns Service - WB FBS Manager.
Client returns processing for Wildberries Seller Analytics API (item-returns).
3-tier correlation (srid -> orderId -> nmId) and True API status bifurcation.
Strict limit: < 300 lines (PROJECT_RULES.md).
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.kiz import BatchStatus, KizProductInfo, KizSignatureBatch
from app.models.order import KizStatus, Order, OrderStatus
from app.models.seller import Seller
from app.models.wb_finance import WbSalesReportRow
from app.services.encryption import decrypt
from app.services.kiz_service import batch_verify_and_sync_cises, is_kiz_withdrawn, parse_kiz_code
from app.services.wb_analytics_client import WBAnalyticsClient

logger = logging.getLogger(__name__)

_seller_sync_locks: Dict[str, asyncio.Lock] = {}
_seller_sync_in_progress: Dict[str, asyncio.Future] = {}


def _get_seller_lock(seller_id: str) -> asyncio.Lock:
    lock = _seller_sync_locks.get(seller_id)
    if not lock:
        _seller_sync_locks[seller_id] = lock = asyncio.Lock()
    return lock


async def fetch_wb_item_returns_data(seller: Seller, days: int = 14) -> List[Dict[str, Any]]:
    """Fetch client return records from WB Seller Analytics API."""
    if not seller.wb_api_token_encrypted:
        logger.warning(f"[Item Returns] Seller {seller.id} has no WB token")
        return []
    try:
        wb_token = decrypt(seller.wb_api_token_encrypted)
    except Exception as e:
        logger.error(f"[Item Returns] Failed to decrypt WB token for seller {seller.id}: {e}")
        return []

    now_utc = datetime.now(timezone.utc)
    date_to, date_from = now_utc.strftime("%Y-%m-%d"), (now_utc - timedelta(days=days)).strftime("%Y-%m-%d")
    async with WBAnalyticsClient(wb_token) as client:
        return await client.get_item_returns(date_from=date_from, date_to=date_to)


async def get_already_queued_return_cises(seller_id: str, db: AsyncSession) -> Set[str]:
    """Return set of KIZ codes currently queued in pending signature batches."""
    stmt = select(KizSignatureBatch).where(KizSignatureBatch.seller_id == seller_id, KizSignatureBatch.status.in_([BatchStatus.PENDING_SIGNATURE, BatchStatus.PROCESSING]))
    res = await db.execute(stmt)
    queued: Set[str] = set()
    for b in res.scalars().all():
        for r in (b.data_payload or {}).get("returns", []):
            code = r.get("kiz_code") or r.get("clean_cis") or r.get("ki")
            if code:
                queued.add(code.strip())
    return queued


def _extract_owner_inn(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    if not payload or not isinstance(payload, dict):
        return None
    c_info = payload.get("cisInfo") if isinstance(payload.get("cisInfo"), dict) else payload
    inn = c_info.get("ownerInn") or c_info.get("owner_inn") or payload.get("ownerInn") or payload.get("owner_inn")
    return str(inn).strip() if inn else None


def _build_return_item(record: Dict[str, Any], clean_cis: str, order: Optional[Order], today: str) -> Dict[str, Any]:
    """Construct return item dictionary for KizSignatureBatch payload."""
    price = float(record.get("price") or (order.price if order and order.price else 0.0))
    srid = str(record.get("srid") or "").strip()
    nm_id = str(record.get("nmId") or record.get("nm_id") or (order.nm_id if order and order.nm_id else ""))
    reason = str(record.get("returnReason") or "Возврат покупателя").strip()
    rec_num = str(record.get("orderId") or srid or (order.id if order else "1"))
    return {
        "order_id": order.id if order else None, "sticker_id": order.sticker_id if order else None,
        "kiz_code": clean_cis, "ki": clean_cis, "clean_cis": clean_cis,
        "receipt_number": rec_num, "primary_document_number": f"RET-{rec_num}",
        "primary_document_type": "RECEIPT", "primary_document_date": today, "document_type": "OTHER",
        "primary_document_custom_name": f"Возврат покупателя WB (SRID: {srid or nm_id})",
        "price": price, "price_kopecks": int(round(price * 100)),
        "article": order.article if order and order.article else nm_id,
        "name": order.name if order and order.name else f"Возврат товара WB (Арт. {nm_id})",
        "task_status": f"Клиентский возврат WB ({reason})", "srid": srid, "action": "RETURN", "doc_type": "LP_RETURN",
        "db_status": order.status.value if order else "RETURNED",
        "db_kiz_status": order.kiz_status.value if order else "WITHDRAWN",
        "cz_status": "RETIRED", "cz_status_desc": "Выведен из оборота (требует возврата LP_RETURN)",
        "is_already_in_circulation": False, "needs_cz_return": True, "is_seller_owner": True, "selected": True,
    }


async def process_item_returns_for_seller(seller: Seller, db: AsyncSession, days: int = 14) -> Dict[str, Any]:
    """Core returns engine: fetch, 3-tier match, True API bifurcate, batch and audit."""
    logger.info(f"[Item Returns] Running for seller {seller.id} ({seller.name}), days={days}")
    returns_rows = await fetch_wb_item_returns_data(seller, days=days)
    if not returns_rows:
        return {
            "success": True, "seller_id": seller.id, "created": False, "batch_id": None,
            "batches_created": 0, "processed_count": 0, "returns_count": 0, "introduced_count": 0,
            "unmatched_count": 0, "foreign_owner_count": 0, "already_queued_count": 0,
            "message": "Нет записей возвратов WB за указанный период",
        }

    srids = [str(r.get("srid") or "").strip() for r in returns_rows if r.get("srid")]
    order_ids = [int(r["orderId"]) for r in returns_rows if r.get("orderId") and str(r["orderId"]).isdigit()]

    srid_to_cis: Dict[str, str] = {}
    if srids:
        res_fin = await db.execute(select(WbSalesReportRow).where(WbSalesReportRow.seller_id == seller.id, WbSalesReportRow.srid.in_(srids), WbSalesReportRow.clean_cis.isnot(None)))
        srid_to_cis = {f.srid: f.clean_cis for f in res_fin.scalars().all() if f.srid and f.clean_cis}

    stmt_ord = select(Order).where(Order.seller_id == seller.id, or_(Order.id.in_(order_ids) if order_ids else False, Order.kiz_code.isnot(None)))
    orders_list = (await db.execute(stmt_ord)).scalars().all()
    orders_by_id: Dict[int, Order] = {o.id: o for o in orders_list}
    orders_by_cis: Dict[str, Order] = {}
    orders_by_nm: Dict[int, List[Order]] = {}
    for o in orders_list:
        if o.kiz_code:
            clean = parse_kiz_code(o.kiz_code).get("clean_cis") or o.kiz_code
            orders_by_cis[clean] = o
            orders_by_cis[o.kiz_code] = o
        if o.nm_id:
            orders_by_nm.setdefault(o.nm_id, []).append(o)

    record_correlations: List[Tuple[Dict[str, Any], str, Optional[Order]]] = []
    unique_cises: Set[str] = set()
    unmatched_count = 0

    for r in returns_rows:
        srid = str(r.get("srid") or "").strip()
        oid = int(r["orderId"]) if r.get("orderId") and str(r["orderId"]).isdigit() else None
        nm_id = int(r["nmId"]) if r.get("nmId") and str(r["nmId"]).isdigit() else None
        resolved_cis: Optional[str] = None
        matched_order: Optional[Order] = None

        if srid and srid in srid_to_cis:
            resolved_cis = srid_to_cis[srid]
            matched_order = orders_by_cis.get(resolved_cis)
        if not resolved_cis and oid and oid in orders_by_id:
            matched_order = orders_by_id[oid]
            if matched_order.kiz_code:
                resolved_cis = parse_kiz_code(matched_order.kiz_code).get("clean_cis") or matched_order.kiz_code
        if not resolved_cis and nm_id and nm_id in orders_by_nm:
            candidates = [o for o in orders_by_nm[nm_id] if o.kiz_code and o.kiz_status != KizStatus.RETURNED]
            if candidates:
                matched_order = candidates[0]
                resolved_cis = parse_kiz_code(matched_order.kiz_code).get("clean_cis") or matched_order.kiz_code
        if not resolved_cis and (r.get("kiz") or r.get("kiz_code") or r.get("cis") or r.get("excise_short")):
            raw_k = str(r.get("kiz") or r.get("kiz_code") or r.get("cis") or r.get("excise_short"))
            resolved_cis = parse_kiz_code(raw_k).get("clean_cis") or raw_k
            matched_order = orders_by_cis.get(resolved_cis)

        if resolved_cis:
            unique_cises.add(resolved_cis)
            record_correlations.append((r, resolved_cis, matched_order))
        else:
            unmatched_count += 1

    verified_map: Dict[str, Optional[KizProductInfo]] = {}
    if unique_cises:
        try:
            verified_map = await batch_verify_and_sync_cises(seller=seller, kiz_codes=list(unique_cises), db=db, force_refresh=True)
        except Exception as e:
            logger.error(f"[Item Returns] True API check failed: {e}")

    already_queued = await get_already_queued_return_cises(seller.id, db)
    seller_inn = str(seller.cz_inn or "").strip()
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    batch_returns: List[Dict[str, Any]] = []
    introduced_count, foreign_owner_count, already_queued_count = 0, 0, 0

    for r, clean_cis, matched_order in record_correlations:
        kinfo = verified_map.get(clean_cis)
        cz_st = kinfo.cz_status if kinfo else None
        cz_ex = kinfo.cz_status_ex if kinfo else None
        raw_pl = kinfo.raw_cz_payload if kinfo else {}
        withdrawn, _ = is_kiz_withdrawn(status=cz_st, status_ex=cz_ex, raw_payload=raw_pl)

        if withdrawn:
            owner_inn = _extract_owner_inn(raw_pl) or (kinfo.cz_owner_inn if kinfo else None)
            if owner_inn and seller_inn and owner_inn != seller_inn:
                foreign_owner_count += 1
                logger.info(f"[Item Returns] KIZ {clean_cis} belongs to {owner_inn} != {seller_inn}. Skipping.")
                continue
            if clean_cis in already_queued:
                already_queued_count += 1
                continue
            batch_returns.append(_build_return_item(r, clean_cis, matched_order, today_str))
        elif kinfo is not None and cz_st:
            introduced_count += 1
            if matched_order:
                matched_order.cz_withdrawal_doc_id = None
                matched_order.kiz_status, matched_order.kiz_cz_status = KizStatus.RETURNED, cz_st
                matched_order.kiz_cz_status_updated_at = matched_order.updated_at = now_utc
                if matched_order.status != OrderStatus.DELIVERED:
                    matched_order.status = OrderStatus.CANCELLED
            if kinfo:
                kinfo.cz_status, kinfo.is_valid, kinfo.checked_at = "INTRODUCED", True, now_utc
                kinfo.validation_message = "Товар в обороте (отказ до выкупа)"
        else:
            logger.warning(f"[Item Returns] KIZ {clean_cis} unverified in True API. Skipping.")

    await db.commit()

    batch_id: Optional[str] = None
    if batch_returns:
        timestamp_tag = now_utc.strftime("%Y%m%d_%H%M%S")
        summary = {
            "total_rows": len(batch_returns), "sales_count": 0, "returns_count": len(batch_returns),
            "returns_needing_cz_return": len(batch_returns), "introduced_count": introduced_count,
            "source_type": "wb_item_returns", "description": "Клиентские возвраты Wildberries (item-returns)",
        }
        batch = KizSignatureBatch(
            seller_id=seller.id, filename=f"wb_returns_{timestamp_tag}.json", source="wb_item_returns",
            status=BatchStatus.PENDING_SIGNATURE, sales_count=0, returns_count=len(batch_returns),
            already_withdrawn_count=0, total_count=len(batch_returns),
            data_payload={"summary": summary, "withdrawals": [], "returns": batch_returns},
        )
        db.add(batch)
        await db.flush()
        batch_id = batch.id

    audit = AuditLog(
        seller_id=seller.id, agent="wb_item_returns",
        action="ITEM_RETURNS_BATCH_CREATED" if batch_id else "ITEM_RETURNS_SYNC",
        entity_type="kiz_signature_batch" if batch_id else "seller", entity_id=batch_id or seller.id,
        payload={
            "total_wb_records": len(returns_rows), "returns_needing_cz": len(batch_returns),
            "introduced_count": introduced_count, "unmatched_count": unmatched_count,
            "foreign_owner_count": foreign_owner_count, "already_queued_count": already_queued_count,
        },
    )
    db.add(audit)
    await db.commit()

    if batch_id and seller.telegram_bot_token_encrypted:
        try:
            from app.services.telegram_service import TelegramService, get_personal_manager_chats
            manager_chats = get_personal_manager_chats(seller)
            if manager_chats:
                tg = TelegramService(decrypt(seller.telegram_bot_token_encrypted))
                await tg.send_auto_kiz_batch_notification(
                    chat_ids=[manager_chats[0]], seller_name=seller.name, sales_count=0,
                    returns_count=len(batch_returns), total_sum_rub=0.0, batch_id=batch_id, is_auto_signed=False,
                )
                await tg.close()
        except Exception as tg_err:
            logger.warning(f"[Item Returns] Failed to send Telegram alert: {tg_err}")

    return {
        "success": True, "seller_id": seller.id, "created": bool(batch_id), "batch_id": batch_id,
        "batches_created": 1 if batch_id else 0, "processed_count": len(returns_rows),
        "returns_count": len(batch_returns), "introduced_count": introduced_count,
        "unmatched_count": unmatched_count, "foreign_owner_count": foreign_owner_count,
        "already_queued_count": already_queued_count,
        "message": f"Processed {len(returns_rows)} returns: {len(batch_returns)} LP_RETURN, {introduced_count} returned in circulation",
    }


async def sync_seller_returns(db: AsyncSession, seller_id: str, days: int = 14, **kwargs: Any) -> Dict[str, Any]:
    """Top-level entrypoint for syncing seller item returns with concurrency serialization."""
    if seller_id in _seller_sync_in_progress:
        return dict(await _seller_sync_in_progress[seller_id])

    fut = asyncio.get_running_loop().create_future()
    _seller_sync_in_progress[seller_id] = fut
    try:
        async with _get_seller_lock(seller_id):
            seller = (await db.execute(select(Seller).where(Seller.id == seller_id))).scalar_one_or_none()
            if not seller:
                res = {"success": False, "error": f"Seller {seller_id} not found", "processed_count": 0, "batches_created": 0}
            elif not seller.is_active:
                res = {"success": True, "skipped": True, "processed_count": 0, "batches_created": 0, "message": "Seller inactive"}
            else:
                res = await process_item_returns_for_seller(seller=seller, db=db, days=days)
            if not fut.done():
                fut.set_result(res)
            return res
    except Exception as e:
        if not fut.done():
            fut.set_exception(e)
            fut.exception()  # Mark as retrieved: usually nobody else awaits this future
        raise
    finally:
        _seller_sync_in_progress.pop(seller_id, None)
