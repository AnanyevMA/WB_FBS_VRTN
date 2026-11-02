"""
Tests for KIZ registry records without a Chestny Znak status.
A sync with an expired token left a second record per code, without a status but with a check date.
Such a record must not count as checked and must not produce documents in the reconciliation.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.database import AsyncSessionLocal, init_db
from app.models.kiz import KizProductInfo, KizSignatureBatch
from app.models.seller import Seller
from app.services.encryption import encrypt
from app.services.kiz_service import batch_verify_and_sync_cises, resolve_kiz_product_info
from app.services.unified_kiz_batch_service import create_unified_kiz_signature_batch

INN = "190207495060"
BATCH_SERVICE = "app.services.unified_kiz_batch_service"
REPORT = "app.services.unified_kiz_sales_report"
GET_CISES = "app.services.cz_client.CZClient.get_cises_info"


def _code() -> str:
    return "0104630199251325215" + uuid.uuid4().hex[:12].upper()


@pytest.fixture(autouse=True)
async def setup_test_db():
    await init_db()


async def _seller(db) -> Seller:
    seller = Seller(
        id=str(uuid.uuid4()), name="Registry", wb_api_token_encrypted="mock", cz_inn=INN,
        cz_token_encrypted=encrypt("cz-token"),
    )
    db.add(seller)
    await db.commit()
    return seller


def _record(seller_id: str, kiz_code: str, clean_cis: str, status) -> KizProductInfo:
    return KizProductInfo(
        id=str(uuid.uuid4()), kiz_code=kiz_code, clean_cis=clean_cis, gtin=clean_cis[2:16], seller_id=seller_id,
        cz_status=status, cz_owner_inn=INN, checked_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_record_with_status_wins_over_duplicate_without_status():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        code = _code()
        db.add(_record(seller.id, f"{code}\x1d91EE12\x1d92TAIL", code, "RETIRED"))
        db.add(_record(seller.id, code, code, None))
        await db.commit()

        with patch(GET_CISES, new_callable=AsyncMock) as get_cises:
            results = await batch_verify_and_sync_cises(seller=seller, kiz_codes=[code], db=db)

        get_cises.assert_not_called()
        assert results[code].cz_status == "RETIRED"


@pytest.mark.asyncio
async def test_record_without_status_is_requested_again_despite_fresh_check_date():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        code = _code()
        db.add(_record(seller.id, code, code, None))
        await db.commit()

        answer = [{"cisInfo": {"requestedCis": code, "status": "INTRODUCED", "ownerInn": INN}}]
        with patch(GET_CISES, new_callable=AsyncMock, return_value=answer) as get_cises:
            results = await batch_verify_and_sync_cises(seller=seller, kiz_codes=[code], db=db)

        get_cises.assert_awaited_once()
        assert results[code].cz_status == "INTRODUCED"


@pytest.mark.asyncio
async def test_failed_true_api_call_does_not_return_record_without_status():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        code = _code()
        db.add(_record(seller.id, code, code, None))
        await db.commit()

        with patch(GET_CISES, new_callable=AsyncMock, side_effect=RuntimeError("401")):
            results = await batch_verify_and_sync_cises(seller=seller, kiz_codes=[code], db=db)

        assert results.get(code) is None


@pytest.mark.asyncio
async def test_resolve_does_not_create_duplicate_for_code_stored_with_tail():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        code = _code()
        existing = _record(seller.id, f"{code}\x1d91EE12\x1d92TAIL", code, "RETIRED")
        db.add(existing)
        await db.commit()

        found = await resolve_kiz_product_info(kiz_code=code, seller=seller, db=db, force_refresh=False)

        assert found.id == existing.id
        assert found.cz_status == "RETIRED"
        from sqlalchemy import select
        rows = (await db.execute(select(KizProductInfo).where(KizProductInfo.clean_cis == code))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,key", [("sale", "withdrawals"), ("return", "returns")])
async def test_reconciliation_does_not_propose_documents_for_code_without_status(operation, key):
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        code = _code()
        rows = [{
            "saleID": "S1", "srid": "srid-1", "date": "2026-10-20T10:00:00",
            "finishedPrice": 3000, "nmId": 1, "subject": "Капоры",
        }]
        if operation == "return":
            rows.append({
                "saleID": "R1", "srid": "srid-1", "date": "2026-10-22T10:00:00",
                "finishedPrice": -3000, "nmId": 1, "subject": "Капоры",
            })
        excise = [{
            "excise_short": code, "fiscal_doc_number": 100, "fiscal_drive_number": "FN1",
            "fiscal_dt": "2026-10-21", "price": 3000, "nm_id": 1, "srid": "srid-1",
        }]
        without_status = _record(seller.id, code, code, None)

        with patch(f"{BATCH_SERVICE}.fetch_wb_excise_data", return_value=excise), \
             patch(f"{BATCH_SERVICE}.batch_verify_and_sync_cises", return_value={code: without_status}), \
             patch(f"{REPORT}.fetch_wb_sales_rows", AsyncMock(return_value=rows)):
            res = await create_unified_kiz_signature_batch(seller=seller, db=db, days=30)

        payload = (await db.get(KizSignatureBatch, res["batch_id"])).data_payload
        assert payload[key] == []
        assert payload["summary"]["cz_unverified_count"] == 1
