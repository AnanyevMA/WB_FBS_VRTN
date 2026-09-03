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

    import httpx
    headers = {
        "clientToken": token,
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    candidate_paths = [
        f"/api/v3/true-api/documents/receipts/{doc_id}",
        f"/api/v3/true-api/doc/{doc_id}/status",
        f"/api/v3/true-api/documents/{doc_id}",
        f"/api/v3/facade/doc/{doc_id}/status",
        f"/api/v3/lk/documents/{doc_id}",
        f"/api/v3/lk/documents/{doc_id}/status",
        f"/api/v3/true-api/doc/{doc_id}",
    ]

    found = False
    async with httpx.AsyncClient(base_url="https://markirovka.crpt.ru", timeout=15.0, headers=headers) as client:
        for path in candidate_paths:
            try:
                res = await client.get(path)
                if res.status_code in (200, 201):
                    print(f"\n✅ Успешный ответ по адресу {path} (код {res.status_code}):")
                    try:
                        data = res.json()
                        print(json.dumps(data, indent=2, ensure_ascii=False))
                    except Exception:
                        print(res.text)
                    found = True
                    break
                else:
                    print(f"ℹ️ {path} -> {res.status_code}: {res.text[:120]}")
            except Exception as e:
                print(f"⚠️ Ошибка запроса к {path}: {e}")

    if not found:
        print("\n🔍 Поиск по реестру документов через True API...")
        # Try searching via /api/v3/true-api/documents/list or filter
        search_paths = [
            f"/api/v3/true-api/documents/list?number={doc_id}",
            f"/api/v3/facade/doc/listV2?number={doc_id}",
        ]
        async with httpx.AsyncClient(base_url="https://markirovka.crpt.ru", timeout=15.0, headers=headers) as client:
            for sp in search_paths:
                try:
                    res = await client.get(sp)
                    print(f"ℹ️ {sp} -> {res.status_code}: {res.text[:200]}")
                except Exception as e:
                    print(f"⚠️ {sp} -> {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check CZ / GIS MT document status")
    parser.add_argument("--doc-id", default="8681694f-96a4-4343-aed8-483f7d2010e7", help="GIS MT document UUID")
    parser.add_argument("--seller-id", default=None, help="Optional Seller UUID")
    args = parser.parse_args()

    asyncio.run(check_document_status(args.doc_id, args.seller_id))
