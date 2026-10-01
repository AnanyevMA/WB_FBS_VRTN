"""
Live test script for WB Archive API synchronization on VPS.
"""
import asyncio
import logging
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.seller import Seller
from app.models.order import Order, OrderStatus
from app.services.wb_archive_service import sync_seller_archive_orders_recent_months

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_archive_sync")


async def main():
    async with AsyncSessionLocal() as db:
        sellers = (await db.execute(select(Seller).where(Seller.is_active == True))).scalars().all()
        if not sellers:
            print("No active sellers found.")
            return

        for seller in sellers:
            print(f"\n--- Testing seller: {seller.name} (ID: {seller.id}) ---")
            if not seller.wb_api_token_encrypted:
                print("No WB API token configured, skipping.")
                continue

            try:
                print("Running sync_seller_archive_orders_recent_months(months_count=3)...")
                summary = await sync_seller_archive_orders_recent_months(
                    seller=seller,
                    db=db,
                    months_count=3,
                )
                print("Result Summary:")
                for k, v in summary.items():
                    print(f"  {k}: {v}")

                # Check cancelled orders with KIZ
                cancelled_stmt = select(Order).where(
                    Order.seller_id == str(seller.id),
                    Order.status == OrderStatus.CANCELLED,
                    Order.kiz_code.isnot(None),
                )
                cancelled_orders = (await db.execute(cancelled_stmt)).scalars().all()
                print(f"Total CANCELLED orders with KIZ in DB for this seller: {len(cancelled_orders)}")
                for o in cancelled_orders[:5]:
                    print(f"  Order #{o.id} | wb_status: {o.wb_status} | KIZ: {o.kiz_code}")

            except Exception as e:
                print(f"ERROR syncing archive for {seller.name}: {e}")
                import traceback
                traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
