"""FastAPI WB Item Returns Endpoints — WB FBS Manager (< 100 lines)."""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.kiz import KizSignatureBatch
from app.models.seller import Seller
from app.services import wb_item_returns_service

logger = logging.getLogger(__name__)
router = APIRouter()


def _resolve_days(days: Optional[int], date_from: Optional[str], date_to: Optional[str]) -> int:
    if date_from and date_to:
        try:
            d_from = datetime.strptime(date_from.strip(), "%Y-%m-%d").date()
            d_to = datetime.strptime(date_to.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Неверный формат даты, ожидается YYYY-MM-DD")
        if d_to < d_from:
            raise HTTPException(status_code=400, detail="date_to не может быть раньше date_from")
        diff = (d_to - d_from).days
        if diff < 0 or diff > 90:
            raise HTTPException(status_code=400, detail="Период выборки должен быть от 1 до 90 дней")
        return max(1, diff)
    if days is not None:
        if days < 1 or days > 90:
            raise HTTPException(status_code=400, detail="Параметр days должен быть в диапазоне от 1 до 90")
        return days
    return 14


@router.post("/item-returns/sync")
async def sync_item_returns(
    seller_id: str,
    days: Optional[int] = Query(default=14, ge=1, le=90),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")
    if not seller.wb_api_token_encrypted:
        raise HTTPException(status_code=400, detail="Токен WB API не настроен для данного продавца")

    resolved_days = _resolve_days(days=days, date_from=date_from, date_to=date_to)
    try:
        res = await wb_item_returns_service.sync_seller_returns(db=db, seller_id=seller_id, days=resolved_days)
        if isinstance(res, dict) and "success" not in res:
            res["success"] = True
        return res
    except Exception as exc:
        logger.error(f"[Item Returns API] Sync error for seller {seller_id}: {exc}")
        raise HTTPException(status_code=500, detail=f"Ошибка синхронизации возвратов WB: {str(exc)}")


@router.get("/item-returns/history")
async def get_item_returns_history(
    seller_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> List[Dict[str, Any]]:
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    stmt = (
        select(KizSignatureBatch)
        .where(KizSignatureBatch.seller_id == seller_id, KizSignatureBatch.source.in_(["item_returns", "wb_item_returns"]))
        .order_by(KizSignatureBatch.created_at.desc())
        .limit(limit)
    )
    res = await db.execute(stmt)
    batches = res.scalars().all()
    return [
        {
            "id": b.id, "filename": b.filename, "source": b.source,
            "status": b.status.value if hasattr(b.status, "value") else str(b.status),
            "sales_count": b.sales_count, "returns_count": b.returns_count,
            "already_withdrawn_count": b.already_withdrawn_count, "total_count": b.total_count,
            "error_message": b.error_message,
            "signed_at": b.signed_at.isoformat() if b.signed_at else None,
            "signed_by": b.signed_by,
            "created_at": b.created_at.isoformat() if b.created_at else None,
        }
        for b in batches
    ]
