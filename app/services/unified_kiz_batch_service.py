"""
Unified KIZ Batch Reconciliation Service.
Aggregates and reconciles data across ALL sources:
1. Active FBS orders (DELIVERED for withdrawal, CANCELLED for returns).
2. WB FBO warehouse sales.
3. WB 90-day detailed financial sales reports (wb_sales_report_rows).

Applies True API verification and strict seller ownership validation:
- WITHDRAWAL: sales not yet retired in GIS MT.
- RETURN: returns that are RETIRED and strictly owned by the seller (owner_inn == seller.cz_inn).
- REMARKING: returns owned by WB (ООО «РВБ») or foreign/third party -> excluded from return.
- INTRODUCED: already in circulation -> no action needed.
"""
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.models.kiz import KizSignatureBatch, BatchStatus, KizProductInfo, KizOperation, KizOperationType
from app.models.wb_finance import WbSalesReportRow
from app.models.audit import AuditLog
from app.services.kiz_service import (
    parse_kiz_code,
    is_kiz_withdrawn,
    CZ_STATUS_DESCRIPTIONS,
    batch_verify_and_sync_cises,
)
from app.services.wb_finance_service import sync_seller_financial_reports
from app.services.wb_warehouse_sales_service import fetch_wb_excise_data
from app.services.unified_kiz_payload_builder import (
    build_unified_withdrawals_payload,
    build_unified_returns_payload,
)

logger = logging.getLogger(__name__)


def _is_return_row(row: WbSalesReportRow) -> bool:
    n = (row.doc_type_name or row.seller_oper_name or "").lower()
    return "возврат" in n or "отказ" in n or "отмен" in n


def _is_sale_row(row: WbSalesReportRow) -> bool:
    n = (row.doc_type_name or row.seller_oper_name or "").lower()
    return ("продаж" in n or "продано" in n) and not _is_return_row(row)


async def create_unified_kiz_signature_batch(
    seller: Seller,
    db: AsyncSession,
    days: int = 90,
    sync_finance_api: bool = False,
    sync_archive_api: bool = False,
) -> Dict[str, Any]:
    """
    Создает единый пакет сверки маркировки по ВСЕМ источникам данных:
    FBS заказы + FBO продажи со склада + финансовые отчеты за N дней.
    """
    now_utc = datetime.now(timezone.utc)
    seller_inn = (seller.cz_inn or "").strip()
    since_date = (now_utc - timedelta(days=days)).date()

    if sync_archive_api and seller.wb_api_token_encrypted:
        try:
            from app.services.wb_archive_service import sync_seller_archive_orders_recent_months
            await sync_seller_archive_orders_recent_months(seller=seller, db=db, months_count=3)
        except Exception as arc_err:
            logger.warning(f"Could not auto-sync fresh archive API orders: {arc_err}")

    if sync_finance_api and seller.wb_api_token_encrypted:
        try:
            d_from = (now_utc - timedelta(days=min(days, 30))).strftime("%Y-%m-%dT00:00:00Z")
            await sync_seller_financial_reports(seller=seller, db=db, date_from=d_from, date_to=now_utc.strftime("%Y-%m-%dT23:59:59Z"))
        except Exception as sync_err:
            logger.warning(f"Could not auto-sync fresh finance reports: {sync_err}")

    # 1. Загрузка заказов FBS
    fbs_stmt = select(Order).where(Order.seller_id == seller.id, Order.kiz_code.isnot(None))
    fbs_res = await db.execute(fbs_stmt)
    all_fbs_orders = fbs_res.scalars().all()

    fbs_order_lookup: Dict[str, Order] = {}
    for o in all_fbs_orders:
        if o.kiz_code:
            fbs_order_lookup[o.kiz_code] = o
            parsed = parse_kiz_code(o.kiz_code)
            clean = parsed.get("clean_cis")
            if clean:
                fbs_order_lookup[clean] = o

    # 2. Загрузка финансовых отчетов за период
    stmt_fin = (
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
    fin_res = await db.execute(stmt_fin)
    all_fin_rows = fin_res.scalars().all()

    # 3. Сквозная хронологическая группировка по clean_cis
    history_by_cis: Dict[str, List[Dict[str, Any]]] = {}

    for r in all_fin_rows:
        cis = r.clean_cis
        if not cis:
            continue
        ev_type = "RETURN" if _is_return_row(r) else ("SALE" if _is_sale_row(r) else "UNKNOWN")
        ev_date = r.sale_dt or (datetime.combine(r.rr_date, datetime.min.time(), tzinfo=timezone.utc) if r.rr_date else now_utc)
        history_by_cis.setdefault(cis, []).append({
            "source": "wb_finance", "type": ev_type, "date": ev_date, "rrd_id": r.rrd_id,
            "rr_date": r.rr_date, "raw_kiz": r.kiz, "price": float(r.retail_amount or r.retail_price or 0.0),
            "article": str(r.nm_id or ""), "name": r.subject_name or "", "srid": r.srid,
        })

    for o in all_fbs_orders:
        if not o.kiz_code:
            continue
        parsed = parse_kiz_code(o.kiz_code)
        cis = parsed.get("clean_cis") or o.kiz_code
        ev_date = o.updated_at or o.created_at or now_utc
        if o.status == OrderStatus.CANCELLED or o.kiz_status == KizStatus.RETURNED or o.wb_status in ["canceled", "canceled_by_client", "declined_by_client", "defect"]:
            if o.kiz_status == KizStatus.WITHDRAWN or o.cz_withdrawal_doc_id:
                history_by_cis.setdefault(cis, []).append({"source": "fbs_order", "type": "SALE", "date": o.created_at or ev_date, "order": o})
            history_by_cis.setdefault(cis, []).append({"source": "fbs_order", "type": "RETURN", "date": ev_date, "order": o})
        elif o.status == OrderStatus.DELIVERED or o.kiz_status == KizStatus.WITHDRAWN or o.wb_status == "sold":
            history_by_cis.setdefault(cis, []).append({"source": "fbs_order", "type": "SALE", "date": ev_date, "order": o})

    # 2.5. Загрузка оперативных онлайн продаж из WB Analytics excise-report (кассовые чеки)
    excise_count = 0
    if seller.wb_api_token_encrypted:
        try:
            excise_rows = await fetch_wb_excise_data(seller=seller, days=min(days, 30))
            excise_count = len(excise_rows)
            for er in excise_rows:
                raw_kiz = str(er.get("excise_short") or "").strip()
                if not raw_kiz:
                    continue
                parsed = parse_kiz_code(raw_kiz)
                cis = parsed.get("clean_cis") or raw_kiz
                dt_str = str(er.get("fiscal_dt") or "").strip()
                try:
                    ev_date = datetime.strptime(dt_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except Exception:
                    ev_date = now_utc
                history_by_cis.setdefault(cis, []).append({
                    "source": "wb_excise_report",
                    "type": "SALE",
                    "date": ev_date,
                    "receipt_number": str(er.get("fiscal_doc_number") or "").strip(),
                    "fn_number": str(er.get("fiscal_drive_number") or "").strip(),
                    "receipt_date": dt_str,
                    "price": float(er.get("price") or 0.0),
                    "article": str(er.get("nm_id") or ""),
                    "raw_kiz": raw_kiz,
                    "srid": str(er.get("srid") or ""),
                })
        except Exception as exc_err:
            logger.warning(f"Could not load online excise report: {exc_err}")

    # 2.6. Загрузка данных из активных пакетов архивов (ожидающих подписи)
    stmt_pending = select(KizSignatureBatch).where(
        KizSignatureBatch.seller_id == seller.id,
        KizSignatureBatch.status == BatchStatus.PENDING_SIGNATURE,
    ).order_by(KizSignatureBatch.created_at.desc())
    res_pending = await db.execute(stmt_pending)
    pending_batches = res_pending.scalars().all()

    for pb in pending_batches:
        p_payload = pb.data_payload or {}
        for r_item in p_payload.get("returns", []):
            raw_k = r_item.get("kiz_code")
            if not raw_k:
                continue
            parsed_k = parse_kiz_code(raw_k)
            c_cis = parsed_k.get("clean_cis") or raw_k
            history_by_cis.setdefault(c_cis, []).append({
                "source": "archive_upload",
                "type": "RETURN",
                "date": pb.created_at or now_utc,
                "order_id": r_item.get("order_id"),
                "price": float(r_item.get("price") or 0.0),
                "article": str(r_item.get("article") or ""),
                "name": r_item.get("name") or "",
                "raw_kiz": raw_k,
                "receipt_number": r_item.get("receipt_number"),
                "receipt_date": r_item.get("receipt_date"),
            })
        for w_item in p_payload.get("withdrawals", []):
            raw_k = w_item.get("kiz_code")
            if not raw_k:
                continue
            parsed_k = parse_kiz_code(raw_k)
            c_cis = parsed_k.get("clean_cis") or raw_k
            history_by_cis.setdefault(c_cis, []).append({
                "source": "archive_upload",
                "type": "SALE",
                "date": pb.created_at or now_utc,
                "order_id": w_item.get("order_id"),
                "price": float(w_item.get("price") or 0.0),
                "article": str(w_item.get("article") or ""),
                "name": w_item.get("name") or "",
                "raw_kiz": raw_k,
                "receipt_number": w_item.get("receipt_number"),
                "fn_number": w_item.get("fn_number"),
                "receipt_date": w_item.get("receipt_date"),
            })

    # 4. Анализ терминального состояния каждого КИЗ
    sales_candidates: Dict[str, Dict[str, Any]] = {}
    return_candidates: Dict[str, Dict[str, Any]] = {}
    resold_after_return_count = 0

    for cis, events in history_by_cis.items():
        sorted_ev = sorted(events, key=lambda x: x["date"])
        last_ev = sorted_ev[-1]
        had_return = any(e["type"] == "RETURN" for e in sorted_ev)

        # Сквозное сопоставление по srid: если продажа имеет тот же srid, что и возврат,
        # то данная продажа аннулирована возвратом покупателя!
        if last_ev["type"] == "SALE" and last_ev.get("srid"):
            matching_return = next(
                (e for e in sorted_ev if e["type"] == "RETURN" and e.get("srid") == last_ev.get("srid")),
                None,
            )
            if matching_return:
                last_ev = matching_return

        if last_ev["type"] == "RETURN":
            return_candidates[cis] = last_ev
        elif last_ev["type"] == "SALE":
            if had_return:
                resold_after_return_count += 1
            sales_candidates[cis] = last_ev

    # 5. Пакетная верификация в True API Честного Знака с Skip-Filter (пропуск чужих владельцев и RETIRED)
    all_cises = list(set(list(sales_candidates.keys()) + list(return_candidates.keys())))
    cz_info_map: Dict[str, Dict[str, Any]] = {}

    if all_cises:
        try:
            k_info_map = await batch_verify_and_sync_cises(
                db=db,
                seller=seller,
                kiz_codes=all_cises,
                force_refresh=False,
            )
            for cis_key, rec in k_info_map.items():
                if not rec:
                    continue
                info = dict(rec.raw_cz_payload or {})
                if rec.cz_status:
                    info["status"] = rec.cz_status
                if rec.cz_owner_inn:
                    info["ownerInn"] = rec.cz_owner_inn
                if rec.cz_owner_name:
                    info["ownerName"] = rec.cz_owner_name
                if rec.cz_producer_inn:
                    info["producerInn"] = rec.cz_producer_inn
                if rec.cz_status_ex:
                    info["statusEx"] = rec.cz_status_ex

                cz_info_map[cis_key] = info
                if rec.clean_cis:
                    cz_info_map[rec.clean_cis] = info
                if rec.kiz_code:
                    cz_info_map[rec.kiz_code] = info
        except Exception as e:
            logger.error(f"Failed to batch verify True API for unified reconciliation: {e}")

    # 5.5. Сбор КИЗов с уже отправленными или выполненными операциями маркировки
    stmt_ops = select(KizOperation.kiz_code, KizOperation.operation).where(
        KizOperation.seller_id == seller.id,
        KizOperation.operation.in_([KizOperationType.WITHDRAWAL, KizOperationType.RETURN]),
        KizOperation.status.in_(["SUCCESS", "IN_PROGRESS"]),
        (KizOperation.cz_doc_status.is_(None) | (KizOperation.cz_doc_status != "CHECKED_NOT_OK")),
    )
    res_ops = await db.execute(stmt_ops)
    withdrawn_cises, returned_cises = set(), set()
    for k_code, op_type in res_ops.all():
        if k_code:
            c = parse_kiz_code(k_code).get("clean_cis") or k_code
            (withdrawn_cises if op_type == KizOperationType.WITHDRAWAL else returned_cises).add(c)

    # 6. Формирование выбытий (WITHDRAWALS)
    (
        withdrawals_payload,
        sales_needing_count,
        sales_already_withdrawn_count,
        sales_wb_owned_count,
        sales_foreign_count,
    ) = build_unified_withdrawals_payload(
        sales_candidates=sales_candidates,
        cz_info_map=cz_info_map,
        fbs_order_lookup=fbs_order_lookup,
        history_by_cis=history_by_cis,
        seller_inn=seller_inn,
        now_utc=now_utc,
        withdrawn_cises=withdrawn_cises,
    )

    # 7. Формирование возвратов (RETURNS)
    (
        returns_payload,
        seller_owned_direct_count,
        wb_owned_remarking_count,
        foreign_remarking_count,
        already_in_circ_count,
    ) = build_unified_returns_payload(
        return_candidates=return_candidates,
        cz_info_map=cz_info_map,
        fbs_order_lookup=fbs_order_lookup,
        seller_inn=seller_inn,
        now_utc=now_utc,
        returned_cises=returned_cises,
    )

    # 8. Сводка пакета
    summary = {
        "period_days": days,
        "total_unique_cises_scanned": len(history_by_cis),
        "sales_candidates_count": len(sales_candidates),
        "sales_excise_report_count": excise_count,
        "sales_needing_withdrawal": sales_needing_count,
        "sales_already_withdrawn": sales_already_withdrawn_count,
        "sales_wb_owned_count": sales_wb_owned_count,
        "sales_foreign_count": sales_foreign_count,
        "return_candidates_count": len(return_candidates),
        "resold_after_return_count": resold_after_return_count,
        "returns_needing_cz_return": seller_owned_direct_count,
        "returns_already_in_circulation": already_in_circ_count,
        "seller_owned_direct_count": seller_owned_direct_count,
        "wb_owned_remarking_count": wb_owned_remarking_count,
        "foreign_belarus_remarking_count": foreign_remarking_count,
    }

    # 9. Сохранение в KizSignatureBatch
    for old_b in pending_batches[1:]:
        old_b.status = BatchStatus.CANCELLED

    date_str = now_utc.strftime("%Y%m%d_%H%M%S")
    if pending_batches:
        batch = pending_batches[0]
        batch.filename = f"Единая_сверка_маркировки_{date_str}.xlsx"
        batch.source = "unified_reconciliation"
        batch.sales_count = sales_needing_count
        batch.returns_count = seller_owned_direct_count
        batch.already_withdrawn_count = sales_already_withdrawn_count
        batch.total_count = len(withdrawals_payload) + len(returns_payload)
        batch.data_payload = {"summary": summary, "withdrawals": withdrawals_payload, "returns": returns_payload}
        batch.submission_results = None
    else:
        batch = KizSignatureBatch(
            id=str(uuid.uuid4()), seller_id=seller.id,
            filename=f"Единая_сверка_маркировки_{date_str}.xlsx",
            source="unified_reconciliation", status=BatchStatus.PENDING_SIGNATURE,
            sales_count=sales_needing_count, returns_count=seller_owned_direct_count,
            already_withdrawn_count=sales_already_withdrawn_count,
            total_count=len(withdrawals_payload) + len(returns_payload),
            data_payload={"summary": summary, "withdrawals": withdrawals_payload, "returns": returns_payload},
        )
        db.add(batch)

    await db.commit()

    # Аудит-лог
    audit = AuditLog(
        seller_id=seller.id,
        agent="unified_kiz_batch_service",
        action="UNIFIED_RECONCILIATION_BATCH",
        entity_type="kiz_signature_batch",
        entity_id=batch.id,
        payload={"batch_id": batch.id, "sales_needing": sales_needing_count, "returns_needing": seller_owned_direct_count, "wb_remarking": wb_owned_remarking_count, "foreign_remarking": foreign_remarking_count},
        trace_id=str(uuid.uuid4()),
        created_at=now_utc,
    )
    db.add(audit)
    await db.commit()

    return {"success": True, "batch_id": batch.id, "summary": summary}
