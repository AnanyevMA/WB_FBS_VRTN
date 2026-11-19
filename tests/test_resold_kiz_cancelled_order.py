"""
A cancelled FBS order returns its KIZ to stock, and the same code can then be sold again in another order.
Withdrawals and CZ status syncs for that later sale must not rewrite the cancelled order
(case of order 5916811598: WB says canceled_by_client, the app showed it as sold and withdrawn).
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.database import AsyncSessionLocal, init_db
from app.models.kiz import BatchStatus, KizOperation, KizSignatureBatch
from app.models.order import KizStatus, Order, OrderStatus
from app.models.seller import Seller
from app.services.encryption import encrypt
from app.services.kiz_service import sync_kiz_status_record
from app.services.signature_batch_submitter import execute_signed_batch_submission
from sqlalchemy import select


@pytest.fixture(autouse=True)
async def setup_test_db():
    await init_db()


def _cis() -> str:
    return f"0104630199252674215{uuid.uuid4().hex[:12]}"


async def _seller(db) -> Seller:
    seller = Seller(
        id=str(uuid.uuid4()), name="Resale Seller", wb_supplier_id=f"WB-{uuid.uuid4().hex[:6]}",
        wb_api_token_encrypted=encrypt("wb"), cz_token_encrypted=encrypt("cz"), cz_inn="190207495060", is_active=True,
    )
    db.add(seller)
    await db.flush()
    return seller


def _order(seller: Seller, cis: str, status: OrderStatus, wb_status: str, kiz_status: KizStatus) -> Order:
    return Order(
        id=int(uuid.uuid4().int % 10**9) + 7_000_000_000, seller_id=seller.id, status=status, wb_status=wb_status,
        wb_created_at=datetime.now(timezone.utc), kiz_code=cis, kiz_required=True, kiz_status=kiz_status,
    )


async def _submit_withdrawal(db, seller: Seller, cis: str, order_id: int) -> dict:
    batch = KizSignatureBatch(
        id=str(uuid.uuid4()), seller_id=seller.id, status=BatchStatus.PENDING_SIGNATURE,
        data_payload={"withdrawals": [{"kiz_code": cis, "order_id": order_id}], "returns": []},
    )
    db.add(batch)
    await db.flush()
    payload = {"signed_documents": [{
        "type": "LK_RECEIPT", "document_base64": "ZG9j", "signature_base64": "c2ln",
        "kiz_code": cis, "order_id": order_id, "action": "WITHDRAWAL",
    }]}
    with patch("app.services.cz_client.CZClient.submit_signed_document", new_callable=AsyncMock, return_value="doc-resale"), \
         patch("app.services.cz_client.CZClient.wait_for_document", new_callable=AsyncMock, return_value={"status": "CHECKED_OK"}):
        return await execute_signed_batch_submission(seller=seller, batch=batch, payload=payload, db=db)


@pytest.mark.asyncio
async def test_withdrawal_of_resold_code_keeps_cancelled_order_intact():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        cis = _cis()
        cancelled = _order(seller, cis, OrderStatus.CANCELLED, "canceled_by_client", KizStatus.RETURNED)
        db.add(cancelled)
        await db.commit()

        result = await _submit_withdrawal(db, seller, cis, cancelled.id)
        await db.commit()

        assert result["successful_submissions"] == 1
        await db.refresh(cancelled)
        assert cancelled.status == OrderStatus.CANCELLED
        assert cancelled.wb_status == "canceled_by_client"
        assert cancelled.kiz_status == KizStatus.RETURNED
        assert cancelled.cz_withdrawal_doc_id is None
        ops = (await db.execute(select(KizOperation).where(KizOperation.order_id == cancelled.id))).scalars().all()
        assert ops == []


@pytest.mark.asyncio
async def test_withdrawal_of_sold_order_still_updates_it_without_inventing_wb_status():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        cis = _cis()
        sold = _order(seller, cis, OrderStatus.DELIVERING, "ready_for_pickup", KizStatus.ATTACHED)
        db.add(sold)
        await db.commit()

        await _submit_withdrawal(db, seller, cis, sold.id)
        await db.commit()

        await db.refresh(sold)
        assert sold.status == OrderStatus.DELIVERED
        assert sold.kiz_status == KizStatus.WITHDRAWN
        assert sold.cz_withdrawal_doc_id == "doc-resale"
        assert sold.wb_status == "ready_for_pickup"  # WB status only ever comes from WB


@pytest.mark.asyncio
async def test_cz_sync_of_retired_code_does_not_mark_cancelled_order_withdrawn():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        cis = _cis()
        cancelled = _order(seller, cis, OrderStatus.CANCELLED, "canceled_by_client", KizStatus.RETURNED)
        no_doc = _order(seller, _cis(), OrderStatus.CANCELLED, "canceled_by_client", KizStatus.VALIDATED)
        db.add_all([cancelled, no_doc])
        await db.commit()

        # Background status sync: the code was sold again elsewhere and is now retired
        await sync_kiz_status_record(db=db, kiz_code=cis, cz_status="RETIRED", seller_id=seller.id)
        await sync_kiz_status_record(db=db, kiz_code=no_doc.kiz_code, cz_status="RETIRED", seller_id=seller.id)
        await db.commit()

        await db.refresh(cancelled)
        await db.refresh(no_doc)
        assert cancelled.kiz_status == KizStatus.RETURNED
        assert cancelled.kiz_cz_status == "RETIRED"  # the live state of the code is still shown
        assert no_doc.kiz_status == KizStatus.VALIDATED  # previously flipped to ERROR


@pytest.mark.asyncio
async def test_cz_sync_still_marks_delivered_order_withdrawn():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        cis = _cis()
        delivered = _order(seller, cis, OrderStatus.DELIVERED, "sold", KizStatus.ATTACHED)
        db.add(delivered)
        await db.commit()

        await sync_kiz_status_record(db=db, kiz_code=cis, cz_status="RETIRED", seller_id=seller.id)
        await db.commit()

        await db.refresh(delivered)
        assert delivered.kiz_status == KizStatus.WITHDRAWN


@pytest.mark.asyncio
async def test_return_for_cancelled_order_still_updates_it():
    """A return of the order's own code targets the cancelled order explicitly and must still apply."""
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        cis = _cis()
        cancelled = _order(seller, cis, OrderStatus.CANCELLED, "canceled_by_client", KizStatus.WITHDRAWN)
        db.add(cancelled)
        await db.commit()

        await sync_kiz_status_record(db=db, kiz_code=cis, cz_status="INTRODUCED", seller_id=seller.id,
                                     target_order_id=cancelled.id)
        await db.commit()

        await db.refresh(cancelled)
        assert cancelled.kiz_cz_status == "INTRODUCED"
