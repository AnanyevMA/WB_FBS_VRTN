"""
CLI Utility to check Chestny Znak (GIS MT / True API) document status
Usage:
    python scripts/check_cz_doc.py --doc-id 8681694f-96a4-4343-aed8-483f7d2010e7
    docker compose -f docker-compose.prod.yml exec -T api python scripts/check_cz_doc.py --doc-id 8681694f-96a4-4343-aed8-483f7d2010e7
"""
import asyncio
import argparse
import json
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.seller import Seller
from app.services.encryption import decrypt
from app.services.cz_client import CZClient


async def check_document_status(doc_id: str, seller_id: str = None):
    sync_engine = create_engine(settings.database_url_sync)
    with Session(sync_engine) as db:
        if seller_id:
            seller = db.execute(select(Seller).where(Seller.id == seller_id)).scalar_one_or_none()
        else:
            seller = db.execute(
                select(Seller).where(Seller.is_active == True, Seller.cz_token_encrypted.isnot(None))
            ).scalars().first()

        if not seller:
            print("❌ Ошибка: В базе данных не найден активный продавец с токеном Честного Знака.")
            sys.exit(1)

        token = decrypt(seller.cz_token_encrypted)
        inn = seller.cz_inn
        seller_name = seller.name

    print("=" * 60)
    print(f"🏢 Продавец: {seller_name} (ИНН: {inn})")
    print(f"📄 Документ ГИС МТ: {doc_id}")
    print("=" * 60)
    print("⏳ Отправка запроса в ГИС МТ (True API)...")

    async with CZClient(inn=inn, token=token) as client:
        try:
            status_data = await client.get_document_status(doc_id)
            print("\n✅ Ответ от системы Честный Знак:")
            print(json.dumps(status_data, indent=2, ensure_ascii=False))

            status = status_data.get("status", "")
            if status in ("CHECKED_OK", "ACCEPTED", "SUCCESS", "COMPLETED"):
                print(f"\n🎉 Статус документа: {status} (Успешно обработан)")
            elif status in ("IN_PROGRESS", "PROCESSING", "PENDING"):
                print(f"\n⏳ Статус документа: {status} (В обработке)")
            else:
                print(f"\n⚠️ Статус документа: {status}")
        except Exception as e:
            print(f"\n❌ Ошибка при обращении к True API: {e}")
            sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check CZ / GIS MT document status")
    parser.add_argument("--doc-id", default="8681694f-96a4-4343-aed8-483f7d2010e7", help="GIS MT document UUID")
    parser.add_argument("--seller-id", default=None, help="Optional Seller UUID")
    args = parser.parse_args()

    asyncio.run(check_document_status(args.doc_id, args.seller_id))
