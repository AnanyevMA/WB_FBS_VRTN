"""
FastAPI WB Warehouse Sales Endpoints — WB FBS Manager
Маркировка: синхронизация и вывод из оборота повторных продаж со склада WB.
"""
import logging
from typing import Any, Dict
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.seller import Seller
from app.models.kiz import KizSignatureBatch
from app.services.wb_warehouse_sales_service import process_warehouse_sales_for_seller

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/warehouse-sales/sync")
async def sync_warehouse_sales(
    seller_id: str,
    days: int = Query(default=14, ge=1, le=90, description="Период в днях для выборки отчета"),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """
    Запуск сверки продаж со склада WB (FBO / остатки после возвратов):
    1. Запрашивает POST /api/v1/analytics/excise-report
    2. Проверяет статусы КИЗ в Честном Знаке
    3. Создает пакет на подпись для кодов, требующих вывода
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    if not seller.wb_api_token_encrypted:
        raise HTTPException(status_code=400, detail="Токен WB API не настроен для данного продавца")

    try:
        result = await process_warehouse_sales_for_seller(seller=seller, db=db, days=days)
        return result
    except Exception as exc:
        logger.error(f"[Warehouse Sales API] Error for seller {seller_id}: {exc}")
        raise HTTPException(status_code=500, detail=f"Ошибка обработки продаж со склада WB: {str(exc)}")


@router.get("/warehouse-sales/batches")
async def list_warehouse_sales_batches(
    seller_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Получение списка пакетов, сформированных по продажам со склада WB."""
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    stmt = (
        select(KizSignatureBatch)
        .where(
            KizSignatureBatch.seller_id == seller_id,
            KizSignatureBatch.source == "wb_warehouse_sale",
        )
        .order_by(KizSignatureBatch.created_at.desc())
        .limit(limit)
    )
    res = await db.execute(stmt)
    batches = res.scalars().all()

    return {
        "success": True,
        "count": len(batches),
        "batches": [
            {
                "id": b.id,
                "filename": b.filename,
                "status": b.status.value,
                "sales_count": b.sales_count,
                "already_withdrawn_count": b.already_withdrawn_count,
                "created_at": b.created_at.isoformat() if b.created_at else None,
                "signed_at": b.signed_at.isoformat() if b.signed_at else None,
            }
            for b in batches
        ],
    }
