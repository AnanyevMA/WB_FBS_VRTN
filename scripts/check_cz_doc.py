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
    candidate_hosts = [
        "https://ismp.crpt.ru",
        "https://markirovka.crpt.ru",
    ]
    candidate_paths = [
        f"/api/v4/true-api/doc/{doc_id}/info?pg=lp",
        f"/api/v4/true-api/doc/{doc_id}/info",
        f"/api/v3/true-api/doc/{doc_id}/info",
        f"/api/v3/facade/doc/{doc_id}/status",
        f"/api/v3/facade/doc/{doc_id}/info",
        f"/api/v3/facade/doc/{doc_id}",
    ]

    found = False
    for host in candidate_hosts:
        async with httpx.AsyncClient(base_url=host, timeout=15.0, headers=headers) as client:
            for path in candidate_paths:
                try:
                    res = await client.get(path)
                    content_type = res.headers.get("content-type", "")
                    if res.status_code in (200, 201) and "json" in content_type:
                        print(f"\n✅ Успешный ответ от {host}{path} (код {res.status_code}):")
                        try:
                            data = res.json()
                            print(json.dumps(data, indent=2, ensure_ascii=False))
                        except Exception:
                            print(res.text)
                        found = True
                        break
                    else:
                        print(f"ℹ️ {host}{path} -> {res.status_code}: {res.text[:120]}")
                except Exception as e:
                    print(f"⚠️ {host}{path} -> {e}")
            if found:
                break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check CZ / GIS MT document status")
    parser.add_argument("--doc-id", default="8681694f-96a4-4343-aed8-483f7d2010e7", help="GIS MT document UUID")
    parser.add_argument("--seller-id", default=None, help="Optional Seller UUID")
    args = parser.parse_args()

    asyncio.run(check_document_status(args.doc_id, args.seller_id))
