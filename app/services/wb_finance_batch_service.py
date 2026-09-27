"""
WB Finance Return Batch Service — WB FBS Manager
Формирование выверенного пакета возврата КИЗ в оборот (KizSignatureBatch)
с хронологическим анализом жизненного цикла и кросс-сверкой с БД заказов FBS.
"""
import copy
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.audit import AuditLog
from app.models.kiz import BatchStatus, KizSignatureBatch
from app.models.order import Order
from app.models.seller import Seller
from app.models.wb_finance import WbSalesReportRow
from app.services.cz_client import CZClient
from app.services.encryption import decrypt
from app.services.kiz_service import parse_kiz_code

logger = logging.getLogger(__name__)


def _is_return_row(row: WbSalesReportRow) -> bool:
    """Определяет, является ли строка отчета операцией возврата."""
    doc_type = (row.doc_type_name or "").strip()
    oper_name = (row.seller_oper_name or "").strip()
    return doc_type == "Возврат" or oper_name == "Возврат" or (row.return_amount is not None and row.return_amount > 0)


def _is_sale_row(row: WbSalesReportRow) -> bool:
    """Определяет, является ли строка отчета операцией продажи."""
    doc_type = (row.doc_type_name or "").strip()
    oper_name = (row.seller_oper_name or "").strip()
    return doc_type == "Продажа" or oper_name == "Продажа"


async def create_finance_return_signature_batch(
    seller: Seller,
    db: AsyncSession,
    days: int = 90,
) -> Dict[str, Any]:
    """
    1. Извлекает все строки отчетов WB реализации за указанный период (по умолчанию 90 дней).
    2. Проводит хронологический анализ по каждому КИЗ (отсекает повторно проданные).
    3. Кросс-сверяет кандидатов с локальной БД заказов FBS (orders).
    4. Запрашивает True API ГИС МТ для получения актуального онлайн-статуса и ИНН владельца.
    5. Создает запись KizSignatureBatch со статусом PENDING_SIGNATURE для подписания в веб-интерфейсе.
    """
    now_utc = datetime.now(timezone.utc)
    since_date = (now_utc - timedelta(days=days)).date()

    # 1. Выбираем все строки отчетов с заполненным КИЗ
    stmt = (
        select(WbSalesReportRow)
        .where(
            WbSalesReportRow.seller_id == seller.id,
            WbSalesReportRow.clean_cis.isnot(None),
            (WbSalesReportRow.rr_date >= since_date) | (WbSalesReportRow.rr_date.is_(None)),
        )
        .order_by(
            WbSalesReportRow.clean_cis,
            WbSalesReportRow.rr_date.asc().nullsfirst(),
            WbSalesReportRow.rrd_id.asc(),
        )
    )
    res = await db.execute(stmt)
    all_rows = res.scalars().all()

    if not all_rows:
        return {
            "success": False,
            "message": "В базе данных нет сохраненных строк финансовых отчетов с КИЗ за указанный период",
            "batch_id": None,
        }

    # 2. Хронологическая группировка по clean_cis
    cises_map: Dict[str, List[WbSalesReportRow]] = {}
    for r in all_rows:
        cis = r.clean_cis
        if not cis:
            continue
        cises_map.setdefault(cis, []).append(r)

    return_candidates: Dict[str, WbSalesReportRow] = {}
    resold_after_return_count = 0
    only_sales_count = 0

    for cis, history in cises_map.items():
        # Сортируем: rr_date, sale_dt, rrd_id
        sorted_history = sorted(
            history,
            key=lambda x: (
                x.rr_date or date.min,
                x.sale_dt or datetime.min.replace(tzinfo=timezone.utc),
                x.rrd_id,
            )
        )
        terminal_row = sorted_history[-1]

        had_return = any(_is_return_row(r) for r in sorted_history)
        last_is_return = _is_return_row(terminal_row)
        last_is_sale = _is_sale_row(terminal_row)

        if last_is_return:
            return_candidates[cis] = terminal_row
        elif had_return and last_is_sale:
            # Товар возвращался, но в итоге был повторно продан!
            resold_after_return_count += 1
        else:
            only_sales_count += 1

    if not return_candidates:
        return {
            "success": False,
            "message": "Среди строк отчетов не найдено актуальных возвратов (все товары либо проданы, либо повторно реализованы)",
            "resold_count": resold_after_return_count,
            "batch_id": None,
        }

    # 3. Кросс-сверка с БД заказов FBS
    candidate_cises = list(return_candidates.keys())
    order_stmt = select(Order).where(
        Order.seller_id == seller.id,
        Order.kiz_code.isnot(None),
    )
    orders_res = await db.execute(order_stmt)
    matching_orders = orders_res.scalars().all()

    fbs_order_lookup: Dict[str, Order] = {}
    for o in matching_orders:
        if not o.kiz_code:
            continue
        parsed = parse_kiz_code(o.kiz_code)
        clean = parsed.get("clean_cis") or o.kiz_code
        if clean in return_candidates:
            fbs_order_lookup[clean] = o
        if o.kiz_code in return_candidates:
            fbs_order_lookup[o.kiz_code] = o

    # 4. Запрос в True API ГИС МТ (пачками по 100)
    cz_info_map: Dict[str, Dict[str, Any]] = {}
    cz_token = decrypt(seller.cz_token_encrypted) if seller.cz_token_encrypted else ""

    if cz_token and seller.cz_inn:
        client = CZClient(inn=seller.cz_inn, token=cz_token)
        try:
            for i in range(0, len(candidate_cises), 100):
                chunk = candidate_cises[i:i + 100]
                chunk_res = await client.get_cises_info(chunk)
                if chunk_res:
                    for item in chunk_res:
                        info = item.get("cisInfo") if isinstance(item, dict) and "cisInfo" in item else item
                        if isinstance(info, dict):
                            req_cis = info.get("requestedCis") or info.get("cis")
                            if req_cis:
                                cz_info_map[req_cis] = info
        except Exception as e:
            logger.warning(f"[WB Finance Batch] True API query error: {e}")

    # 5. Формирование структуры возвратов для KizSignatureBatch
    returns_payload: List[Dict[str, Any]] = []
    wb_owned_count = 0
    seller_owned_count = 0
    already_in_circulation_count = 0
    needing_return_count = 0

    seller_inn = (seller.cz_inn or "").strip()

    for cis, row in return_candidates.items():
        cz_info = cz_info_map.get(cis, {})
        cz_status = cz_info.get("status") or row.cz_status
        owner_inn = (cz_info.get("ownerInn") or row.cz_owner_inn or "").strip()
        owner_name = cz_info.get("ownerName") or row.cz_owner_name or ""
        producer_inn = (cz_info.get("producerInn") or "").strip()
        withdraw_reason = cz_info.get("withdrawReason") or ""

        is_already_in_circ = (cz_status == "INTRODUCED")
        needs_cz_return = (cz_status == "RETIRED") or (not cz_status and True)
        if is_already_in_circ:
            needs_cz_return = False

        is_wb = (owner_inn == "9714053621")
        is_seller = (owner_inn == seller_inn) if seller_inn else False

        if is_wb:
            wb_owned_count += 1
        elif is_seller:
            seller_owned_count += 1

        if is_already_in_circ:
            already_in_circulation_count += 1
        elif needs_cz_return:
            needing_return_count += 1

        # FBS context
        fbs_order = fbs_order_lookup.get(cis)
        fbs_order_id = fbs_order.id if fbs_order else None
        fbs_sticker = fbs_order.sticker_id if fbs_order else None
        fbs_status = fbs_order.status.value if fbs_order else "Нет в FBS (FBO/архив)"
        fbs_kiz_status = fbs_order.kiz_status.value if fbs_order else None

        if is_already_in_circ:
            action_rec = "✅ Уже в обороте (готов к привязке)"
        elif needs_cz_return and is_wb:
            action_rec = "⚠️ Требует возврата в оборот (баланс WB / РВБ)"
        elif needs_cz_return and is_seller:
            action_rec = "⚠️ Требует возврата в оборот (баланс ИП)"
        else:
            action_rec = f"⚠️ Требует возврата в оборот ({cz_status or 'Статус не определен'})"

        if fbs_order and fbs_order.status.value in ("ASSEMBLING", "ASSEMBLED"):
            action_rec += " 🚨 Товар в сборке!"

        price_val = float(row.retail_amount or row.retail_price or 0.0)

        returns_payload.append({
            "order_id": fbs_order_id,
            "sticker_id": fbs_sticker,
            "srid": row.srid,
            "kiz_code": row.kiz or cis,
            "clean_cis": cis,
            "receipt_number": str(row.rrd_id),
            "receipt_date": str(row.rr_date) if row.rr_date else None,
            "price": price_val,
            "price_kopecks": int(round(price_val * 100)),
            "article": str(row.nm_id or ""),
            "vendor_code": row.vendor_code or "",
            "name": row.subject_name or (f"Товар арт. {row.nm_id}" if row.nm_id else "Товар WB"),
            "task_status": "Возврат WB",
            "db_status": fbs_status,
            "db_kiz_status": fbs_kiz_status,
            "cz_status": cz_status or "UNKNOWN",
            "cz_status_desc": f"{cz_status or 'Не проверен'}" + (f" ({withdraw_reason})" if withdraw_reason else ""),
            "cz_owner_inn": owner_inn,
            "cz_owner_name": owner_name,
            "cz_producer_inn": producer_inn,
            "is_wb_owned": is_wb,
            "is_seller_owner": is_seller,
            "needs_cz_return": needs_cz_return,
            "is_already_in_circulation": is_already_in_circ,
            "action_recommended": action_rec,
            "selected": needs_cz_return and bool(row.kiz or cis),
        })

    # Сводка пакета
    summary = {
        "period_days": days,
        "total_unique_cises_scanned": len(cises_map),
        "resold_after_return_count": resold_after_return_count,
        "only_sales_count": only_sales_count,
        "return_candidates_count": len(return_candidates),
        "returns_needing_cz_return": needing_return_count,
        "returns_already_in_circulation": already_in_circulation_count,
        "wb_owned_count": wb_owned_count,
        "seller_owned_count": seller_owned_count,
        "linked_to_fbs_orders": len(fbs_order_lookup),
    }

    # 6. Сохранение в KizSignatureBatch
    batch_id = str(uuid.uuid4())
    date_str = now_utc.strftime("%Y%m%d_%H%M%S")
    batch = KizSignatureBatch(
        id=batch_id,
        seller_id=seller.id,
        filename=f"Возвраты_WB_Финансы_90дней_{date_str}.xlsx",
        source="wb_finance_returns",
        status=BatchStatus.PENDING_SIGNATURE,
        sales_count=0,
        returns_count=needing_return_count,
        already_withdrawn_count=already_in_circulation_count,
        total_count=len(returns_payload),
        data_payload={
            "summary": summary,
            "withdrawals": [],
            "returns": returns_payload,
        },
    )
    db.add(batch)

    # 7. Запись в AuditLog
    audit = AuditLog(
        seller_id=seller.id,
        agent="wb_finance_batch_service",
        action="CREATE_FINANCE_RETURN_BATCH",
        payload={
            "batch_id": batch_id,
            "days": days,
            "total_candidates": len(return_candidates),
            "needing_return": needing_return_count,
            "wb_owned": wb_owned_count,
            "seller_owned": seller_owned_count,
            "resold_after_return": resold_after_return_count,
            "linked_fbs": len(fbs_order_lookup),
        },
    )
    db.add(audit)
    await db.commit()

    return {
        "success": True,
        "batch_id": batch_id,
        "filename": batch.filename,
        "summary": summary,
        "returns_count": len(returns_payload),
    }
