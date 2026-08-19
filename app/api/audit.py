from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional, List
from app.database import get_db
from app.models.audit import AuditLog

router = APIRouter(tags=["audit"])

@router.get("/sellers/{seller_id}/audit")
@router.get("/audit")
async def list_audit_logs(
    seller_id: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db)
):
    query = select(AuditLog)
    if seller_id and seller_id != "all":
        query = query.where(AuditLog.seller_id == seller_id)
        
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    
    query = query.order_by(AuditLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    logs = result.scalars().all()
    
    items = []
    for log in logs:
        items.append({
            "id": log.id,
            "seller_id": log.seller_id,
            "agent": log.agent,
            "action": log.action,
            "entity_type": log.entity_type,
            "entity_id": log.entity_id,
            "payload": log.payload,
            "error": log.error,
            "trace_id": log.trace_id,
            "created_at": log.created_at.isoformat() if log.created_at else None
        })
        
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }
