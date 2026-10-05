"""
Unit tests verifying prevention of duplicate KIZ batch additions.
Ensures that:
1. Orders with kiz_status == WITHDRAWN or pending cz_withdrawal_doc_id are never re-added to batches,
   even if True API status is still INTRODUCED due to CRPT processing delay.
2. Active KizOperation WITHDRAWAL records exclude items from new batches.
3. sync_kiz_status_record updates all duplicate rows for the same clean_cis.
"""
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
import pytest

from app.database import AsyncSessionLocal, init_db
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.models.kiz import KizOperation, KizOperationType, KizProductInfo
from app.services.auth_service import ensure_initial_admin
from app.services.unified_kiz_batch_service import create_unified_kiz_signature_batch
from app.services.kiz_service import sync_kiz_status_record
from sqlalchemy import select


@pytest.fixture(autouse=True)
async def setup_test_db():
    await init_db()
    async with AsyncSessionLocal() as session:
        await ensure_initial_admin(session)


@pytest.mark.asyncio
async def test_order_with_in_progress_withdrawal_is_never_readded():
    """
    Test that an order that was already submitted (with cz_withdrawal_doc_id and cz_doc_status=IN_PROGRESS)
    is excluded from new batches even if True API status is still INTRODUCED.
    """
    async with AsyncSessionLocal() as db:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Test Seller Dedup",
            wb_api_token_encrypted="mock_token",
            cz_inn="190207495060",
        )
        db.add(seller)
        await db.commit()

        # Order with in-progress withdrawal
        test_cis = f"0104630199252636215{uuid.uuid4().hex[:13]}"
        unique_order_id = int(uuid.uuid4().int % 10000000000)
        order = Order(
            id=unique_order_id,
            seller_id=seller_id,
            status=OrderStatus.DELIVERED,
            wb_status="sold",
            kiz_code=test_cis,
            kiz_status=KizStatus.WITHDRAWN,
            cz_withdrawal_doc_id="79889eef-2434-47a5-8487-ec77d9d37531",
            cz_doc_status="IN_PROGRESS",
            wb_created_at=datetime.now(timezone.utc) - timedelta(days=5),
            price=1500.0,
            article="skirt.01",
        )
        db.add(order)
        await db.commit()

        # Mock True API returning INTRODUCED (simulating CRPT queue delay)
        mock_cz_info = {
            test_cis: {
                "status": "INTRODUCED",
                "ownerInn": "190207495060",
                "ownerName": "ИП АНАНЬЕВ",
            }
        }

        with patch("app.services.unified_kiz_batch_service.batch_verify_and_sync_cises") as mock_verify:
            mock_verify.return_value = {}
            with patch("app.services.unified_kiz_batch_service.fetch_wb_excise_data", return_value=[]):
                res = await create_unified_kiz_signature_batch(seller=seller, db=db, days=30)

        assert res["success"] is True
        summary = res["summary"]
        # The order must be classified as already withdrawn, NOT needing withdrawal
        assert summary["sales_needing_withdrawal"] == 0
        assert summary["sales_already_withdrawn"] == 1


@pytest.mark.asyncio
async def test_kiz_operation_excludes_item_from_withdrawal():
    """
    Test that an active KizOperation of type WITHDRAWAL excludes the item from new batches.
    """
    async with AsyncSessionLocal() as db:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Test Seller Op Dedup",
            wb_api_token_encrypted="mock_token",
            cz_inn="190207495060",
        )
        db.add(seller)
        await db.commit()

        test_cis = f"0104630199255293215{uuid.uuid4().hex[:13]}"
        unique_order_id = int(uuid.uuid4().int % 10000000000)
        # Order is delivered, but kiz_status is not updated yet
        order = Order(
            id=unique_order_id,
            seller_id=seller_id,
            status=OrderStatus.DELIVERED,
            wb_status="sold",
            kiz_code=test_cis,
            kiz_status=KizStatus.VALIDATED,
            wb_created_at=datetime.now(timezone.utc) - timedelta(days=3),
            price=2000.0,
            article="joggers.01",
        )
        db.add(order)

        # Existing KizOperation WITHDRAWAL in progress
        op = KizOperation(
            seller_id=seller_id,
            order_id=unique_order_id,
            kiz_code=test_cis,
            operation=KizOperationType.WITHDRAWAL,
            status="IN_PROGRESS",
            cz_doc_id="56d841c0-d1f2-4344-aeec-92e046a4dd7b",
            cz_doc_status="IN_PROGRESS",
        )
        db.add(op)
        await db.commit()

        with patch("app.services.unified_kiz_batch_service.batch_verify_and_sync_cises", return_value={}):
            with patch("app.services.unified_kiz_batch_service.fetch_wb_excise_data", return_value=[]):
                res = await create_unified_kiz_signature_batch(seller=seller, db=db, days=30)

        assert res["success"] is True
        summary = res["summary"]
        assert summary["sales_needing_withdrawal"] == 0
        assert summary["sales_already_withdrawn"] == 1


@pytest.mark.asyncio
async def test_sync_kiz_status_record_updates_all_duplicate_rows():
    """
    Test that sync_kiz_status_record updates all existing records for the same clean_cis,
    preventing split-brain status discrepancies between records with/without crypto tails.
    """
    async with AsyncSessionLocal() as db:
        seller_id = str(uuid.uuid4())
        clean_cis = f"0104630199251318215{uuid.uuid4().hex[:12]}"
        full_kiz = clean_cis + "\x1d91EE12\x1d92XYZ="

        # Create two duplicate rows
        rec1 = KizProductInfo(
            id=str(uuid.uuid4()),
            kiz_code=full_kiz,
            gtin="04630199251318",
            clean_cis=clean_cis,
            seller_id=seller_id,
            cz_status="INTRODUCED",
        )
        rec2 = KizProductInfo(
            id=str(uuid.uuid4()),
            kiz_code=clean_cis,
            gtin="04630199251318",
            clean_cis=clean_cis,
            seller_id=seller_id,
            cz_status="INTRODUCED",
        )
        db.add_all([rec1, rec2])
        await db.commit()

        # Update status using clean_cis
        await sync_kiz_status_record(
            db=db,
            kiz_code=clean_cis,
            cz_status="RETIRED",
            seller_id=seller_id,
        )
        await db.commit()

        # Verify BOTH records now have cz_status == RETIRED
        res = await db.execute(
            select(KizProductInfo).where(KizProductInfo.clean_cis == clean_cis)
        )
        records = res.scalars().all()
        assert len(records) == 2
        for r in records:
            assert r.cz_status == "RETIRED"
