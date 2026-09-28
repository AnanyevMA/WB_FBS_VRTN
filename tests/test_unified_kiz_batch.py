"""
Unit tests for Unified KIZ Batch Reconciliation and Owner Validation.
Verifies that:
1. Sales and returns from FBS orders and WB financial reports are merged chronologically.
2. Resold items after return are eliminated from return candidates.
3. KIZs owned by WB (9714053621) or Belarus (100083608) are marked for remarking, NEVER for LP_RETURN.
4. Only KIZs strictly owned by the seller (ownerInn == seller.cz_inn) can be selected for LP_RETURN.
5. prepare_batch_documents_for_signing excludes unowned KIZs.
6. sync_batch_with_cz_data never turns WB-owned items into return candidates.
"""
import uuid
from datetime import date, datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch
import pytest
from httpx import AsyncClient, ASGITransport

from app.database import AsyncSessionLocal, init_db
from app.main import app
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.models.kiz import KizSignatureBatch, BatchStatus, KizProductInfo
from app.models.wb_finance import WbSalesReportRow
from app.services.auth_service import ensure_initial_admin, create_access_token
from app.services.unified_kiz_batch_service import create_unified_kiz_signature_batch
from app.services.signature_batch_executor import (
    build_batch_signing_payloads,
    sync_batch_with_cz_data,
)


@pytest.fixture(autouse=True)
async def setup_test_db():
    await init_db()
    async with AsyncSessionLocal() as session:
        await ensure_initial_admin(session)


@pytest.mark.asyncio
async def test_unified_reconciliation_ownership_and_chronology():
    async with AsyncSessionLocal() as db:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Test Seller",
            wb_api_token_encrypted="mock_token",
            cz_inn="190207495060",
        )
        db.add(seller)
        await db.commit()

        # 1. Row 1: Return owned by WB (9714053621)
        r1 = WbSalesReportRow(
            seller_id=seller_id,
            rrd_id=101,
            clean_cis="0104603702055109215WBOWNED1234",
            kiz="0104603702055109215WBOWNED1234\x1d91EE10\x1d92XYZ",
            doc_type_name="Возврат",
            rr_date=date.today() - timedelta(days=10),
            retail_amount=500.0,
        )
        # 2. Row 2: Return owned by Seller (190207495060)
        r2 = WbSalesReportRow(
            seller_id=seller_id,
            rrd_id=102,
            clean_cis="0104603702055109215SELLEROWN12",
            kiz="0104603702055109215SELLEROWN12\x1d91EE10\x1d92XYZ",
            doc_type_name="Возврат покупателя",
            rr_date=date.today() - timedelta(days=5),
            retail_amount=600.0,
        )
        # 3. Row 3: Return that was subsequentely resold!
        r3_ret = WbSalesReportRow(
            seller_id=seller_id,
            rrd_id=103,
            clean_cis="0104603702055109215RESOLDITEM1",
            kiz="0104603702055109215RESOLDITEM1\x1d91EE10\x1d92XYZ",
            doc_type_name="Возврат",
            sale_dt=datetime.now(timezone.utc) - timedelta(days=15),
            rr_date=date.today() - timedelta(days=15),
            retail_amount=700.0,
        )
        r3_sale = WbSalesReportRow(
            seller_id=seller_id,
            rrd_id=104,
            clean_cis="0104603702055109215RESOLDITEM1",
            kiz="0104603702055109215RESOLDITEM1\x1d91EE10\x1d92XYZ",
            doc_type_name="Продажа",
            sale_dt=datetime.now(timezone.utc) - timedelta(days=2),
            rr_date=date.today() - timedelta(days=2),
            retail_amount=700.0,
        )
        # 4. Row 4: Sale owned by WB (9714053621) - in INTRODUCED status!
        r4_sale = WbSalesReportRow(
            seller_id=seller_id,
            rrd_id=105,
            clean_cis="0104603702055109215WBSALEITEM1",
            kiz="0104603702055109215WBSALEITEM1\x1d91EE10\x1d92XYZ",
            doc_type_name="Продажа",
            sale_dt=datetime.now(timezone.utc) - timedelta(days=1),
            rr_date=date.today() - timedelta(days=1),
            retail_amount=800.0,
        )
        db.add_all([r1, r2, r3_ret, r3_sale, r4_sale])
        await db.commit()

        # Mock True API responses
        mock_sync_result = {
            "0104603702055109215WBOWNED1234": KizProductInfo(
                kiz_code="0104603702055109215WBOWNED1234",
                clean_cis="0104603702055109215WBOWNED1234",
                cz_status="RETIRED",
                cz_owner_inn="9714053621",
                cz_owner_name="ООО «РВБ»",
                raw_cz_payload={"ownerInn": "9714053621", "ownerName": "ООО «РВБ»"},
            ),
            "0104603702055109215SELLEROWN12": KizProductInfo(
                kiz_code="0104603702055109215SELLEROWN12",
                clean_cis="0104603702055109215SELLEROWN12",
                cz_status="RETIRED",
                cz_owner_inn="190207495060",
                cz_owner_name="ИП АНАНЬЕВ МАКСИМ АНДРЕЕВИЧ",
                raw_cz_payload={"ownerInn": "190207495060", "ownerName": "ИП АНАНЬЕВ МАКСИМ АНДРЕЕВИЧ"},
            ),
            "0104603702055109215RESOLDITEM1": KizProductInfo(
                kiz_code="0104603702055109215RESOLDITEM1",
                clean_cis="0104603702055109215RESOLDITEM1",
                cz_status="INTRODUCED",
                cz_owner_inn="190207495060",
                raw_cz_payload={"ownerInn": "190207495060"},
            ),
            "0104603702055109215WBSALEITEM1": KizProductInfo(
                kiz_code="0104603702055109215WBSALEITEM1",
                clean_cis="0104603702055109215WBSALEITEM1",
                cz_status="INTRODUCED",
                cz_owner_inn="9714053621",
                cz_owner_name="ООО «РВБ»",
                raw_cz_payload={"ownerInn": "9714053621", "ownerName": "ООО «РВБ»"},
            ),
        }

        with patch("app.services.unified_kiz_batch_service.batch_verify_and_sync_cises", return_value=mock_sync_result):
            seller.cz_token_encrypted = b"dummy"
            res = await create_unified_kiz_signature_batch(seller=seller, db=db, days=90)
            assert res["success"] is True

            batch_id = res["batch_id"]
            batch = await db.get(KizSignatureBatch, batch_id)
            assert batch is not None
            assert batch.returns_count == 1  # Only the seller-owned return!
            assert batch.sales_count == 1    # Only the seller-owned resold item, NOT the WB-owned sale!

            summary = batch.data_payload["summary"]
            assert summary["resold_after_return_count"] == 1
            assert summary["seller_owned_direct_count"] == 1
            assert summary["wb_owned_remarking_count"] == 1
            assert summary["sales_wb_owned_count"] == 1

            withdrawals = batch.data_payload["withdrawals"]
            assert len(withdrawals) == 1  # Only actionable seller-owned withdrawal!
            seller_sale = withdrawals[0]
            assert seller_sale["clean_cis"] == "0104603702055109215RESOLDITEM1"
            assert seller_sale["needs_withdrawal"] is True
            assert seller_sale["is_seller_owner"] is True
            assert seller_sale["selected"] is True

            # WB-owned items must NOT clutter the actionable lists
            assert not any(w["clean_cis"] == "0104603702055109215WBSALEITEM1" for w in withdrawals)

            returns = batch.data_payload["returns"]
            assert len(returns) == 1  # Only actionable seller-owned return!
            seller_item = returns[0]
            assert seller_item["clean_cis"] == "0104603702055109215SELLEROWN12"
            assert seller_item["needs_cz_return"] is True
            assert seller_item["selected"] is True

            # WB-owned return items must NOT clutter the actionable lists
            assert not any(r["clean_cis"] == "0104603702055109215WBOWNED1234" for r in returns)

            # Test build_batch_signing_payloads: WB item must NOT be generated into LP_RETURN or LK_RECEIPT
            seller.cz_token_encrypted = None
            docs = build_batch_signing_payloads(seller=seller, batch=batch)
            assert docs["total_documents"] == 2
            actions = [d["action"] for d in docs["documents"]]
            assert "WITHDRAWAL" in actions
            assert "RETURN" in actions
            # WB-owned items must be strictly omitted
            codes = [d["kiz_code"] for d in docs["documents"]]
            assert "0104603702055109215WBOWNED1234" not in codes
            assert "0104603702055109215WBSALEITEM1" not in codes
            assert seller_item["kiz_code"] in codes



@pytest.mark.asyncio
async def test_sync_batch_with_cz_data_preserves_owner_safety():
    """Verify sync_batch_with_cz_data never turns WB-owned items into return candidates."""
    async with AsyncSessionLocal() as db:
        seller_id = str(uuid.uuid4())
        seller = Seller(id=seller_id, name="Test Seller", wb_api_token_encrypted="mock_token", cz_inn="190207495060")
        db.add(seller)

        batch_id = str(uuid.uuid4())
        batch = KizSignatureBatch(
            id=batch_id,
            seller_id=seller_id,
            status=BatchStatus.PENDING_SIGNATURE,
            filename="test_sync.xlsx",
            source="test",
            data_payload={
                "withdrawals": [
                    {
                        "kiz_code": "0104603702055109215WBWITHDRAW12",
                        "clean_cis": "0104603702055109215WBWITHDRAW12",
                        "order_id": "ord-w1",
                    }
                ],
                "returns": [
                    {
                        "kiz_code": "0104603702055109215WBTEST123456",
                        "clean_cis": "0104603702055109215WBTEST123456",
                        "order_id": "ord-1",
                    }
                ],
            }
        )
        db.add(batch)
        await db.commit()

        # Mock batch_verify_and_sync_cises returning RETIRED with WB owner
        from app.models.kiz import KizProductInfo
        rec = KizProductInfo(
            kiz_code="0104603702055109215WBTEST123456",
            clean_cis="0104603702055109215WBTEST123456",
            cz_status="RETIRED",
            raw_cz_payload={"ownerInn": "9714053621", "ownerName": "ООО «РВБ»"},
        )
        rec_w = KizProductInfo(
            kiz_code="0104603702055109215WBWITHDRAW12",
            clean_cis="0104603702055109215WBWITHDRAW12",
            cz_status="INTRODUCED",
            raw_cz_payload={"ownerInn": "9714053621", "ownerName": "ООО «РВБ»"},
        )

        with patch("app.services.signature_batch_executor.batch_verify_and_sync_cises", return_value={"0104603702055109215WBTEST123456": rec, "0104603702055109215WBWITHDRAW12": rec_w}):
            sync_res = await sync_batch_with_cz_data(seller=seller, batch=batch, db=db)
            assert sync_res["success"] is True
            assert sync_res["returns_count"] == 0

            ret_item = sync_res["data_payload"]["returns"][0]
            assert ret_item["needs_cz_return"] is False
            assert ret_item["needs_remarking"] is True
            assert ret_item["selected"] is False
            assert ret_item["is_wb_owned"] is True

            w_item = sync_res["data_payload"]["withdrawals"][0]
            assert w_item["needs_withdrawal"] is False
            assert w_item["selected"] is False
            assert w_item["is_wb_owned"] is True
            assert "РВБ" in w_item["action_recommended"]


@pytest.mark.asyncio
async def test_unified_reconcile_api_endpoint():
    """Verify POST /api/v1/sellers/{seller_id}/kiz/signature-batches/unified-reconcile."""
    async with AsyncSessionLocal() as db:
        admin_user = await ensure_initial_admin(db)
        token = create_access_token(
            data={"sub": admin_user.id, "username": admin_user.username, "role": "admin", "is_superuser": True}
        )
        headers = {"Authorization": f"Bearer {token}"}

        seller_id = str(uuid.uuid4())
        seller = Seller(id=seller_id, name="API Test Seller", wb_api_token_encrypted="mock_token", cz_inn="190207495060")
        db.add(seller)
        await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch("app.api.kiz.signature_batches.create_unified_kiz_signature_batch", return_value={"success": True, "batch_id": "mock-batch", "summary": {"period_days": 90}}):
                resp = await client.post(
                    f"/api/v1/sellers/{seller_id}/kiz/signature-batches/unified-reconcile",
                    json={"days": 90},
                    headers=headers,
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is True
                assert data["batch_id"] == "mock-batch"


@pytest.mark.asyncio
async def test_unified_reconciliation_integrates_excise_report():
    """Verify excise-report online sales are fetched and enrich withdrawal documents with real fiscal receipts."""
    async with AsyncSessionLocal() as db:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Excise Test Seller",
            wb_api_token_encrypted="mock_token",
            cz_inn="190207495060",
            cz_token_encrypted=b"dummy",
        )
        db.add(seller)
        await db.commit()

        mock_excise_rows = [
            {
                "barcode": "2049792157460",
                "excise_short": "0104630199253602215!_x<2R:/KWcL",
                "fiscal_doc_number": 74808,
                "fiscal_drive_number": "7380440903834140",
                "fiscal_dt": "2026-09-25",
                "price": 5083,
                "nm_id": 899193428,
                "srid": "srid-12345",
            }
        ]
        rec_excise = KizProductInfo(
            kiz_code="0104630199253602215!_x<2R:/KWcL",
            clean_cis="0104630199253602215!_x<2R:/KWcL",
            cz_status="INTRODUCED",
            cz_owner_inn="190207495060",
            cz_owner_name="ИП АНАНЬЕВ",
            raw_cz_payload={"ownerInn": "190207495060", "ownerName": "ИП АНАНЬЕВ"},
        )

        with patch("app.services.unified_kiz_batch_service.fetch_wb_excise_data", return_value=mock_excise_rows) as mock_fetch, \
             patch("app.services.unified_kiz_batch_service.batch_verify_and_sync_cises", return_value={"0104630199253602215!_x<2R:/KWcL": rec_excise}):
            res = await create_unified_kiz_signature_batch(seller=seller, db=db, days=30)
            assert res["success"] is True

            batch_id = res["batch_id"]
            batch = await db.get(KizSignatureBatch, batch_id)
            assert batch is not None
            assert batch.sales_count == 1
            assert batch.data_payload["summary"]["sales_excise_report_count"] == 1

            withdrawals = batch.data_payload["withdrawals"]
            assert len(withdrawals) == 1
            w = withdrawals[0]
            assert w["clean_cis"] == "0104630199253602215!_x<2R:/KWcL"
            assert w["receipt_number"] == "74808"
            assert w["fn_number"] == "7380440903834140"
            assert w["receipt_date"] == "2026-09-25"
            assert w["price"] == 5083.0
            assert w["needs_withdrawal"] is True
            assert w["selected"] is True

