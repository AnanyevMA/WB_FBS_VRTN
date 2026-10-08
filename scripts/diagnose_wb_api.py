"""
Diagnostic script to test live Wildberries API methods and token status.
"""
import asyncio
import time
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.seller import Seller
from app.services.encryption import decrypt
from app.services.wb_client import WBClient, get_wb_token_status


async def test_wb():
    async with AsyncSessionLocal() as db:
        sellers = (await db.execute(select(Seller).where(Seller.is_active == True))).scalars().all()
        if not sellers:
            print("No active sellers in DB.")
            return

        for s in sellers:
            print(f"\n==========================================")
            print(f"=== SELLER: {s.name} (ID: {s.id}) ===")
            print(f"==========================================")
            if not s.wb_api_token_encrypted:
                print("No WB token configured.")
                continue

            token = decrypt(s.wb_api_token_encrypted)
            status, expires_at, days_left = get_wb_token_status(token)
            print(f"Token Status : {status.upper()}")
            print(f"Expires At   : {expires_at}")
            print(f"Days Left    : {days_left} days")
            print("------------------------------------------")

            client = WBClient(token)

            # 1. Test Warehouses
            t0 = time.time()
            try:
                whs = await client._request("GET", "/api/v3/warehouses")
                dt1 = time.time() - t0
                wh_list = whs if isinstance(whs, list) else []
                print(f"1. GET /api/v3/warehouses: [SUCCESS] {len(wh_list)} warehouses found in {dt1:.2f}s")
                for w in wh_list[:3]:
                    print(f"   • Warehouse: ID={w.get('id')}, Name='{w.get('name')}'")
            except Exception as e:
                print(f"1. GET /api/v3/warehouses: [FAILED] {e}")

            # 2. Test New Orders
            t0 = time.time()
            try:
                new_orders = await client.get_new_orders()
                dt2 = time.time() - t0
                print(f"2. GET /api/v3/orders/new: [SUCCESS] {len(new_orders)} new orders in {dt2:.2f}s")
            except Exception as e:
                print(f"2. GET /api/v3/orders/new: [FAILED] {e}")

            # 3. Test Orders (last 7 days)
            t0 = time.time()
            try:
                from datetime import datetime, timezone, timedelta
                now = datetime.now(timezone.utc)
                week_ago = now - timedelta(days=7)
                orders_list = await client.get_orders(date_start=week_ago, date_end=now)
                dt3 = time.time() - t0
                print(f"3. GET /api/v3/orders: [SUCCESS] {len(orders_list)} orders fetched in {dt3:.2f}s")
            except Exception as e:
                print(f"3. GET /api/v3/orders: [FAILED] {e}")

            # 4. Test Supplies
            t0 = time.time()
            try:
                res_sup = await client.get_supplies(limit=10)
                dt4 = time.time() - t0
                sup_list = res_sup.get("supplies", []) if isinstance(res_sup, dict) else res_sup
                print(f"4. GET /api/v3/supplies: [SUCCESS] {len(sup_list)} supplies fetched in {dt4:.2f}s")
            except Exception as e:
                print(f"4. GET /api/v3/supplies: [FAILED] {e}")

            # 5. Test Cards Catalog
            t0 = time.time()
            try:
                cat = await client.get_cards_catalog()
                dt5 = time.time() - t0
                by_code = cat.get("by_vendor_code", {})
                print(f"5. POST content-api get_cards_catalog: [SUCCESS] {len(by_code)} cards mapped in {dt5:.2f}s")
            except Exception as e:
                print(f"5. POST content-api get_cards_catalog: [FAILED] {e}")

            await client.close()


if __name__ == "__main__":
    asyncio.run(test_wb())
