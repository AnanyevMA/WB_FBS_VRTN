"""
Unit tests for WB Finance Return Signature Batch Document Preparation.
Verifies that primary_document_type, primary_document_number, and primary_document_date
are strictly present in all LP_RETURN documents, preventing GIS MT rejection.
"""
import json
import uuid
from datetime import date, datetime, timezone
import pytest
from app.database import AsyncSessionLocal, init_db
from app.models.seller import Seller
from app.models.kiz import KizSignatureBatch, BatchStatus
from app.services.cz_client import CZClient
from app.services.auth_service import ensure_initial_admin
from app.api.kiz.signature_batches import prepare_batch_documents_for_signing


@pytest.fixture(autouse=True)
async def setup_test_db():
    await init_db()
    async with AsyncSessionLocal() as session:
        await ensure_initial_admin(session)


def test_cz_client_build_return_payload_with_receipt():
    """Verify that build_return_payload with receipt_number sets RECEIPT type and number."""
    client = CZClient(inn="190207495060")
    payload = client.build_return_payload(
        kiz_codes=["0104603702055109215PcNrGZUlfCwb"],
        receipt_number="3131054561442",
        receipt_date="2026-07-24",
    )
    doc_json = json.loads(payload["inner_json"])
    assert doc_json["trade_participant_inn"] == "190207495060"
    assert doc_json["return_type"] == "REMOTE_SALE_RETURN"
    assert len(doc_json["products_list"]) == 1
    prod = doc_json["products_list"][0]
    assert prod["ki"] == "0104603702055109215PcNrGZUlfCwb"
    assert prod["primary_document_type"] == "RECEIPT"
    assert prod["primary_document_number"] == "3131054561442"
    assert prod["primary_document_date"] == "2026-07-24"


def test_cz_client_build_return_payload_fallback():
    """Verify that build_return_payload without order_id or receipt has safe fallback."""
    client = CZClient(inn="190207495060")
    payload = client.build_return_payload(
        kiz_codes=["0104603702055109215PcNrGZUlfCwb"],
        wb_order_id=None,
        receipt_number=None,
    )
    doc_json = json.loads(payload["inner_json"])
    prod = doc_json["products_list"][0]
    assert prod["primary_document_type"] == "OTHER"
    assert prod["primary_document_number"] == "1"
    assert prod["primary_document_custom_name"] == "Возврат от покупателя Wildberries FBS"
    assert prod["primary_document_date"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest.mark.asyncio
async def test_prepare_batch_documents_for_signing_finance_returns():
    """Verify that prepare_batch_documents_for_signing correctly populates primary doc attributes."""
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="VRTN workshop",
            wb_api_token_encrypted="mock_token",
            cz_inn="190207495060",
            is_active=True,
        )
        session.add(seller)
        await session.commit()

        batch_id = str(uuid.uuid4())
        batch = KizSignatureBatch(
            id=batch_id,
            seller_id=seller_id,
            filename="wb_finance_returns_90d.json",
            source="wb_finance_returns",
            status=BatchStatus.PENDING_SIGNATURE,
            returns_count=2,
            data_payload={
                "withdrawals": [],
                "returns": [
                    {
                        "order_id": None,
                        "sticker_id": None,
                        "kiz_code": "0104603702055109215PcNrGZUlfCwb 91EE10 92xyz=",
                        "clean_cis": "0104603702055109215PcNrGZUlfCwb",
                        "receipt_number": "3131054561442",
                        "receipt_date": "2026-07-24",
                        "needs_cz_return": True,
                        "selected": True,
                    },
                    {
                        "order_id": 987654321,
                        "sticker_id": "STK-111",
                        "kiz_code": "0104603702055123215/nXHCvzoM_Wb 91EE10 92abc=",
                        "clean_cis": "0104603702055123215/nXHCvzoM_Wb",
                        "receipt_number": "3131054561446",
                        "receipt_date": "2026-07-25",
                        "needs_cz_return": True,
                        "selected": True,
                    }
                ],
                "summary": {},
            }
        )
        session.add(batch)
        await session.commit()

        res = await prepare_batch_documents_for_signing(
            seller_id=seller_id,
            batch_id=batch_id,
            payload={},
            db=session,
        )

        assert res["success"] is True
        assert res["total_documents"] == 2
        docs = res["documents"]

        # Item 1: order_id is None, receipt_number from finance row
        doc1 = docs[0]
        assert doc1["action"] == "RETURN"
        assert doc1["receipt_number"] == "3131054561442"
        inner1 = json.loads(doc1["inner_json"])
        prod1 = inner1["products_list"][0]
        assert prod1["ki"] == "0104603702055109215PcNrGZUlfCwb"
        assert prod1["primary_document_type"] == "RECEIPT"
        assert prod1["primary_document_number"] == "3131054561442"
        assert prod1["primary_document_date"] == "2026-07-24"

        # Item 2: order_id is present
        doc2 = docs[1]
        assert doc2["action"] == "RETURN"
        assert doc2["order_id"] == 987654321
        assert doc2["receipt_number"] == "3131054561446"
        inner2 = json.loads(doc2["inner_json"])
        prod2 = inner2["products_list"][0]
        assert prod2["ki"] == "0104603702055123215/nXHCvzoM_Wb"
        assert prod2["primary_document_type"] == "RECEIPT"
        assert prod2["primary_document_number"] == "3131054561446"
        assert prod2["primary_document_date"] == "2026-07-25"
