"""
FastAPI WB Finance Endpoints — WB FBS Manager
Сбор, хранение и аудит детальных финансовых отчетов реализации WB (POST /api/finance/v1/sales-reports/detailed).
Статистика по продажам, возвратам и контроль принадлежности КИЗ возвратов в Честном Знаке.
"""
import logging
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select

from app.database import get_db
from app.models.seller import Seller
from app.models.wb_finance import WbSalesReportRow
from app.services.wb_finance_service import sync_seller_financial_reports

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sellers/{seller_id}/finance", tags=["WB Finance"])


@router.post("/sync")
async def sync_financial_reports(
    seller_id: str,
    days: int = Query(default=30, ge=1, le=180, description="Период в днях для выборки отчета (1-180)"),
    verify_cz: bool = Query(default=True, description="Выполнять ли онлайн-сверку принадлежности КИЗ возвратов в Честном Знаке"),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """
    Запуск синхронизации детального финансового отчета реализации WB:
    1. Запрашивает POST /api/finance/v1/sales-reports/detailed за указанный период.
    2. Сохраняет и обновляет строки отчета в БД (продажи, возвраты, логистика, штрафы).
    3. Сверяет КИЗ из возвратов с True API ГИС МТ на предмет принадлежности юрлицу.
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    if not seller.wb_api_token_encrypted:
        raise HTTPException(status_code=400, detail="Токен WB API не настроен для данного продавца")

    try:
        result = await sync_seller_financial_reports(
            seller=seller,
            db=db,
            days=days,
            verify_cz=verify_cz,
        )
        return result
    except Exception as exc:
        logger.error(f"[WB Finance API] Error for seller {seller_id}: {exc}")
        raise HTTPException(status_code=500, detail=f"Ошибка синхронизации финансового отчета WB: {str(exc)}")


@router.get("/summary")
async def get_financial_summary(
    seller_id: str,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """
    Возвращает общую финансовую статистику по магазину из сохраненных строк отчетов реализации.
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    # Общие счетчики
    total_rows = await db.scalar(
        select(func.count(WbSalesReportRow.id)).where(WbSalesReportRow.seller_id == seller_id)
    ) or 0

    # Продажи
    sales_stmt = select(
        func.count(WbSalesReportRow.id),
        func.sum(WbSalesReportRow.retail_amount),
        func.count().filter(WbSalesReportRow.clean_cis.isnot(None)),
    ).where(
        WbSalesReportRow.seller_id == seller_id,
        (WbSalesReportRow.doc_type_name == "Продажа") | (WbSalesReportRow.seller_oper_name == "Продажа"),
    )
    sales_res = (await db.execute(sales_stmt)).one()
    sales_count = sales_res[0] or 0
    sales_amount = float(sales_res[1] or 0.0)
    sales_with_kiz = sales_res[2] or 0

    # Возвраты
    returns_stmt = select(
        func.count(WbSalesReportRow.id),
        func.sum(WbSalesReportRow.retail_amount),
        func.count().filter(WbSalesReportRow.clean_cis.isnot(None)),
        func.count().filter(WbSalesReportRow.is_seller_owner == True),
    ).where(
        WbSalesReportRow.seller_id == seller_id,
        (WbSalesReportRow.doc_type_name == "Возврат") | (WbSalesReportRow.seller_oper_name == "Возврат") | (WbSalesReportRow.return_amount > 0),
    )
    returns_res = (await db.execute(returns_stmt)).one()
    returns_count = returns_res[0] or 0
    returns_amount = float(returns_res[1] or 0.0)
    returns_with_kiz = returns_res[2] or 0
    seller_owned_returns = returns_res[3] or 0

    # Даты отчетов
    dates_stmt = select(
        func.min(WbSalesReportRow.rr_date),
        func.max(WbSalesReportRow.rr_date),
    ).where(WbSalesReportRow.seller_id == seller_id)
    dates_res = (await db.execute(dates_stmt)).one()
    min_date = str(dates_res[0]) if dates_res[0] else None
    max_date = str(dates_res[1]) if dates_res[1] else None

    return {
        "seller_id": seller_id,
        "total_records": total_rows,
        "date_range": {"min_date": min_date, "max_date": max_date},
        "sales": {
            "count": sales_count,
            "total_amount": round(sales_amount, 2),
            "with_kiz_count": sales_with_kiz,
        },
        "returns": {
            "count": returns_count,
            "total_amount": round(returns_amount, 2),
            "with_kiz_count": returns_with_kiz,
            "seller_owned_kiz_count": seller_owned_returns,
            "wb_owned_kiz_count": returns_with_kiz - seller_owned_returns,
        },
    }


@router.get("/returns")
async def list_financial_returns(
    seller_id: str,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    kiz_only: bool = Query(default=False, description="Только позиции с кодом маркировки"),
    seller_owned_only: bool = Query(default=False, description="Только КИЗ, числящиеся за юрлицом продавца"),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """
    Возвращает список возвращенных товаров с информацией о коде маркировки и статусе в Честном Знаке.
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    conditions = [
        WbSalesReportRow.seller_id == seller_id,
        (WbSalesReportRow.doc_type_name == "Возврат") | (WbSalesReportRow.seller_oper_name == "Возврат") | (WbSalesReportRow.return_amount > 0),
    ]

    if kiz_only:
        conditions.append(WbSalesReportRow.clean_cis.isnot(None))

    if seller_owned_only:
        conditions.append(WbSalesReportRow.is_seller_owner == True)

    total_stmt = select(func.count(WbSalesReportRow.id)).where(*conditions)
    total_count = await db.scalar(total_stmt) or 0

    offset = (page - 1) * limit
    stmt = (
        select(WbSalesReportRow)
        .where(*conditions)
        .order_by(WbSalesReportRow.rr_date.desc().nullslast(), WbSalesReportRow.rrd_id.desc())
        .offset(offset)
        .limit(limit)
    )
    res = await db.execute(stmt)
    rows = res.scalars().all()

    items = []
    for r in rows:
        items.append({
            "id": r.id,
            "rrd_id": r.rrd_id,
            "rr_date": str(r.rr_date) if r.rr_date else None,
            "srid": r.srid,
            "nm_id": r.nm_id,
            "vendor_code": r.vendor_code,
            "brand_name": r.brand_name,
            "subject_name": r.subject_name,
            "tech_size": r.tech_size,
            "barcode": r.barcode,
            "retail_amount": float(r.retail_amount or 0.0),
            "doc_type_name": r.doc_type_name,
            "seller_oper_name": r.seller_oper_name,
            "kiz": r.kiz,
            "clean_cis": r.clean_cis,
            "cz_status": r.cz_status,
            "cz_owner_inn": r.cz_owner_inn,
            "cz_owner_name": r.cz_owner_name,
            "is_seller_owner": r.is_seller_owner,
            "cz_checked_at": r.cz_checked_at.isoformat() if r.cz_checked_at else None,
        })

    return {
        "page": page,
        "limit": limit,
        "total": total_count,
        "items": items,
    }
