import logging
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from typing import List, Optional
import uuid
from datetime import datetime, timezone

from app.database import get_db
from app.models.seller import Seller
from app.models.supply import Supply, SupplyStatus
from app.models.order import Order, OrderStatus
from app.models.audit import AuditLog
from app.services.encryption import decrypt
from app.services.wb_client import WBClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sellers/{seller_id}/supplies", tags=["supplies"])

@router.get("")
async def list_supplies(seller_id: str, db: AsyncSession = Depends(get_db)):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")
        
    result = await db.execute(
        select(Supply)
        .where(Supply.seller_id == seller_id)
        .order_by(Supply.created_at.desc())
    )
    supplies = result.scalars().all()
    
    items = []
    for s in supplies:
        # count orders for this supply
        orders_cnt = await db.scalar(
            select(func.count(Order.id)).where(
                and_(
                    Order.seller_id == seller_id,
                    or_(
                        Order.supply_id == s.id,
                        Order.wb_supply_id == s.wb_supply_id
                    )
                )
            )
        ) or 0
        
        items.append({
            "id": str(s.id),
            "wb_supply_id": s.wb_supply_id,
            "name": s.name or f"Поставка {s.wb_supply_id}",
            "status": s.status.value,
            "orders_count": orders_cnt,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "closed_at": s.closed_at.isoformat() if s.closed_at else None,
        })
        
    return {"items": items}


@router.post("/sync")
@router.post("/refresh")
async def sync_supplies(seller_id: str, db: AsyncSession = Depends(get_db)):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")
        
    token = decrypt(seller.wb_api_token_encrypted)
    if not token:
        raise HTTPException(status_code=400, detail="Токен WB API не настроен")
        
    client = WBClient(token)
    new_count = 0
    updated_count = 0
    now = datetime.now(timezone.utc)
    try:
        sup_data = await client.get_supplies(limit=100)
        for s_raw in sup_data.get("supplies", []):
            wb_sup_id = s_raw.get("id")
            if not wb_sup_id:
                continue
            res_s = await db.execute(
                select(Supply).where(
                    Supply.seller_id == seller.id,
                    Supply.wb_supply_id == wb_sup_id
                )
            )
            sup_obj = res_s.scalars().first()
            sup_name = s_raw.get("name") or f"Поставка {wb_sup_id}"
            is_done = s_raw.get("done", False)
            closed_at_str = s_raw.get("closedAt")
            created_at_str = s_raw.get("createdAt")

            c_at = None
            if created_at_str:
                try:
                    c_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                    if c_at.tzinfo is None:
                        c_at = c_at.replace(tzinfo=timezone.utc)
                except Exception:
                    pass
            cl_at = None
            if closed_at_str:
                try:
                    cl_at = datetime.fromisoformat(closed_at_str.replace("Z", "+00:00"))
                    if cl_at.tzinfo is None:
                        cl_at = cl_at.replace(tzinfo=timezone.utc)
                except Exception:
                    pass

            sup_st = SupplyStatus.DONE if is_done else (SupplyStatus.CLOSED if cl_at else SupplyStatus.CREATED)

            if not sup_obj:
                sup_obj = Supply(
                    id=uuid.uuid4(),
                    seller_id=str(seller.id),
                    wb_supply_id=wb_sup_id,
                    name=sup_name,
                    status=sup_st,
                    created_at=c_at or now,
                    closed_at=cl_at,
                )
                db.add(sup_obj)
                await db.flush()
                new_count += 1
            else:
                sup_obj.status = sup_st
                sup_obj.closed_at = cl_at
                updated_count += 1

            # Link orders with this wb_supply_id
            orders_res = await db.execute(
                select(Order).where(
                    Order.seller_id == seller.id,
                    Order.wb_supply_id == wb_sup_id
                )
            )
            for o in orders_res.scalars().all():
                o.supply_id = sup_obj.id

        audit = AuditLog(
            seller_id=seller_id,
            agent="supplies_api",
            action="SYNC_SUPPLIES",
            entity_type="seller",
            entity_id=str(seller_id),
            payload={
                "message": f"Синхронизация поставок: +{new_count} новых, {updated_count} обновлено",
                "new_count": new_count,
                "updated_count": updated_count
            }
        )
        db.add(audit)
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"Error in sync_supplies for seller {seller_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Ошибка синхронизации поставок: {str(e)}")
    finally:
        await client.close()

    return {
        "success": True,
        "message": f"Синхронизировано поставок: +{new_count} новых, {updated_count} обновлено",
        "new_count": new_count,
        "updated_count": updated_count
    }

@router.post("")
async def create_supply(
    seller_id: str, 
    order_ids: Optional[List[int]] = Body(default=[]), 
    name: Optional[str] = Body(default=None),
    db: AsyncSession = Depends(get_db)
):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")
        
    wb_sup_id = f"WB-SUP-{uuid.uuid4().hex[:8].upper()}"
    supply_name = name or f"Поставка от {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    
    supply = Supply(
        seller_id=seller_id,
        wb_supply_id=wb_sup_id,
        name=supply_name,
        status=SupplyStatus.CREATED
    )
    db.add(supply)
    await db.flush()
    
    # Link orders if provided
    attached_count = 0
    if order_ids:
        for oid in order_ids:
            order = await db.get(Order, oid)
            if order and str(order.seller_id) == str(seller_id):
                order.supply_id = supply.id
                order.wb_supply_id = wb_sup_id
                attached_count += 1
                
    # Audit log
    audit = AuditLog(
        seller_id=seller_id,
        agent="supplies_api",
        action="CREATE_SUPPLY",
        entity_type="supply",
        entity_id=str(supply.id),
        payload={"wb_supply_id": wb_sup_id, "name": supply_name, "attached_orders": attached_count}
    )
    db.add(audit)
    
    await db.commit()
    await db.refresh(supply)
    
    return {
        "id": str(supply.id),
        "wb_supply_id": supply.wb_supply_id,
        "name": supply.name,
        "status": supply.status.value,
        "orders_count": attached_count,
        "message": "Поставка успешно создана"
    }

@router.get("/{supply_id}")
async def get_supply(seller_id: str, supply_id: str, db: AsyncSession = Depends(get_db)):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")
        
    supply = await db.get(Supply, supply_id)
    if not supply or str(supply.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Supply not found")
        
    orders_res = await db.execute(select(Order).where(Order.supply_id == supply.id))
    orders = orders_res.scalars().all()
    
    return {
        "id": str(supply.id),
        "wb_supply_id": supply.wb_supply_id,
        "name": supply.name,
        "status": supply.status.value,
        "created_at": supply.created_at.isoformat() if supply.created_at else None,
        "orders": [
            {
                "id": o.id,
                "article": o.article,
                "name": o.name,
                "status": o.status.value,
                "kiz_status": o.kiz_status.value
            } for o in orders
        ]
    }

@router.get("/{supply_id}/barcode")
async def get_supply_barcode(seller_id: str, supply_id: str, db: AsyncSession = Depends(get_db)):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")
        
    supply = await db.get(Supply, supply_id)
    wb_sup_id = supply.wb_supply_id if supply else supply_id
    
    # Return barcode payload / representation
    return {
        "supply_id": supply_id,
        "wb_supply_id": wb_sup_id,
        "barcode_text": wb_sup_id,
        "svg_base64": None,
        "message": f"Штрихкод поставки {wb_sup_id} сформирован"
    }


@router.post("/create-from-pending")
async def create_supply_from_pending(
    seller_id: str,
    supply_name: Optional[str] = Body(default=None, embed=True),
    db: AsyncSession = Depends(get_db),
):
    """
    Создаёт поставку из всех необработанных заказов (NEW / ASSEMBLING без supply_id).

    Используется кнопкой «📦 Сформировать поставку» в Telegram-дайджесте.
    Запускает задачу supply_agent.create_supply_for_seller асинхронно.

    Returns:
        task_id: Celery task ID для отслеживания статуса.
        pending_count: сколько заказов передано в задачу.
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")

    # Fetch all pending order IDs
    pending_statuses = [OrderStatus.NEW, OrderStatus.ASSEMBLING]
    result = await db.execute(
        select(Order.id).where(
            and_(
                Order.seller_id == seller_id,
                Order.status.in_(pending_statuses),
                Order.supply_id.is_(None),
            )
        )
    )
    order_ids = [row[0] for row in result.all()]

    if not order_ids:
        raise HTTPException(
            status_code=409,
            detail="Нет необработанных заказов для формирования поставки.",
        )

    auto_name = supply_name or f"WB FBS {datetime.now().strftime('%d.%m.%Y %H:%M')}"

    # Dispatch Celery task (existing supply_agent)
    from app.agents.supply_agent import create_supply_for_seller
    task = create_supply_for_seller.delay(
        seller_id=seller_id,
        order_ids=order_ids,
        supply_name=auto_name,
    )

    # Audit log
    audit = AuditLog(
        seller_id=seller_id,
        agent="supplies_api",
        action="CREATE_SUPPLY_FROM_PENDING",
        entity_type="supply",
        entity_id=task.id,
        payload={
            "order_ids": order_ids,
            "pending_count": len(order_ids),
            "supply_name": auto_name,
            "task_id": task.id,
        },
    )
    db.add(audit)
    await db.commit()

    return {
        "message": f"Поставка формируется: {len(order_ids)} заказов",
        "task_id": task.id,
        "pending_count": len(order_ids),
        "supply_name": auto_name,
    }
