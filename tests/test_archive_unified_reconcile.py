"""
Tests for Archive Orders Status Synchronization, Unified Reconciliation Preservation,
and GIS MT submission handling (including error 14).
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock
import pytest

from app.database import AsyncSessionLocal, init_db
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.models.kiz import KizSignatureBatch, BatchStatus, KizProductInfo
from app.services.auth_service import ensure_initial_admin
from app.services.unified_kiz_batch_service import create_unified_kiz_signature_batch
from app.services.signature_batch_submitter import execute_signed_batch_submission


@pytest.fixture(autouse=True)
async def setup_test_db():
    await init_db()
    async with AsyncSessionLocal() as session:
        await ensure_initial_admin(session)


@pytest.mark.asyncio
async def test_unified_reconciliation_preserves_archive_batch_and_order_sync():
    """Проверяет, что единая сверка сохраняет возвраты из активных пакетов архивов."""
    async with AsyncSessionLocal() as db:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Test Seller",
            cz_inn="190207495060",
            wb_api_token_encrypted="mock_token",
        )
        db.add(seller)

        ord_id = int(str(uuid.uuid4().int)[:10])
        order = Order(
            id=ord_id,
            seller_id=seller_id,
            wb_created_at=datetime.now(timezone.utc),
            status=OrderStatus.DELIVERED,
            wb_status="sold",
            kiz_status=KizStatus.WITHDRAWN,
            kiz_code="0104630199252612215*I)EeruudhW6",
        )
        db.add(order)

        archive_batch = KizSignatureBatch(
            id=str(uuid.uuid4()),
            seller_id=seller_id,
            filename="archive (17).xlsx",
            source="telegram",
            status=BatchStatus.PENDING_SIGNATURE,
            sales_count=0,
            returns_count=1,
            data_payload={
                "returns": [{
                    "order_id": ord_id,
                    "kiz_code": "0104630199252612215*I)EeruudhW6",
                    "receipt_number": "12345",
                    "receipt_date": "2026-09-30",
                    "price": 1000.0,
                    "needs_cz_return": True,
                }]
            },
        )
        db.add(archive_batch)
        await db.commit()

        rec_ret = KizProductInfo(
            kiz_code="0104630199252612215*I)EeruudhW6",
            clean_cis="0104630199252612215*I)EeruudhW6",
            cz_status="RETIRED",
            cz_owner_inn="190207495060",
            raw_cz_payload={"ownerInn": "190207495060"},
        )

        with patch("app.services.unified_kiz_batch_service.fetch_wb_excise_data", return_value=[]), \
             patch("app.services.unified_kiz_batch_service.batch_verify_and_sync_cises", return_value={"0104630199252612215*I)EeruudhW6": rec_ret}):
            res = await create_unified_kiz_signature_batch(seller=seller, db=db, days=30)
            assert res["success"] is True

            batch = await db.get(KizSignatureBatch, res["batch_id"])
            assert batch.returns_count == 1
            ret = batch.data_payload["returns"][0]
            assert ret["clean_cis"] == "0104630199252612215*I)EeruudhW6"
            assert ret["order_id"] == ord_id
            assert ret["needs_cz_return"] is True


@pytest.mark.asyncio
async def test_signature_batch_submitter_syncs_status_and_handles_error_14():
    """Проверяет, что при ошибке 14 ГИС МТ (недопустимый статус) КИЗ помечается как INTRODUCED."""
    async with AsyncSessionLocal() as db:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Test Seller",
            cz_inn="190207495060",
            wb_api_token_encrypted="mock_token",
        )
        db.add(seller)

        batch_id = str(uuid.uuid4())
        kiz = "0104630199251844215a&hTOsiaepo1"
        batch = KizSignatureBatch(
            id=batch_id,
            seller_id=seller_id,
            filename="unified_test.xlsx",
            source="unified_reconciliation",
            status=BatchStatus.PENDING_SIGNATURE,
            returns_count=1,
            data_payload={
                "returns": [{
                    "order_id": None,
                    "kiz_code": kiz,
                    "needs_cz_return": True,
                }]
            },
        )
        db.add(batch)
        await db.commit()

        mock_cz_client = AsyncMock()
        mock_cz_client.submit_signed_document = AsyncMock(return_value="doc-err-14")
        from app.services.cz_client import CZDocumentError
        mock_cz_client.wait_for_document = AsyncMock(
            side_effect=CZDocumentError("14: Недопустимый статус кода идентификации 0104630199251844215a&hTOsiaepo1.")
        )

        with patch("app.services.signature_batch_submitter.CZClient", return_value=mock_cz_client):
            res = await execute_signed_batch_submission(
                seller=seller,
                batch=batch,
                payload={
                    "sign_mode": "client_cades",
                    "cert_subject": "Test Admin",
                    "signed_documents": [{
                        "action": "RETURN",
                        "order_id": None,
                        "kiz_code": kiz,
                        "signature_base64": "mock_sig",
                        "document_base64": "mock_b64",
                        "type": "LP_RETURN",
                    }],
                },
                db=db,
            )
            assert res["success"] is True
            assert res["failed_submissions"] == 1

            # Проверяем, что в kiz_product_info статус обновился на INTRODUCED
            from sqlalchemy import select
            kinfo_res = await db.execute(select(KizProductInfo).where(KizProductInfo.clean_cis == kiz))
            kinfo = kinfo_res.scalar_one_or_none()
            assert kinfo is not None
            assert kinfo.cz_status == "INTRODUCED"
