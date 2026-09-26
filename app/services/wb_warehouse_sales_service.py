"""
WB Warehouse Sales Service — WB FBS Manager
Отслеживание повторных продаж маркированных товаров со склада WB (FBO / остатки после возвратов FBS).
Сверка с True API ГИС МТ и формирование очереди пакетов ЭЦП на вывод по фискальным чекам WB.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.seller import Seller
from app.models.order import Order, KizStatus
from app.models.kiz import KizSignatureBatch, BatchStatus, KizProductInfo
from app.models.audit import AuditLog
from app.services.encryption import decrypt
from app.services.wb_analytics_client import WBAnalyticsClient
from app.services.kiz_service import (
    batch_verify_and_sync_cises,
    is_kiz_withdrawn,
    parse_kiz_code,
)
from app.services.telegram_service import TelegramService

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
            code = w.get("kiz_code")
            if code:
                queued.add(code.strip())
    return queued


def build_warehouse_sale_item(
    record: Dict[str, Any],
    clean_cis: str,
    matched_order: Optional[Order],
) -> Dict[str, Any]:
    """Формирует структуру элемента вывода для пакета ЭЦП."""
    price = float(record.get("price") or 0.0)
    price_kop = int(round(price * 100))
    doc_num = str(record.get("fiscal_doc_number") or "").strip()
    fn_num = str(record.get("fiscal_drive_number") or "").strip()
    doc_date = str(record.get("fiscal_dt") or "").strip()
    nm_id = str(record.get("nm_id") or "")
    srid = str(record.get("srid") or "")

    return {
        "order_id": matched_order.id if matched_order else None,
        "sticker_id": matched_order.sticker_id if matched_order else None,
        "kiz_code": clean_cis,
        "receipt_number": doc_num,
        "fn_number": fn_num,
        "receipt_date": doc_date,
        "document_type": "OTHER",
        "primary_document_custom_name": f"Фискальный чек WB (ФД №{doc_num}, ФН №{fn_num})",
        "price": price,
        "price_kopecks": price_kop,
        "article": matched_order.article if matched_order and matched_order.article else nm_id,
        "name": matched_order.name if matched_order and matched_order.name else f"Товар со склада WB (Арт. {nm_id})",
        "task_status": "Продажа со склада WB (FBO)",
        "srid": srid,
        "db_status": matched_order.status.value if matched_order else "FBO_WAREHOUSE",
        "db_kiz_status": matched_order.kiz_status.value if matched_order else "UNATTACHED",
        "cz_status": "INTRODUCED",
        "cz_status_desc": "В обороте на балансе ИП",
        "is_already_withdrawn": False,
        "needs_withdrawal": True,
        "selected": True,
    }


async def process_warehouse_sales_for_seller(
    seller: Seller,
    db: AsyncSession,
    days: int = 14,
) -> Dict[str, Any]:
    """
    Основной метод обработки продаж со склада WB:
    1. Загружает операции из WB excise-report.
    2. Сопоставляет КИЗ с локальной базой и запрашивает True API ГИС МТ.
    3. Для RETIRED — обновляет статус в БД (без лишних документов).
    4. Для INTRODUCED — формирует KizSignatureBatch (source='wb_warehouse_sale') на подпись.
    """
    logger.info(f"[Warehouse Sales] Running for seller {seller.id} ({seller.name}), days={days}")
    excise_rows = await fetch_wb_excise_data(seller, days=days)

    if not excise_rows:
        return {
            "success": True,
            "seller_id": seller.id,
            "created": False,
            "message": "Нет записей в отчете маркировки WB за указанный период",
            "total_wb_records": 0,
            "needs_withdrawal_count": 0,
            "already_retired_count": 0,
        }

    # 1. Сбор уникальных кодов КИЗ
    kiz_to_record: Dict[str, Dict[str, Any]] = {}
    for r in excise_rows:
        raw_cis = str(r.get("excise_short") or "").strip()
        if raw_cis:
            parsed = parse_kiz_code(raw_cis)
            clean = parsed.get("clean_cis") or raw_cis
            kiz_to_record[clean] = r

    unique_cises = list(kiz_to_record.keys())
    logger.info(f"[Warehouse Sales] Found {len(unique_cises)} unique KIZ codes in WB report")

    # 2. Поиск связанных заказов в локальной БД
    stmt_orders = select(Order).where(
        Order.seller_id == seller.id,
        Order.kiz_code.isnot(None),
    )
    res_orders = await db.execute(stmt_orders)
    orders_map: Dict[str, Order] = {}
    for ord_obj in res_orders.scalars().all():
        if ord_obj.kiz_code:
            p = parse_kiz_code(ord_obj.kiz_code)
            c = p.get("clean_cis") or ord_obj.kiz_code
            orders_map[c] = ord_obj

    # 3. Живая пакетная проверка в True API ГИС МТ
    verified_map = await batch_verify_and_sync_cises(
        seller=seller,
        kiz_codes=unique_cises,
        db=db,
        force_refresh=True,
    )

    already_queued = await get_already_queued_cises(seller.id, db)
    now = datetime.now(timezone.utc)
    batch_withdrawals: List[Dict[str, Any]] = []
    already_retired_count = 0
    total_sales_sum = 0.0

    for clean_cis, record in kiz_to_record.items():
        kinfo = verified_map.get(clean_cis)
        cz_st = kinfo.cz_status if kinfo else None
        cz_ex = kinfo.cz_status_ex if kinfo else None
        raw_pl = kinfo.raw_cz_payload if kinfo else {}

        withdrawn, _ = is_kiz_withdrawn(status=cz_st, status_ex=cz_ex, raw_payload=raw_pl)
        matched_order = orders_map.get(clean_cis)

        if withdrawn:
            # Товар уже выбыл в Честном Знаке (например, по ОФД чеку WB)
            already_retired_count += 1
            if matched_order and matched_order.kiz_status != KizStatus.WITHDRAWN:
                matched_order.kiz_status = KizStatus.WITHDRAWN
                matched_order.kiz_cz_status = cz_st or "RETIRED"
                matched_order.updated_at = now
            continue

        # Товар числится в обороте (INTRODUCED) — требует вывода по чеку WB
        if clean_cis in already_queued:
            logger.debug(f"[Warehouse Sales] KIZ {clean_cis} already in pending signature batch")
            continue

        item_data = build_warehouse_sale_item(record, clean_cis, matched_order)
        batch_withdrawals.append(item_data)
        total_sales_sum += item_data["price"]

    await db.commit()

    if not batch_withdrawals:
        return {
            "success": True,
            "seller_id": seller.id,
            "created": False,
            "message": "Все товары из отчета уже выведены из оборота в ГИС МТ",
            "total_wb_records": len(excise_rows),
            "needs_withdrawal_count": 0,
            "already_retired_count": already_retired_count,
        }

    # 4. Формирование KizSignatureBatch
    timestamp_tag = now.strftime("%Y%m%d_%H%M%S")
    summary = {
        "total_rows": len(batch_withdrawals),
        "sales_count": len(batch_withdrawals),
        "sales_needing_withdrawal": len(batch_withdrawals),
        "sales_already_withdrawn": already_retired_count,
        "returns_count": 0,
        "returns_needing_cz_return": 0,
        "total_sales_sum": total_sales_sum,
        "source_type": "wb_warehouse_sale",
        "description": "Повторные продажи со склада Wildberries (FBO)",
    }

    batch = KizSignatureBatch(
        seller_id=seller.id,
        filename=f"wb_warehouse_{timestamp_tag}.json",
        source="wb_warehouse_sale",
        status=BatchStatus.PENDING_SIGNATURE,
        sales_count=len(batch_withdrawals),
        returns_count=0,
        already_withdrawn_count=already_retired_count,
        total_count=len(batch_withdrawals),
        data_payload={
            "summary": summary,
            "withdrawals": batch_withdrawals,
            "returns": [],
        },
    )
    db.add(batch)
    await db.flush()

    # 5. Лог аудита
    audit = AuditLog(
        seller_id=seller.id,
        agent="wb_warehouse_sales",
        action="WAREHOUSE_SALES_BATCH_CREATED",
        entity_type="kiz_signature_batch",
        entity_id=batch.id,
        payload={
            "sales_count": len(batch_withdrawals),
            "already_retired_count": already_retired_count,
            "total_sales_sum": total_sales_sum,
        },
    )
    db.add(audit)
    await db.commit()

    # 6. Уведомление в Telegram
    await _send_telegram_notification(seller, len(batch_withdrawals), total_sales_sum, batch.id)

    return {
        "success": True,
        "seller_id": seller.id,
        "created": True,
        "batch_id": batch.id,
        "sales_count": len(batch_withdrawals),
        "already_retired_count": already_retired_count,
        "total_sales_sum": total_sales_sum,
        "status": batch.status.value,
        "message": f"Сформирован пакет на вывод {len(batch_withdrawals)} товаров со склада WB",
    }


async def _send_telegram_notification(
    seller: Seller,
    count: int,
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
                sales_count=count,
                returns_count=0,
                total_sum_rub=total_sum,
                batch_id=batch_id,
                is_auto_signed=False,
            )
            await tg.close()
    except Exception as err:
        logger.warning(f"[Warehouse Sales] Telegram notification failed: {err}")
