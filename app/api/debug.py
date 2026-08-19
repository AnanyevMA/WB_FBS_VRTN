"""
Debug & Testing Router — Отладочный модуль для симуляции и генерации тестовых данных
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
import uuid
import random
from datetime import datetime, timezone

from app.database import get_db
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.models.supply import Supply
from app.models.kiz import KizOperation, KizOperationType
from app.models.audit import AuditLog
from app.services.encryption import encrypt

router = APIRouter(prefix="/debug", tags=["debug"])


@router.get("/status")
async def get_debug_status(db: AsyncSession = Depends(get_db)):
    """Получить системную статистику для отладки."""
    sellers_count = (await db.execute(select(func.count(Seller.id)))).scalar() or 0
    orders_count = (await db.execute(select(func.count(Order.id)))).scalar() or 0
    supplies_count = (await db.execute(select(func.count(Supply.id)))).scalar() or 0
    kiz_ops_count = (await db.execute(select(func.count(KizOperation.id)))).scalar() or 0
    audit_count = (await db.execute(select(func.count(AuditLog.id)))).scalar() or 0

    return {
        "debug_mode": True,
        "database_stats": {
            "sellers": sellers_count,
            "orders": orders_count,
            "supplies": supplies_count,
            "kiz_operations": kiz_ops_count,
            "audit_logs": audit_count,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/seed-mock-data")
async def seed_mock_data(db: AsyncSession = Depends(get_db)):
    """Сгенерировать тестового селлера и 5 образцовых FBS заказов."""
    # Create or get mock seller
    res = await db.execute(select(Seller).where(Seller.name == "Demo Seller (Debug)"))
    seller = res.scalar_one_or_none()

    if not seller:
        seller = Seller(
            name="Demo Seller (Debug)",
            wb_api_token_encrypted=encrypt("mock-wb-token-debug"),
            wb_supplier_id="DEMO-SUPPLIER-99",
            cz_inn="770000000000",
            cz_token_encrypted=encrypt("mock-cz-token-debug"),
            mod_fias="8ed74f90-0119-48f2-b289-379707934e2f",
            telegram_chat_ids=["123456789"],
            is_active=True,
            polling_enabled=True,
        )
        db.add(seller)
        await db.commit()
        await db.refresh(seller)

    # Seed 5 mock orders
    mock_items = [
        {"subject": "Куртка зимняя", "brand": "Nordic", "price": 4990.00},
        {"subject": "Джинсы приталенные", "brand": "Denim Co", "price": 2990.00},
        {"subject": "Пальто шерстяное", "brand": "Elegant", "price": 8990.00},
        {"subject": "Свитер оверсайз", "brand": "Cozy", "price": 1990.00},
        {"subject": "Худи с капюшоном", "brand": "Streetwear", "price": 2490.00},
    ]

    created_orders = []
    for item in mock_items:
        wb_id = random.randint(10000000, 99999999)
        order = Order(
            id=wb_id,
            seller_id=seller.id,
            status=OrderStatus.NEW,
            wb_created_at=datetime.now(timezone.utc),
            article=f"ART-{random.randint(100, 999)}",
            brand=item["brand"],
            subject=item["subject"],
            name=f"{item['subject']} {item['brand']}",
            price=item["price"],
            kiz_required=True,
            kiz_status=KizStatus.PENDING,
            sticker_id=f"{wb_id}-ST",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(order)
        created_orders.append(wb_id)

    await db.commit()

    return {
        "success": True,
        "message": "Mock data seeded successfully",
        "seller_id": seller.id,
        "created_order_ids": created_orders,
    }


@router.post("/simulate-order-flow")
async def simulate_order_flow(order_id: int, db: AsyncSession = Depends(get_db)):
    """Симулировать сквозной процесс: сборка -> привязка КИЗ -> выбытие."""
    order = await db.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    mock_sgtin = f"010460123456789021{random.randint(100000000, 999999999)}AB"

    # Step 1: Mark Assembling
    order.status = OrderStatus.ASSEMBLING

    # Step 2: Attach SGTIN
    order.kiz_code = mock_sgtin
    order.kiz_status = KizStatus.ATTACHED
    order.kiz_attached_at = datetime.now(timezone.utc)

    # Step 3: Mark Assembled & Deliver
    order.status = OrderStatus.DELIVERED
    order.kiz_status = KizStatus.WITHDRAWN

    # Log operation
    audit = AuditLog(
        seller_id=order.seller_id,
        agent="debug_simulator",
        action="SIMULATE_FLOW",
        entity_type="order",
        entity_id=str(order_id),
        payload={"sgtin": mock_sgtin, "status": "WITHDRAWN"},
        trace_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc),
    )
    db.add(audit)
    await db.commit()

    return {
        "success": True,
        "order_id": order_id,
        "simulated_sgtin": mock_sgtin,
        "final_order_status": order.status,
        "final_kiz_status": order.kiz_status,
    }
