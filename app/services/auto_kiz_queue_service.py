"""
Auto KIZ Queue Service — WB FBS Manager
Автоматический суточный сбор проданных и возвращённых заказов WB FBS,
онлайн-верификация в True API ГИС МТ и формирование очереди пакетов ЭЦП без Excel-файлов.
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple

from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.models.kiz import KizSignatureBatch, BatchStatus, KizProductInfo
from app.models.audit import AuditLog
from app.services.encryption import decrypt
from app.services.wb_client import WBClient
from app.services.kiz_service import (
    batch_verify_and_sync_cises,
    is_kiz_withdrawn,
    parse_kiz_code,
    normalize_kiz_light_industry,
)
from app.services.cz_client import CZClient, CZDocumentError
from app.services.telegram_service import TelegramService
from app.services.crypto_service import is_cryptopro_available

logger = logging.getLogger(__name__)


async def sync_delivered_orders_with_wb(seller: Seller, db: AsyncSession) -> int:
    """
    Шаг 1. Предварительная синхронизация с WB API (Live Sync).
    Запрашивает актуальные статусы (wbStatus) у WB API по всем заказам магазина,
    находящимся в процессе доставки или ожидающим вручения.
    """
    if not seller.wb_api_token_encrypted:
        return 0

    # Отбираем заказы в активных статусах доставки или сборки с привязанным КИЗ
    stmt = select(Order).where(
        Order.seller_id == seller.id,
        Order.kiz_code.isnot(None),
        Order.status.in_([OrderStatus.NEW, OrderStatus.ASSEMBLING, OrderStatus.DELIVERING]),
    )
    res = await db.execute(stmt)
    orders_to_check = res.scalars().all()

    if not orders_to_check:
        return 0

    order_ids = [o.id for o in orders_to_check]
    try:
        wb_token = decrypt(seller.wb_api_token_encrypted)
    except Exception as e:
        logger.error(f"[Auto KIZ] Failed to decrypt WB token for seller {seller.id}: {e}")
        return 0

    try:
        async with WBClient(wb_token) as client:
            statuses_raw = await client.get_orders_status(order_ids)
    except Exception as e:
        logger.warning(f"[Auto KIZ] Failed to fetch WB order statuses for seller {seller.id}: {e}")
        return 0

    status_by_id = {st["id"]: st for st in (statuses_raw or []) if isinstance(st, dict) and "id" in st}
    now = datetime.now(timezone.utc)
    updated_count = 0

    for order in orders_to_check:
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
        elif wb_status in ["canceled", "canceled_by_client", "declined_by_client", "defect"] and order.status != OrderStatus.CANCELLED:
            order.status = OrderStatus.CANCELLED
            order_updated = True
        elif wb_status in ["sorted", "ready_for_pickup", "waiting"] and order.status != OrderStatus.DELIVERING:
            order.status = OrderStatus.DELIVERING
            order_updated = True

        if order_updated:
            order.updated_at = now
            updated_count += 1

    if updated_count > 0:
        await db.commit()
        logger.info(f"[Auto KIZ] Live synced {updated_count} orders from WB API for seller {seller.id}")

    return updated_count


async def collect_auto_kiz_candidates(
    seller: Seller,
    db: AsyncSession,
) -> Tuple[List[Order], List[Order]]:
    """
    Шаг 2. Отбор кандидатов на вывод и возврат КИЗ с дедупликацией.
    Исключает заказы, которые уже находятся в активных незавершённых пакетах PENDING_SIGNATURE.
    """
    # 1. Собираем ID заказов, уже ожидающих подписания в незавершённых пакетах
    stmt_batches = select(KizSignatureBatch).where(
        KizSignatureBatch.seller_id == seller.id,
        KizSignatureBatch.status == BatchStatus.PENDING_SIGNATURE,
    )
    res_batches = await db.execute(stmt_batches)
    pending_batches = res_batches.scalars().all()

    already_queued_order_ids = set()
    for b in pending_batches:
        payload = b.data_payload or {}
        for w in payload.get("withdrawals", []):
            oid = w.get("order_id")
            if oid is not None:
                already_queued_order_ids.add(oid)
        for r in payload.get("returns", []):
            oid = r.get("order_id")
            if oid is not None:
                already_queued_order_ids.add(oid)

    # 2. Кандидаты на вывод (Продажи): wb_status == 'sold' или (DELIVERED без wb_status), еще не выведены
    stmt_withdrawals = select(Order).where(
        Order.seller_id == seller.id,
        Order.kiz_code.isnot(None),
        Order.kiz_status != KizStatus.WITHDRAWN,
        or_(
            Order.wb_status == "sold",
            and_(Order.status == OrderStatus.DELIVERED, Order.wb_status.is_(None)),
        ),
    )
    if already_queued_order_ids:
        stmt_withdrawals = stmt_withdrawals.where(Order.id.not_in(already_queued_order_ids))

    res_w = await db.execute(stmt_withdrawals)
    withdrawal_orders = res_w.scalars().all()

    # 3. Кандидаты на возврат в оборот (Возвраты): отмененные/возвращенные заказы, где КИЗ БЫЛ выведен
    stmt_returns = select(Order).where(
        Order.seller_id == seller.id,
        Order.kiz_code.isnot(None),
        Order.kiz_status == KizStatus.WITHDRAWN,
        or_(
            Order.status == OrderStatus.CANCELLED,
            Order.wb_status.in_(["canceled", "canceled_by_client", "declined_by_client", "defect"]),
        ),
    )
    if already_queued_order_ids:
        stmt_returns = stmt_returns.where(Order.id.not_in(already_queued_order_ids))

    res_r = await db.execute(stmt_returns)
    return_orders = res_r.scalars().all()

    # 4. Также проверяем отмененные заказы, где КИЗ НЕ выводился (kiz_status != WITHDRAWN)
    # Они не требуют документов в ГИС МТ — просто освобождаем КИЗ в БД
    stmt_unwithdrawn_cancelled = select(Order).where(
        Order.seller_id == seller.id,
        Order.kiz_code.isnot(None),
        Order.kiz_status.in_([KizStatus.ATTACHED, KizStatus.VALIDATED, KizStatus.ERROR]),
        or_(
            Order.status == OrderStatus.CANCELLED,
            Order.wb_status.in_(["canceled", "canceled_by_client", "declined_by_client", "defect"]),
        ),
    )
    res_uw = await db.execute(stmt_unwithdrawn_cancelled)
    for unwithdrawn_order in res_uw.scalars().all():
        # Товар не выводился из ГИС МТ — освобождаем в БД
        unwithdrawn_order.kiz_status = KizStatus.NOT_ATTACHED
        unwithdrawn_order.updated_at = datetime.now(timezone.utc)
    if res_uw.scalars().all():
        await db.commit()

    return list(withdrawal_orders), list(return_orders)


async def verify_candidates_against_cz(
    seller: Seller,
    withdrawals: List[Order],
    returns: List[Order],
    db: AsyncSession,
) -> Tuple[List[Order], List[Order]]:
    """
    Шаг 3. Предварительная валидация в True API «Честный Знак».
    Отсеивает коды, которые уже выведены (для продаж) или уже в обороте (для возвратов).
    """
    all_codes = set()
    for o in withdrawals:
        if o.kiz_code:
            all_codes.add(o.kiz_code)
    for o in returns:
        if o.kiz_code:
            all_codes.add(o.kiz_code)

    if not all_codes or not seller.cz_token_encrypted:
        return withdrawals, returns

    # Запрашиваем актуальные данные из ГИС МТ
    verified_map = {}
    try:
        verified_map = await batch_verify_and_sync_cises(
            seller=seller,
            kiz_codes=list(all_codes),
            db=db,
            force_refresh=True,
        )
    except Exception as e:
        logger.warning(f"[Auto KIZ] True API verification failed for seller {seller.id}: {e}")
        return withdrawals, returns

    final_withdrawals = []
    now = datetime.now(timezone.utc)

    for order in withdrawals:
        kinfo = verified_map.get(order.kiz_code)
        if kinfo:
            is_withdrawn_flag, _ = is_kiz_withdrawn(
                status=kinfo.cz_status,
                status_ex=kinfo.cz_status_ex,
                raw_payload=kinfo.raw_cz_payload or {},
            )
            if is_withdrawn_flag:
                # КИЗ уже выбыл в ГИС МТ (например, списан вручную) — синхронизируем БД и не включаем в пакет
                order.kiz_status = KizStatus.WITHDRAWN
                order.kiz_cz_status = kinfo.cz_status or "RETIRED"
                order.updated_at = now
                continue
        final_withdrawals.append(order)

    final_returns = []
    for order in returns:
        kinfo = verified_map.get(order.kiz_code)
        if kinfo:
            is_withdrawn_flag, _ = is_kiz_withdrawn(
                status=kinfo.cz_status,
                status_ex=kinfo.cz_status_ex,
                raw_payload=kinfo.raw_cz_payload or {},
            )
            if not is_withdrawn_flag:
                # КИЗ уже находится в обороте (INTRODUCED) — возвращать повторно не нужно
                order.kiz_status = KizStatus.RETURNED
                order.kiz_cz_status = kinfo.cz_status or "INTRODUCED"
                order.updated_at = now
                continue
        final_returns.append(order)

    await db.commit()
    return final_withdrawals, final_returns


async def process_auto_kiz_queue_for_seller(
    seller: Seller,
    db: AsyncSession,
    trigger_source: str = "auto",
) -> Dict[str, Any]:
    """
    Основной метод суточного цикла обработки КИЗ:
    1. Синхронизирует статусы WB API.
    2. Отбирает кандидатов на вывод и ввод с защитой от дублей.
    3. Верифицирует статусы в True API.
    4. Формирует KizSignatureBatch (source='auto').
    5. При включенной серверной подписи — подписывает и отправляет в ГИС МТ.
    6. Отправляет Telegram-уведомление персонально менеджеру.
    """
    logger.info(f"[Auto KIZ] Running for seller {seller.id} ({seller.name}), source={trigger_source}")

    # 1. Live WB sync
    await sync_delivered_orders_with_wb(seller, db)

    # 2. Collect candidates
    withdrawals_raw, returns_raw = await collect_auto_kiz_candidates(seller, db)

    # 3. Verify with True API
    withdrawals_orders, returns_orders = await verify_candidates_against_cz(seller, withdrawals_raw, returns_raw, db)

    now = datetime.now(timezone.utc)
    seller.last_auto_kiz_queue_at = now

    if not withdrawals_orders and not returns_orders:
        await db.commit()
        logger.info(f"[Auto KIZ] No pending KIZ operations for seller {seller.id}")
        return {
            "success": True,
            "seller_id": seller.id,
            "created": False,
            "message": "Нет заказов, требующих вывода или возврата КИЗ",
            "sales_count": 0,
            "returns_count": 0,
        }

    # 4. Build structured payload
    today_str = now.strftime("%Y-%m-%d")
    batch_withdrawals = []
    total_sales_sum = 0.0

    for o in withdrawals_orders:
        parsed = parse_kiz_code(o.kiz_code)
        clean_cis = parsed.get("clean_cis") or o.kiz_code
        price = float(o.price or 0.0)
        price_kop = int(round(price * 100))
        total_sales_sum += price

        doc_date = o.updated_at.strftime("%Y-%m-%d") if o.updated_at else today_str

        batch_withdrawals.append({
            "order_id": o.id,
            "sticker_id": o.sticker_id,
            "kiz_code": clean_cis,
            "receipt_number": None,
            "document_type": "OTHER",
            "primary_document_custom_name": "Сборочное задание Wildberries FBS",
            "receipt_date": doc_date,
            "price": price,
            "price_kopecks": price_kop,
            "article": o.article or "",
            "name": o.name or "Товар Wildberries FBS",
            "task_status": "Продано (WB API)",
            "db_status": o.status.value if o.status else "DELIVERED",
            "db_kiz_status": o.kiz_status.value if o.kiz_status else "ATTACHED",
            "cz_status": o.kiz_cz_status or "INTRODUCED",
            "cz_status_desc": "В обороте",
            "is_already_withdrawn": False,
            "needs_withdrawal": True,
            "selected": True,
        })

    batch_returns = []
    for o in returns_orders:
        parsed = parse_kiz_code(o.kiz_code)
        clean_cis = parsed.get("clean_cis") or o.kiz_code
        price = float(o.price or 0.0)
        price_kop = int(round(price * 100))
        doc_date = o.updated_at.strftime("%Y-%m-%d") if o.updated_at else today_str

        batch_returns.append({
            "order_id": o.id,
            "sticker_id": o.sticker_id,
            "kiz_code": clean_cis,
            "receipt_number": None,
            "document_type": "OTHER",
            "primary_document_custom_name": "Возврат от покупателя Wildberries FBS",
            "receipt_date": doc_date,
            "price": price,
            "price_kopecks": price_kop,
            "article": o.article or "",
            "name": o.name or "Товар Wildberries FBS",
            "task_status": "Возврат / отказ покупателя (WB API)",
            "db_status": o.status.value if o.status else "CANCELLED",
            "db_kiz_status": o.kiz_status.value if o.kiz_status else "WITHDRAWN",
            "cz_status": o.kiz_cz_status or "RETIRED",
            "cz_status_desc": "Выбыл",
            "needs_cz_return": True,
            "action_recommended": "⚠️ Требует возврата в оборот",
            "selected": True,
        })

    summary = {
        "total_rows": len(batch_withdrawals) + len(batch_returns),
        "sales_count": len(batch_withdrawals),
        "sales_needing_withdrawal": len(batch_withdrawals),
        "sales_already_withdrawn": 0,
        "returns_count": len(batch_returns),
        "returns_needing_cz_return": len(batch_returns),
        "returns_already_in_circulation": 0,
        "total_sales_sum": total_sales_sum,
    }

    # 5. Save KizSignatureBatch
    timestamp_tag = now.strftime("%Y%m%d_%H%M%S")
    batch = KizSignatureBatch(
        seller_id=seller.id,
        filename=f"auto_kiz_{timestamp_tag}.json",
        source=trigger_source,
        status=BatchStatus.PENDING_SIGNATURE,
        sales_count=len(batch_withdrawals),
        returns_count=len(batch_returns),
        already_withdrawn_count=0,
        total_count=len(batch_withdrawals) + len(batch_returns),
        data_payload={
            "summary": summary,
            "withdrawals": batch_withdrawals,
            "returns": batch_returns,
        },
    )
    db.add(batch)
    await db.flush()

    is_auto_signed = False

    # 6. Optional auto-sign on server if enabled and CryptoPro CSP is available
    if seller.auto_kiz_auto_sign_server and is_cryptopro_available():
        try:
            cz_token = decrypt(seller.cz_token_encrypted) if seller.cz_token_encrypted else ""
            async with CZClient(inn=seller.cz_inn or "", token=cz_token, cert_thumbprint=seller.cz_cert_path) as cz_client:
                # Withdrawals
                for w in batch_withdrawals:
                    await cz_client.withdraw_from_circulation(
                        kiz_codes=[w["kiz_code"]],
                        price_kopecks=w["price_kopecks"],
                        mod_fias=seller.mod_fias,
                        mod_kpp=seller.mod_kpp,
                        wb_order_id=w["order_id"],
                        receipt_date=w["receipt_date"],
                        document_type="OTHER",
                        wait_for_result=False,
                    )
                # Returns
                for r in batch_returns:
                    await cz_client.return_to_circulation(
                        kiz_codes=[r["kiz_code"]],
                        wb_order_id=r["order_id"],
                        receipt_date=r["receipt_date"],
                        primary_document_type="OTHER",
                        wait_for_result=False,
                    )
            batch.status = BatchStatus.COMPLETED
            batch.signed_at = now
            batch.signed_by = "server_cryptopro_auto"
            is_auto_signed = True
            logger.info(f"[Auto KIZ] Batch {batch.id} auto-signed on server")
        except Exception as sign_err:
            logger.error(f"[Auto KIZ] Server auto-signing failed for batch {batch.id}: {sign_err}")
            batch.error_message = f"Ошибка серверной подписи: {sign_err}"

    # 7. Audit log
    audit = AuditLog(
        seller_id=seller.id,
        agent="auto_kiz_queue",
        action="AUTO_BATCH_CREATED",
        entity_type="kiz_signature_batch",
        entity_id=batch.id,
        payload={
            "source": trigger_source,
            "sales_count": len(batch_withdrawals),
            "returns_count": len(batch_returns),
            "is_auto_signed": is_auto_signed,
        },
    )
    db.add(audit)
    await db.commit()

    # 8. Send Telegram notification strictly to designated manager
    if seller.telegram_bot_token_encrypted:
        try:
            tg_token = decrypt(seller.telegram_bot_token_encrypted)
            # Manager selection logic:
            # 1. Prefer seller.auto_kiz_manager_chat_id
            # 2. Fallback to first chat in seller.telegram_chat_ids if manager not explicitly set
            target_chats = []
            if seller.auto_kiz_manager_chat_id and str(seller.auto_kiz_manager_chat_id).strip():
                target_chats = [str(seller.auto_kiz_manager_chat_id).strip()]
            elif seller.telegram_chat_ids and len(seller.telegram_chat_ids) > 0:
                target_chats = [seller.telegram_chat_ids[0]]

            if target_chats:
                tg = TelegramService(tg_token)
                await tg.send_auto_kiz_batch_notification(
                    chat_ids=target_chats,
                    seller_name=seller.name,
                    sales_count=len(batch_withdrawals),
                    returns_count=len(batch_returns),
                    total_sum_rub=total_sales_sum,
                    batch_id=batch.id,
                    is_auto_signed=is_auto_signed,
                )
                await tg.close()
        except Exception as tg_err:
            logger.warning(f"[Auto KIZ] Failed to send Telegram notification: {tg_err}")

    return {
        "success": True,
        "seller_id": seller.id,
        "created": True,
        "batch_id": batch.id,
        "sales_count": len(batch_withdrawals),
        "returns_count": len(batch_returns),
        "total_sales_sum": total_sales_sum,
        "is_auto_signed": is_auto_signed,
        "status": batch.status.value,
        "message": f"Сформирован пакет на {len(batch_withdrawals)} выводов и {len(batch_returns)} возвратов",
    }
