"""
Script to trigger retry-withdrawal for an order.
Usage:
    python scripts/retry_order_cz.py --order-id 5647931541
"""
import asyncio
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import AsyncSessionLocal
from app.models.order import Order
from app.api.orders import retry_order_cz_withdrawal


async def main(order_id: int):
    async with AsyncSessionLocal() as db:
        order = await db.get(Order, order_id)
        if not order:
            print(f"Order {order_id} not found")
            return
        res = await retry_order_cz_withdrawal(
            seller_id=str(order.seller_id),
            order_id=order.id,
            db=db,
        )
        print("RETRY RESULT:", res)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--order-id", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.order_id))
