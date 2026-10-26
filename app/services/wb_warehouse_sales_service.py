"""
WB Warehouse Sales Service — WB FBS Manager
Отслеживание повторных продаж маркированных товаров со склада WB (FBO / остатки после возвратов FBS).
Сверка с True API ГИС МТ и формирование очереди пакетов ЭЦП на вывод (LK_RECEIPT) и возврат (LP_RETURN).
Strict line limit: < 330 lines (PROJECT_RULES.md).
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.seller import Seller
from app.models.order import Order, KizStatus, OrderStatus
from app.models.kiz import KizSignatureBatch, BatchStatus
from app.models.wb_finance import WbSalesReportRow
from app.models.audit import AuditLog
from app.services.encryption import decrypt
from app.services.wb_analytics_client import WBAnalyticsClient
from app.services.kiz_service import (
    batch_verify_and_sync_cises,
    is_kiz_withdrawn,
    parse_kiz_code,
)
from app.services.telegram_service import TelegramService
from app.services.wb_warehouse_sales_builder import (
    build_warehouse_return_item,
    build_warehouse_sale_item,
    extract_owner_inn,
)

logger = logging.getLogger(__name__)


async def fetch_wb_excise_data(
    seller: Seller,
    days: int = 14,
) -> List[Dict[str, Any]]:
    """Получает данные отчета по маркированным товарам из WB Analytics API."""
    if not seller.wb_api_token_encrypted:
        logger.warning(f"[Warehouse Sales] Seller {seller.id} has no WB token")
        return []

    wb_token = decrypt(seller.wb_api_token_encrypted)
    now_utc = datetime.now(timezone.utc)
    date_to = now_utc.strftime("%Y-%m-%d")
    date_from = (now_utc - timedelta(days=days)).strftime("%Y-%m-%d")

    async with WBAnalyticsClient(wb_token) as client:
        return await client.get_excise_report(date_from=date_from, date_to=date_to)


async def get_already_queued_cises(seller_id: str, db: AsyncSession) -> Set[str]:
    """Возвращает множество КИЗ, уже находящихся в очереди неподписанных пакетов."""
    stmt = select(KizSignatureBatch).where(
        KizSignatureBatch.seller_id == seller_id,
        KizSignatureBatch.status == BatchStatus.PENDING_SIGNATURE,
    )
    res = await db.execute(stmt)
    batches = res.scalars().all()

    queued: Set[str] = set()
    for b in batches:
        payload = b.data_payload or {}
        for w in payload.get("withdrawals", []):
            code = w.get("kiz_code") or w.get("clean_cis") or w.get("ki")
            if code:
                queued.add(code.strip())
        for r in payload.get("returns", []):
            code = r.get("kiz_code") or r.get("clean_cis") or r.get("ki")
            if code:
                queued.add(code.strip())
    return queued


async def process_warehouse_sales_for_seller(
    seller: Seller,
    db: AsyncSession,
    days: int = 30,
) -> Dict[str, Any]:
    """
    Основной метод обработки продаж и возвратов со склада WB:
    1. Загружает операции из WB excise-report.
    2. Сопоставляет КИЗ с локальной базой и запрашивает True API ГИС МТ.
    3. Для RETIRED продаж — обновляет статус в БД (без лишних документов).
    4. Для RETIRED возвратов/сторно — добавляет в batch.returns (LP_RETURN).
    5. Для INTRODUCED продаж — формирует batch.withdrawals (LK_RECEIPT).
    """
    logger.info(f"[Warehouse Sales] Running for seller {seller.id} ({seller.name}), days={days}")
    excise_rows = await fetch_wb_excise_data(seller, days=days)

    if not excise_rows:
        return {
            "success": True, "seller_id": seller.id, "created": False,
            "message": "Нет записей в отчете маркировки WB за указанный период",
            "total_wb_records": 0, "needs_withdrawal_count": 0,
            "already_retired_count": 0, "returns_count": 0, "sales_count": 0,
        }

    kiz_to_record: Dict[str, Dict[str, Any]] = {}
    for r in excise_rows:
        raw_cis = str(r.get("excise_short") or "").strip()
        if raw_cis:
            parsed = parse_kiz_code(raw_cis)
            clean = parsed.get("clean_cis") if (parsed.get("crypto_tail") or parsed.get("crypto_key")) else raw_cis
            kiz_to_record[clean] = r

    unique_cises = list(kiz_to_record.keys())
    logger.info(f"[Warehouse Sales] Found {len(unique_cises)} unique KIZ codes in WB report")

    stmt_orders = select(Order).where(Order.seller_id == seller.id, Order.kiz_code.isnot(None))
    res_orders = await db.execute(stmt_orders)
    orders_map: Dict[str, Order] = {}
    for ord_obj in res_orders.scalars().all():
        if ord_obj.kiz_code:
            p = parse_kiz_code(ord_obj.kiz_code)
            orders_map[ord_obj.kiz_code] = ord_obj
            if p.get("clean_cis"):
                orders_map[p["clean_cis"]] = ord_obj

    returned_cises: Set[str] = set()
    returned_srids: Set[str] = set()
    excise_srids = [str(r.get("srid") or "").strip() for r in excise_rows if r.get("srid")]

    if unique_cises or excise_srids:
        conds = []
        if unique_cises:
            conds.append(WbSalesReportRow.clean_cis.in_(unique_cises))
        if excise_srids:
            conds.append(WbSalesReportRow.srid.in_(excise_srids))

        stmt_fin = select(WbSalesReportRow).where(WbSalesReportRow.seller_id == seller.id, or_(*conds))
        res_fin = await db.execute(stmt_fin)
        for f_row in res_fin.scalars().all():
            doc_name = (f_row.doc_type_name or f_row.seller_oper_name or "").lower()
            amt = float(f_row.retail_amount or 0.0)
            if "возврат" in doc_name or "отказ" in doc_name or "отмен" in doc_name or amt < 0:
                if f_row.clean_cis:
                    returned_cises.add(f_row.clean_cis)
                if f_row.srid:
                    returned_srids.add(f_row.srid)

    verified_map = await batch_verify_and_sync_cises(seller=seller, kiz_codes=unique_cises, db=db, force_refresh=True)
    already_queued = await get_already_queued_cises(seller.id, db)
    seller_inn = str(seller.cz_inn or "").strip()

    now = datetime.now(timezone.utc)
    batch_withdrawals: List[Dict[str, Any]] = []
    batch_returns: List[Dict[str, Any]] = []
    already_retired_count = 0
    other_owner_count = 0
    total_sales_sum = 0.0
    total_returns_sum = 0.0

    for clean_cis, record in kiz_to_record.items():
        r_srid = str(record.get("srid") or "").strip()
        rec_doc = str(record.get("doc_type") or record.get("operation") or record.get("operation_type") or "").lower()
        rec_price = float(record.get("price") or 0.0)
        is_return_record = bool(
            (r_srid and r_srid in returned_srids)
            or (clean_cis in returned_cises)
            or any(w in rec_doc for w in ("возврат", "сторно", "отказ", "отмен"))
            or rec_price < 0
        )

        p_cis = parse_kiz_code(clean_cis).get("clean_cis")
        kinfo = verified_map.get(clean_cis) or (verified_map.get(p_cis) if p_cis else None)
        cz_st = kinfo.cz_status if kinfo else None
        cz_ex = kinfo.cz_status_ex if kinfo else None
        raw_pl = kinfo.raw_cz_payload if kinfo else {}
        matched_order = orders_map.get(clean_cis) or (orders_map.get(p_cis) if p_cis else None)

        owner_inn = extract_owner_inn(raw_pl)
        if owner_inn and seller_inn and owner_inn != seller_inn:
            other_owner_count += 1
            logger.info(f"[Warehouse Sales] KIZ {clean_cis} owner {owner_inn} != seller {seller_inn}. Skipping.")
            continue

        withdrawn, _ = is_kiz_withdrawn(status=cz_st, status_ex=cz_ex, raw_payload=raw_pl)

        if is_return_record:
            if cz_st == "INTRODUCED" or not withdrawn:
                logger.debug(f"[Warehouse Sales] Return KIZ {clean_cis} already INTRODUCED in True API. Skipping return batch.")
                if matched_order:
                    matched_order.cz_withdrawal_doc_id = None
                    matched_order.kiz_status = KizStatus.RETURNED
                    matched_order.kiz_cz_status = cz_st or "INTRODUCED"
                    matched_order.updated_at = now
                    if matched_order.status != OrderStatus.DELIVERED:
                        matched_order.status = OrderStatus.CANCELLED
                continue
            if clean_cis in already_queued:
                continue
            ret_item = build_warehouse_return_item(record, clean_cis, matched_order)
            batch_returns.append(ret_item)
            total_returns_sum += ret_item["price"]
            continue

        if withdrawn:
            already_retired_count += 1
            if matched_order and matched_order.kiz_status != KizStatus.WITHDRAWN:
                matched_order.kiz_status = KizStatus.WITHDRAWN
                matched_order.kiz_cz_status = cz_st or "RETIRED"
                matched_order.updated_at = now
            continue

        if clean_cis in already_queued:
            logger.debug(f"[Warehouse Sales] KIZ {clean_cis} already in pending signature batch")
            continue

        item_data = build_warehouse_sale_item(record, clean_cis, matched_order)
        batch_withdrawals.append(item_data)
        total_sales_sum += item_data["price"]

    await db.commit()

    if not batch_withdrawals and not batch_returns:
        details = []
        if already_retired_count:
            details.append(f"{already_retired_count} уже выведены из оборота")
        if other_owner_count:
            details.append(f"{other_owner_count} на балансе Wildberries (ООО «РВБ»)")
        msg_details = ", ".join(details) if details else "нет товаров на балансе ИП"
        return {
            "success": True, "seller_id": seller.id, "created": False,
            "message": f"Сверка завершена: {msg_details}",
            "total_wb_records": len(excise_rows), "needs_withdrawal_count": 0,
            "already_retired_count": already_retired_count, "other_owner_count": other_owner_count,
            "returns_count": 0, "sales_count": 0,
        }

    timestamp_tag = now.strftime("%Y%m%d_%H%M%S")
    summary = {
        "total_rows": len(batch_withdrawals) + len(batch_returns),
        "sales_count": len(batch_withdrawals),
        "sales_needing_withdrawal": len(batch_withdrawals),
        "sales_already_withdrawn": already_retired_count,
        "other_owner_count": other_owner_count,
        "returns_count": len(batch_returns),
        "returns_needing_cz_return": len(batch_returns),
        "total_sales_sum": total_sales_sum,
        "total_returns_sum": total_returns_sum,
        "source_type": "wb_warehouse_sale",
        "description": "Повторные продажи и возвраты со склада Wildberries (FBO)",
    }

    batch = KizSignatureBatch(
        seller_id=seller.id,
        filename=f"wb_warehouse_{timestamp_tag}.json",
        source="wb_warehouse_sale",
        status=BatchStatus.PENDING_SIGNATURE,
        sales_count=len(batch_withdrawals),
        returns_count=len(batch_returns),
        already_withdrawn_count=already_retired_count,
        total_count=len(batch_withdrawals) + len(batch_returns),
        data_payload={"summary": summary, "withdrawals": batch_withdrawals, "returns": batch_returns},
    )
    db.add(batch)
    await db.flush()

    audit = AuditLog(
        seller_id=seller.id,
        agent="wb_warehouse_sales",
        action="WAREHOUSE_SALES_BATCH_CREATED",
        entity_type="kiz_signature_batch",
        entity_id=batch.id,
        payload={
            "sales_count": len(batch_withdrawals),
            "returns_count": len(batch_returns),
            "already_retired_count": already_retired_count,
            "other_owner_count": other_owner_count,
            "total_sales_sum": total_sales_sum,
        },
    )
    db.add(audit)
    await db.commit()

    await _send_telegram_notification(seller, len(batch_withdrawals), len(batch_returns), total_sales_sum, batch.id)

    return {
        "success": True, "seller_id": seller.id, "created": True, "batch_id": batch.id,
        "sales_count": len(batch_withdrawals), "returns_count": len(batch_returns),
        "already_retired_count": already_retired_count, "other_owner_count": other_owner_count,
        "total_sales_sum": total_sales_sum, "status": batch.status.value,
        "message": f"Сформирован пакет на вывод {len(batch_withdrawals)} и возврат {len(batch_returns)} товаров со склада WB",
    }


async def _send_telegram_notification(
    seller: Seller,
    sales_count: int,
    returns_count: int,
    total_sum: float,
    batch_id: str,
) -> None:
    """Отправляет уведомление менеджеру в Telegram."""
    if not seller.telegram_bot_token_encrypted:
        return
    try:
        tg_token = decrypt(seller.telegram_bot_token_encrypted)
        target_chats = []
        if getattr(seller, "auto_kiz_manager_chat_id", None) and str(seller.auto_kiz_manager_chat_id).strip():
            target_chats = [str(seller.auto_kiz_manager_chat_id).strip()]
        elif seller.telegram_chat_ids:
            target_chats = [seller.telegram_chat_ids[0]]

        if target_chats:
            tg = TelegramService(tg_token)
            await tg.send_auto_kiz_batch_notification(
                chat_ids=target_chats,
                seller_name=seller.name,
                sales_count=sales_count,
                returns_count=returns_count,
                total_sum_rub=total_sum,
                batch_id=batch_id,
                is_auto_signed=False,
            )
            await tg.close()
    except Exception as err:
        logger.warning(f"[Warehouse Sales] Telegram notification failed: {err}")
