"""
WB Finance Service — WB FBS Manager
Синхронизация детального финансового отчета реализации WB (POST /api/finance/v1/sales-reports/detailed).
Парсинг продаж, возвратов, логистики, сохранение в БД и проверка принадлежности КИЗ возвратов в Честном Знаке.
"""
import gc
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.seller import Seller
from app.models.order import Order, KizStatus
from app.models.wb_finance import WbSalesReportRow
from app.models.audit import AuditLog
from app.services.encryption import decrypt
from app.services.wb_finance_client import WBFinanceClient
from app.services.kiz_service import (
    batch_verify_and_sync_cises,
    parse_kiz_code,
)

logger = logging.getLogger(__name__)


def _parse_iso_datetime(val: Any) -> Optional[datetime]:
    """Безопасно парсит дату-время ISO/RFC3339."""
    if not val or not isinstance(val, str):
        return None
    try:
        return datetime.fromisoformat(val.strip().replace("Z", "+00:00"))
    except Exception:
        return None


def _parse_iso_date(val: Any) -> Optional[date]:
    """Безопасно парсит дату ISO (YYYY-MM-DD)."""
    if not val:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str):
        try:
            return date.fromisoformat(val.strip()[:10])
        except Exception:
            return None
    return None


def _parse_decimal(val: Any, default: Decimal = Decimal("0.00")) -> Decimal:
    """Безопасно преобразует число или строку в Decimal."""
    if val is None or val == "":
        return default
    try:
        return Decimal(str(val).strip().replace(",", "."))
    except Exception:
        return default


def _map_raw_row_to_dict(row: Dict[str, Any], seller_id: str) -> Dict[str, Any]:
    """Нормализует сырую строку отчета WB (camelCase/snake_case) в словарь модели WbSalesReportRow."""
    raw_kiz = (row.get("kiz") or "").strip()
    clean_cis = None
    if raw_kiz:
        parsed_kiz = parse_kiz_code(raw_kiz)
        clean_cis = parsed_kiz.get("clean_cis") or raw_kiz

    rrd_id = int(row.get("rrdId") or row.get("rrd_id") or 0)
    report_id = row.get("reportId") or row.get("realizationreport_id")

    return {
        "seller_id": seller_id,
        "rrd_id": rrd_id,
        "report_id": int(report_id) if report_id else None,
        "gi_id": int(row.get("giId") or row.get("gi_id") or 0) or None,
        "subject_name": row.get("subjectName") or row.get("subject_name"),
        "nm_id": int(row.get("nmId") or row.get("nm_id") or 0) or None,
        "brand_name": row.get("brandName") or row.get("brand_name"),
        "vendor_code": row.get("vendorCode") or row.get("sa_name"),
        "tech_size": row.get("techSize") or row.get("ts_name"),
        "barcode": str(row.get("barcode") or row.get("sku") or "").strip() or None,
        "doc_type_name": row.get("docTypeName") or row.get("doc_type_name"),
        "quantity": int(row.get("quantity") or 0),
        "retail_price": _parse_decimal(row.get("retailPrice") or row.get("retail_price")),
        "retail_amount": _parse_decimal(row.get("retailAmount") or row.get("retail_amount")),
        "sale_percent": _parse_decimal(row.get("salePercent") or row.get("sale_percent")),
        "commission_percent": _parse_decimal(row.get("commissionPercent") or row.get("commission_percent")),
        "office_name": row.get("officeName") or row.get("office_name"),
        "seller_oper_name": row.get("sellerOperName") or row.get("supplier_oper_name"),
        "order_dt": _parse_iso_datetime(row.get("orderDt") or row.get("order_dt")),
        "sale_dt": _parse_iso_datetime(row.get("saleDt") or row.get("sale_dt")),
        "rr_date": _parse_iso_date(row.get("rrDate") or row.get("rr_dt")),
        "shk_id": int(row.get("shkId") or row.get("shk_id") or 0) or None,
        "retail_price_with_disc": _parse_decimal(row.get("retailPriceWithDisc") or row.get("retail_price_withdisc_rub")),
        "delivery_amount": int(row.get("deliveryAmount") or row.get("delivery_amount") or 0),
        "return_amount": int(row.get("returnAmount") or row.get("return_amount") or 0),
        "delivery_rub": _parse_decimal(row.get("deliveryService") or row.get("delivery_rub")),
        "gi_box_type_name": row.get("giBoxTypeName") or row.get("gi_box_type_name"),
        "ppvz_sales_commission": _parse_decimal(row.get("ppvzSalesCommission") or row.get("ppvz_sales_commission")),
        "for_pay": _parse_decimal(row.get("forPay") or row.get("ppvz_for_pay")),
        "ppvz_reward": _parse_decimal(row.get("ppvzReward") or row.get("ppvz_reward_claims")),
        "ppvz_office_name": row.get("ppvzOfficeName") or row.get("ppvz_office_name"),
        "ppvz_supplier_inn": row.get("ppvzSupplierInn") or row.get("ppvz_inn"),
        "kiz": raw_kiz or None,
        "clean_cis": clean_cis,
        "srid": row.get("srid") or row.get("rid"),
        "delivery_method": row.get("deliveryMethod"),
        "date_from": _parse_iso_date(row.get("dateFrom") or row.get("date_from")),
        "date_to": _parse_iso_date(row.get("dateTo") or row.get("date_to")),
        "raw_payload": None,
    }


async def _upsert_sales_report_rows(
    db: AsyncSession,
    seller_id: str,
    row_dicts: List[Dict[str, Any]],
) -> Tuple[int, int]:
    """Идемпотентно сохраняет пачку строк отчета (PostgreSQL / SQLite). Возвращает (inserted, updated)."""
    if not row_dicts:
        return 0, 0

    inserted_count = 0
    updated_count = 0

    is_postgres = "postgres" in str(db.bind.url if db.bind else "")
    if is_postgres:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        for chunk in [row_dicts[i:i + 100] for i in range(0, len(row_dicts), 100)]:
            for item in chunk:
                if "id" not in item:
                    item["id"] = str(uuid.uuid4())

            stmt = pg_insert(WbSalesReportRow).values(chunk)
            update_cols = {
                c.name: getattr(stmt.excluded, c.name)
                for c in WbSalesReportRow.__table__.columns
                if c.name not in ("id", "seller_id", "rrd_id", "created_at")
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=["seller_id", "rrd_id"],
                set_=update_cols,
            )
            await db.execute(stmt)
            inserted_count += len(chunk)
            del stmt
            del update_cols
            del chunk
    else:
        # SQLite / Unit test compatibility fallback
        rrd_ids = [r["rrd_id"] for r in row_dicts]
        res = await db.execute(
            select(WbSalesReportRow).where(
                WbSalesReportRow.seller_id == seller_id,
                WbSalesReportRow.rrd_id.in_(rrd_ids),
            )
        )
        existing_map = {row.rrd_id: row for row in res.scalars().all()}

        for item in row_dicts:
            r_id = item["rrd_id"]
            if r_id in existing_map:
                existing_obj = existing_map[r_id]
                for k, v in item.items():
                    if k not in ("id", "seller_id", "rrd_id"):
                        setattr(existing_obj, k, v)
                updated_count += 1
            else:
                new_obj = WbSalesReportRow(id=str(uuid.uuid4()), **item)
                db.add(new_obj)
                inserted_count += 1
        existing_map.clear()
        del existing_map

    await db.flush()
    return inserted_count, updated_count


async def _verify_and_update_return_cises(
    seller: Seller,
    db: AsyncSession,
    returned_cises: List[str],
) -> Dict[str, Any]:
    """
    Проверяет принадлежность КИЗ из возвратов через True API.
    Обновляет cz_status, cz_owner_inn и флаг is_seller_owner в wb_sales_report_rows.
    """
    if not returned_cises or not seller.cz_inn:
        return {"checked": 0, "seller_owned": 0, "other_owned": 0}

    unique_cises = list(set(returned_cises))
    logger.info(f"[WB Finance] Verifying {len(unique_cises)} return KIZs in True API for seller {seller.id}")

    try:
        cz_info_map = await batch_verify_and_sync_cises(
            seller=seller,
            kiz_codes=unique_cises,
            db=db,
            force_refresh=True,
        )
    except Exception as e:
        logger.warning(f"[WB Finance] True API check error during return KIZ audit: {e}")
        return {"checked": 0, "error": str(e), "seller_owned": 0, "other_owned": 0}

    now_utc = datetime.now(timezone.utc)
    seller_owned = 0
    other_owned = 0

    for code, info in cz_info_map.items():
        if not info:
            continue

        clean_cis = info.clean_cis or code
        owner_inn = (info.cz_owner_inn or "").strip()
        is_owner = (owner_inn == seller.cz_inn.strip()) if seller.cz_inn else False

        if is_owner:
            seller_owned += 1
        else:
            other_owned += 1

        # Обновляем строки отчета с этим clean_cis
        stmt_upd = (
            update(WbSalesReportRow)
            .where(
                WbSalesReportRow.seller_id == seller.id,
                WbSalesReportRow.clean_cis == clean_cis,
            )
            .values(
                cz_status=info.cz_status,
                cz_owner_inn=owner_inn or None,
                cz_owner_name=info.cz_owner_name,
                is_seller_owner=is_owner,
                cz_checked_at=now_utc,
            )
        )
        await db.execute(stmt_upd)

    await db.flush()
    return {
        "checked": len(cz_info_map),
        "seller_owned": seller_owned,
        "other_owned": other_owned,
    }


async def sync_seller_financial_reports(
    seller: Seller,
    db: AsyncSession,
    days: int = 14,
    verify_cz: bool = True,
    trace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Загружает еженедельные детальные отчеты реализации WB, сохраняет в БД и верифицирует КИЗ возвратов.
    """
    if not seller.wb_api_token_encrypted:
        return {
            "success": False,
            "seller_id": seller.id,
            "message": "У продавца отсутствует токен WB API",
        }

    wb_token = decrypt(seller.wb_api_token_encrypted)
    now_utc = datetime.now(timezone.utc)
    date_to = now_utc.strftime("%Y-%m-%dT23:59:59Z")
    date_from = (now_utc - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    effective_trace_id = trace_id or str(uuid.uuid4())

    # Фиксируем статус STARTED перед началом выполнения и сразу коммитим в БД
    audit_entry = AuditLog(
        id=str(uuid.uuid4()),
        seller_id=seller.id,
        agent="wb_finance_service",
        action="SYNC_WB_FINANCIAL_REPORTS",
        entity_type="seller",
        entity_id=seller.id,
        payload={
            "status": "STARTED",
            "days": days,
            "started_at": now_utc.isoformat(),
            "date_from": date_from,
            "date_to": date_to,
        },
        trace_id=effective_trace_id,
        created_at=now_utc,
    )
    db.add(audit_entry)
    await db.commit()

    logger.info(f"[WB Finance] Syncing sales reports for seller {seller.name} ({seller.id}), {date_from}..{date_to}")

    total_rows = 0
    sales_count = 0
    returns_count = 0
    return_cises_to_verify: List[str] = []
    total_inserted = 0
    total_updated = 0

    try:
        async with WBFinanceClient(wb_token) as client:
            async for page in client.fetch_all_sales_reports(date_from=date_from, date_to=date_to, limit=500):
                if not page:
                    continue

                row_dicts = []
                for raw_row in page:
                    parsed_row = _map_raw_row_to_dict(raw_row, seller.id)
                    row_dicts.append(parsed_row)

                    doc_type = (parsed_row.get("doc_type_name") or "").strip()
                    oper_name = (parsed_row.get("seller_oper_name") or "").strip()
                    clean_cis = parsed_row.get("clean_cis")

                    if doc_type == "Продажа" or "Продажа" in oper_name:
                        sales_count += 1
                    elif doc_type == "Возврат" or "Возврат" in oper_name or parsed_row.get("return_amount", 0) > 0:
                        returns_count += 1
                        if clean_cis:
                            return_cises_to_verify.append(clean_cis)

                ins, upd = await _upsert_sales_report_rows(db, seller.id, row_dicts)
                await db.commit()
                total_inserted += ins
                total_updated += upd
                total_rows += len(row_dicts)

                # Очистка промежуточных списков и вызов gc.collect() после каждого коммита пачки в БД
                row_dicts.clear()
                del row_dicts
                del page
                gc.collect()

        # Верификация принадлежности КИЗ возвратов в Честном Знаке
        cz_verification_result = {"checked": 0, "seller_owned": 0, "other_owned": 0}
        if verify_cz and return_cises_to_verify:
            cz_verification_result = await _verify_and_update_return_cises(
                seller=seller,
                db=db,
                returned_cises=return_cises_to_verify,
            )
            await db.commit()
            return_cises_to_verify.clear()
            del return_cises_to_verify
            gc.collect()

        # Обновление статуса на COMPLETED при успешном завершении
        completed_at = datetime.now(timezone.utc)
        audit_entry.payload = {
            "status": "COMPLETED",
            "days": days,
            "total_rows": total_rows,
            "sales_count": sales_count,
            "returns_count": returns_count,
            "returns_with_kiz": cz_verification_result.get("checked", 0),
            "seller_owned_returns": cz_verification_result.get("seller_owned", 0),
            "other_owned_returns": cz_verification_result.get("other_owned", 0),
            "inserted_count": total_inserted,
            "updated_count": total_updated,
            "started_at": now_utc.isoformat(),
            "completed_at": completed_at.isoformat(),
        }
        await db.commit()

        return {
            "success": True,
            "seller_id": seller.id,
            "status": "COMPLETED",
            "total_rows": total_rows,
            "sales_count": sales_count,
            "returns_count": returns_count,
            "returns_with_kiz": cz_verification_result.get("checked", 0),
            "cz_audit": cz_verification_result,
            "inserted_count": total_inserted,
            "updated_count": total_updated,
        }
    except Exception as exc:
        logger.error(f"[WB Finance] Failed sync for seller {seller.id}: {exc}")
        try:
            audit_entry.error = str(exc)
            audit_entry.payload = {
                **(audit_entry.payload or {}),
                "status": "FAILED",
                "failed_at": datetime.now(timezone.utc).isoformat(),
            }
            await db.commit()
        except Exception as log_err:
            logger.error(f"[WB Finance] Failed to update audit log on error: {log_err}")
        raise
