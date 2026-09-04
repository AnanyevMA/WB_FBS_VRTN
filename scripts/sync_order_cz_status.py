"""
Script to synchronize order CZ withdrawal document status with True API v4.
Usage:
    python scripts/sync_order_cz_status.py --order-id 5647931541
"""
import asyncio
import argparse
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.order import Order, KizStatus
from app.models.seller import Seller
from app.models.kiz import KizOperation
from app.services.encryption import decrypt
from app.services.cz_client import CZClient, extract_document_error_text


async def sync_order(order_id: int):
    async with AsyncSessionLocal() as db:
        order = await db.get(Order, order_id)
        if not order:
            print(f"❌ Заказ {order_id} не найден в БД.")
            return

        if not order.cz_withdrawal_doc_id:
            print(f"ℹ️ У заказа {order_id} нет cz_withdrawal_doc_id.")
            return

        seller = await db.get(Seller, order.seller_id)
        if not seller or not seller.cz_token_encrypted:
            print(f"❌ Продавец {order.seller_id} не найден или нет токена ЧЗ.")
            return

        token = decrypt(seller.cz_token_encrypted)
        doc_id = order.cz_withdrawal_doc_id
        print(f"🔍 Опрос документа {doc_id} в True API v4 для заказа #{order_id}...")

        client = CZClient(token=token, inn=seller.cz_inn)
        doc_info = await client.get_document_status(doc_id)
        status = doc_info.get("status")
        print(f"📊 Статус документа в ГИС МТ: {status}")

        if status == "CHECKED_OK" or status == "ACCEPTED":
            order.cz_doc_status = "CHECKED_OK"
            order.cz_rejection_reason = None
            order.kiz_status = KizStatus.WITHDRAWN
            order.kiz_cz_status = "RETIRED"
            print("✅ Документ подтвержден ГИС МТ! Статус заказа обновлен в WITHDRAWN.")
        elif status == "CHECKED_NOT_OK" or status == "FAILED":
            error_reason = extract_document_error_text(doc_info) or "Документ отклонен ГИС МТ"
            order.cz_doc_status = "CHECKED_NOT_OK"
            order.cz_rejection_reason = error_reason
            order.kiz_status = KizStatus.ERROR
            order.kiz_cz_status = "CHECKED_NOT_OK"
            print(f"❌ Документ отклонен ГИС МТ: {error_reason}")
        else:
            order.cz_doc_status = status
            print(f"⏳ Документ в обработке: {status}")

        await db.commit()
        await db.refresh(order)
        print(f"💾 Заказ #{order.id} сохранен. Текущий kiz_status={order.kiz_status}, cz_rejection_reason={order.cz_rejection_reason}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--order-id", type=int, required=True, help="ID заказа")
    args = parser.parse_args()
    asyncio.run(sync_order(args.order_id))
