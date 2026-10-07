import asyncio
import logging
import sys
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.seller import Seller
from app.services.wb_finance_service import sync_seller_financial_reports

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

async def run():
    async with AsyncSessionLocal() as db:
        seller = (await db.execute(select(Seller).where(Seller.is_active == True))).scalars().first()
        if not seller:
            print("No active seller found!")
            return
        
        print(f"Starting financial report sync for {seller.name} (days=14)...")
        try:
            # We test with verify_cz=False first or verify_cz=True?
            # Let's see: verify_cz=False or True
            res = await sync_seller_financial_reports(seller=seller, db=db, days=14, verify_cz=True)
            print("Sync result:", res)
        except Exception as e:
            logging.exception(f"Sync failed with error: {e}")

if __name__ == "__main__":
    asyncio.run(run())
