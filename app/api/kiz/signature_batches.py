"""
FastAPI KIZ Signature Batches Queue Endpoints — WB FBS Manager
"""
import logging
from typing import Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.seller import Seller
from app.models.kiz import KizSignatureBatch, BatchStatus
from app.services.signature_batch_executor import (
    sync_batch_with_cz_data,
    build_batch_signing_payloads,
    execute_signed_batch_submission,
)
from app.services.unified_kiz_batch_service import create_unified_kiz_signature_batch

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/kiz/signature-batches")
async def list_signature_batches(
    seller_id: str,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Возвращает список пакетов операций с маркировкой.
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    stmt = select(KizSignatureBatch).where(KizSignatureBatch.seller_id == seller_id)
    if status:
        try:
            enum_status = BatchStatus(status.upper())
            stmt = stmt.where(KizSignatureBatch.status == enum_status)
        except ValueError:
            pass

    stmt = stmt.order_by(KizSignatureBatch.created_at.desc()).limit(50)
    res = await db.execute(stmt)
    batches = res.scalars().all()

    return [
        {
            "id": b.id,
            "filename": b.filename,
            "source": b.source,
            "status": b.status.value,
            "sales_count": b.sales_count,
            "returns_count": b.returns_count,
            "already_withdrawn_count": b.already_withdrawn_count,
            "total_count": b.total_count,
            "error_message": b.error_message,
            "signed_at": b.signed_at.isoformat() if b.signed_at else None,
            "signed_by": b.signed_by,
            "created_at": b.created_at.isoformat() if b.created_at else None,
        }
        for b in batches
    ]


@router.get("/kiz/signature-batches/{batch_id}")
async def get_signature_batch(
    seller_id: str,
    batch_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Возвращает подробные данные пакета (списки выбытия и возврата).
    """
    batch = await db.get(KizSignatureBatch, batch_id)
    if not batch or str(batch.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Пакет не найден")

    return {
        "id": batch.id,
        "filename": batch.filename,
        "source": batch.source,
        "status": batch.status.value,
        "sales_count": batch.sales_count,
        "returns_count": batch.returns_count,
        "already_withdrawn_count": batch.already_withdrawn_count,
        "total_count": batch.total_count,
        "data_payload": batch.data_payload,
        "submission_results": batch.submission_results,
        "error_message": batch.error_message,
        "signed_at": batch.signed_at.isoformat() if batch.signed_at else None,
        "signed_by": batch.signed_by,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
    }


@router.post("/kiz/signature-batches/{batch_id}/sync-cz")
async def sync_signature_batch_cz(
    seller_id: str,
    batch_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Принудительная живая сверка всех кодов маркировки пакета с True API Честного Знака.
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    batch = await db.get(KizSignatureBatch, batch_id)
    if not batch or str(batch.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Пакет не найден")

    return await sync_batch_with_cz_data(seller=seller, batch=batch, db=db)


@router.post("/kiz/signature-batches/{batch_id}/prepare-documents")
async def prepare_batch_documents_for_signing(
    seller_id: str,
    batch_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    db: AsyncSession = Depends(get_db)
):
    """
    Формирует неподписанные канонические документы (LK_RECEIPT и LP_RETURN)
    для последующего подписания через КриптоПро ЭЦП Browser Plugin.
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    batch = await db.get(KizSignatureBatch, batch_id)
    if not batch or str(batch.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Пакет не найден")

    selected_kiz_list = payload.get("selected_kiz_codes") if payload else None
    return build_batch_signing_payloads(seller=seller, batch=batch, selected_kiz_list=selected_kiz_list)


@router.post("/kiz/signature-batches/{batch_id}/submit-signed")
async def submit_signed_batch(
    seller_id: str,
    batch_id: str,
    payload: Dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Принимает подписанные браузерным плагином документы или запускает серверную обработку пакета.
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    batch = await db.get(KizSignatureBatch, batch_id)
    if not batch or str(batch.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Пакет не найден")

    return await execute_signed_batch_submission(seller=seller, batch=batch, payload=payload, db=db)


@router.post("/kiz/signature-batches/unified-reconcile")
async def trigger_unified_batch_reconcile(
    seller_id: str,
    payload: Dict[str, Any] = Body(default={}),
    db: AsyncSession = Depends(get_db)
):
    """
    Единая точка входа сверки: объединяет FBS заказы, FBO продажи со склада WB,
    и финансовые отчеты за 90 дней в один консолидированный пакет.
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    days = int(payload.get("days", 90))
    sync_finance = bool(payload.get("sync_finance_api", False))

    res = await create_unified_kiz_signature_batch(
        seller=seller,
        db=db,
        days=days,
        sync_finance_api=sync_finance,
    )
    return res


@router.delete("/kiz/signature-batches/{batch_id}")
async def cancel_signature_batch(
    seller_id: str,
    batch_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Отменяет пакет из очереди на подписание."""
    batch = await db.get(KizSignatureBatch, batch_id)
    if not batch or str(batch.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Пакет не найден")

    batch.status = BatchStatus.CANCELLED
    await db.commit()
    return {"success": True, "message": f"Пакет {batch_id} отменен"}
