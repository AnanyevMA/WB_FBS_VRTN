"""
FastAPI KIZ Attach, Lookup & Validation Endpoints — WB FBS Manager
"""
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.seller import Seller
from app.models.order import Order, KizStatus, OrderStatus
from app.models.kiz import KizOperation, KizOperationType
from app.models.audit import AuditLog
from app.schemas.order import KIZAttachRequest

router = APIRouter()


@router.post("/orders/{order_id}/kiz")
@router.post("/kiz/attach")
async def attach_kiz(
    seller_id: str,
    order_id: Optional[int] = None,
    req: KIZAttachRequest = Body(...),
    db: AsyncSession = Depends(get_db)
):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")
        
    kiz_code = req.kiz_code.strip()
    if len(kiz_code) < 10:
        raise HTTPException(status_code=400, detail="Неверный формат КИЗ / SGTIN")
        
    # If order_id not specified or 0, auto-find active order needing KIZ
    target_order = None
    if order_id and order_id > 0:
        target_order = await db.get(Order, order_id)
    
    if not target_order:
        res = await db.execute(
            select(Order)
            .where(
                Order.seller_id == seller_id,
                Order.kiz_status == KizStatus.PENDING,
                Order.status.in_([OrderStatus.NEW, OrderStatus.ASSEMBLING])
            )
            .order_by(Order.created_at.asc())
        )
        target_order = res.scalars().first()

    if not target_order:
        # If still no order, look for any order by seller
        res = await db.execute(
            select(Order)
            .where(Order.seller_id == seller_id)
            .order_by(Order.created_at.desc())
        )
        target_order = res.scalars().first()

    if not target_order:
        raise HTTPException(status_code=404, detail="Не найден подходящий заказ для привязки КИЗ")

    # Update order
    target_order.kiz_code = kiz_code
    target_order.kiz_status = KizStatus.ATTACHED
    target_order.kiz_attached_at = datetime.now(timezone.utc)
    
    # Resolve product info and cross-check
    from app.services.kiz_service import resolve_kiz_product_info
    kiz_info = await resolve_kiz_product_info(
        kiz_code=kiz_code,
        seller=seller,
        order=target_order,
        db=db,
        force_refresh=True
    )

    # Synchronize CZ status and error checking
    target_order.kiz_cz_status = kiz_info.cz_status
    target_order.kiz_cz_status_updated_at = datetime.now(timezone.utc)
    if not kiz_info.is_valid:
        target_order.kiz_status = KizStatus.ERROR
    else:
        target_order.kiz_status = KizStatus.ATTACHED

    # Audit log
    audit = AuditLog(
        seller_id=seller_id,
        agent="kiz_scanner",
        action="ATTACH_KIZ",
        entity_type="order",
        entity_id=str(target_order.id),
        payload={
            "kiz_code": kiz_code,
            "order_id": target_order.id,
            "gtin": kiz_info.gtin,
            "product_name": kiz_info.product_name,
            "cz_status": kiz_info.cz_status,
            "is_valid": kiz_info.is_valid,
            "validation_message": kiz_info.validation_message,
        }
    )
    db.add(audit)
    
    await db.commit()
    
    raw_cz = kiz_info.raw_cz_payload or {}
    ogvs = raw_cz.get("ogvs") or []

    return {
        "success": True,
        "message": f"КИЗ успешно прикреплен к заказу #{target_order.id}" if kiz_info.is_valid else f"КИЗ прикреплен к заказу #{target_order.id} (Обнаружены ошибки: {kiz_info.validation_message})",
        "order_id": target_order.id,
        "kiz_code": kiz_code,
        "kiz_status": target_order.kiz_status.value,
        "product_info": {
            "gtin": kiz_info.gtin,
            "product_name": kiz_info.product_name,
            "article": kiz_info.article,
            "tech_size": kiz_info.tech_size,
            "wb_size": kiz_info.wb_size,
            "cz_status": kiz_info.cz_status,
            "cz_status_ex": kiz_info.cz_status_ex,
            "ogvs": ogvs,
            "blocked_by_ogv": len(ogvs) > 0,
            "is_valid": kiz_info.is_valid,
            "validation_message": kiz_info.validation_message,
        }
    }


@router.post("/kiz/lookup")
async def lookup_kiz(
    seller_id: str,
    req: KIZAttachRequest = Body(...),
    db: AsyncSession = Depends(get_db)
):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")
        
    raw_kiz = req.kiz_code.strip()
    if not raw_kiz:
        raise HTTPException(status_code=400, detail="Не указан код КИЗ")

    from app.services.kiz_service import resolve_kiz_product_info
    kiz_info = await resolve_kiz_product_info(
        kiz_code=raw_kiz,
        seller=seller,
        db=db,
        force_refresh=True
    )
    
    raw_cz = kiz_info.raw_cz_payload or {}
    ogvs = raw_cz.get("ogvs") or []

    return {
        "kiz_code": kiz_info.kiz_code,
        "gtin": kiz_info.gtin,
        "serial_number": kiz_info.serial_number,
        "clean_cis": kiz_info.clean_cis,
        "product_name": kiz_info.product_name,
        "brand": kiz_info.brand,
        "article": kiz_info.article,
        "tech_size": kiz_info.tech_size,
        "wb_size": kiz_info.wb_size,
        "tnved": kiz_info.tnved,
        "cz_status": kiz_info.cz_status,
        "cz_status_ex": kiz_info.cz_status_ex,
        "cz_owner_inn": kiz_info.cz_owner_inn,
        "cz_owner_name": kiz_info.cz_owner_name,
        "ogvs": ogvs,
        "blocked_by_ogv": len(ogvs) > 0,
        "is_valid": kiz_info.is_valid,
        "validation_message": kiz_info.validation_message,
        "checked_at": kiz_info.checked_at.isoformat() if kiz_info.checked_at else None
    }


@router.delete("/orders/{order_id}/kiz")
async def detach_kiz(seller_id: str, order_id: int, db: AsyncSession = Depends(get_db)):
    order = await db.get(Order, order_id)
    if not order or str(order.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Заказ не найден")
        
    order.kiz_code = None
    order.kiz_status = KizStatus.PENDING
    order.kiz_cz_status = None
    order.kiz_cz_status_updated_at = None
    order.kiz_attached_at = None
    
    audit = AuditLog(
        seller_id=seller_id,
        agent="kiz_api",
        action="DETACH_KIZ",
        entity_type="order",
        entity_id=str(order_id),
        payload={"order_id": order_id}
    )
    db.add(audit)
    await db.commit()
    return {"message": "КИЗ откреплен", "order_id": order_id}


@router.get("/orders/{order_id}/kiz/validate")
async def validate_kiz(seller_id: str, order_id: int, db: AsyncSession = Depends(get_db)):
    order = await db.get(Order, order_id)
    if not order or str(order.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Заказ не найден")
        
    code = order.kiz_code or ""
    is_valid = len(code) >= 25 and (code.startswith("01") or code.startswith("046"))
    return {
        "valid": is_valid,
        "details": {
            "kiz_code": code,
            "length": len(code),
            "format": "GS1 DataMatrix (SGTIN)" if is_valid else "Неизвестный формат"
        }
    }


@router.get("/kiz/operations")
async def list_kiz_operations(seller_id: str, db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(AuditLog)
        .where(AuditLog.seller_id == seller_id, AuditLog.entity_type.in_(["kiz", "order"]))
        .order_by(AuditLog.created_at.desc())
        .limit(50)
    )
    logs = res.scalars().all()
    
    items = []
    for l in logs:
        items.append({
            "id": l.id,
            "action": l.action,
            "entity_id": l.entity_id,
            "payload": l.payload,
            "created_at": l.created_at.isoformat() if l.created_at else None
        })
    return {"items": items}
