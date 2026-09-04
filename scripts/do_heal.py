import asyncio
from datetime import datetime, timezone
from app.database import AsyncSessionLocal
from app.models.order import Order, KizStatus
from app.models.kiz import KizOperation, KizOperationType, KizProductInfo
from sqlalchemy import select, desc, text

async def main():
    async with AsyncSessionLocal() as db:
        # 1. Delete duplicate KPI
        await db.execute(text("DELETE FROM kiz_product_info WHERE id = 'e4ea08c4-2fb6-44af-a1b8-375c774efd73'"))
        await db.commit()

        # 2. Update order
        order = await db.get(Order, 5647931541)
        order.kiz_code = "0104630199252988215sEZejEe1Y>VK"
        order.kiz_status = KizStatus.WITHDRAWN
        order.kiz_cz_status = "RETIRED"
        order.kiz_cz_status_updated_at = datetime.now(timezone.utc)
        order.cz_withdrawal_doc_id = "651d7449-fd50-41ec-bc96-8ec7c28d1dcc"
        order.cz_doc_status = "CHECKED_OK"
        order.cz_rejection_reason = None
        order.updated_at = datetime.now(timezone.utc)

        # 3. Update or create KIZ Operation
        res = await db.execute(
            select(KizOperation)
            .where(KizOperation.order_id == 5647931541)
            .order_by(desc(KizOperation.id))
        )
        op = res.scalars().first()
        if op:
            op.status = "SUCCESS"
            op.cz_doc_id = "651d7449-fd50-41ec-bc96-8ec7c28d1dcc"
            op.cz_doc_status = "CHECKED_OK"
            op.error_message = None
            op.updated_at = datetime.now(timezone.utc)

        # 4. Update remaining KPI
        kpi = await db.get(KizProductInfo, "ca02db6a-1642-44bb-b3be-3bd2e8bf4b29")
        if kpi:
            kpi.clean_cis = "0104630199252988215sEZejEe1Y>VK"
            kpi.kiz_code = "0104630199252988215sEZejEe1Y>VK"
            kpi.cz_status = "RETIRED"
            kpi.order_id = 5647931541
            kpi.checked_at = datetime.now(timezone.utc)

        await db.commit()
        await db.refresh(order)
        print("SUCCESS! Order:", order.id, order.kiz_status, order.cz_withdrawal_doc_id, order.cz_doc_status, order.cz_rejection_reason)

if __name__ == '__main__':
    asyncio.run(main())
