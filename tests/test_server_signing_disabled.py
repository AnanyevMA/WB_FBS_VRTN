"""
Tests for disabled server signing.
Without the seller setting or КриптоПро on the server, a background return must not try to sign:
the order keeps its status and stays a candidate for a signature batch.
"""
import random
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.agents.cz_return import return_order_kiz, sync_engine
from app.api.kiz.archive import process_wb_archive
from app.api.kiz.signature_batches import submit_signed_batch
from app.database import AsyncSessionLocal, init_db
from app.models.audit import AuditLog
from app.models.kiz import BatchStatus, KizOperation, KizSignatureBatch
from app.models.order import KizStatus, Order, OrderStatus
from app.models.seller import Seller
from app.services.auto_kiz_queue_service import collect_auto_kiz_candidates
from app.services.crypto_service import can_sign_on_server
from app.services.encryption import encrypt

KIZ = "0104630199251318215SIGNTEST0001"
CRYPTO = "app.services.crypto_service"


@pytest.fixture(autouse=True)
async def setup_test_db():
    await init_db()


def _no_mock_settings():
    return MagicMock(mock_cz=False)


def _create_cancelled_order(auto_sign: bool = False) -> tuple:
    seller_id = str(uuid.uuid4())
    order_id = random.randint(10**9, 10**12)
    with Session(sync_engine) as db:
        db.add(Seller(
            id=seller_id, name="No server signing", wb_api_token_encrypted=encrypt("wb"),
            cz_inn="190207495060", cz_token_encrypted=encrypt("cz"), auto_kiz_auto_sign_server=auto_sign,
        ))
        db.add(Order(
            id=order_id, seller_id=seller_id, status=OrderStatus.CANCELLED, wb_status="canceled_by_client",
            wb_created_at=datetime.now(timezone.utc), price=3000, kiz_required=True, kiz_code=KIZ,
            kiz_status=KizStatus.WITHDRAWN,
        ))
        db.commit()
    return seller_id, order_id


@pytest.mark.parametrize("flag,csp,expected", [
    (False, False, False),
    (False, True, False),
    (True, False, False),
    (True, True, True),
])
def test_can_sign_on_server_needs_setting_and_cryptopro(flag, csp, expected):
    with patch(f"{CRYPTO}.settings", _no_mock_settings()), patch(f"{CRYPTO}.is_cryptopro_available", return_value=csp):
        assert can_sign_on_server(MagicMock(auto_kiz_auto_sign_server=flag)) is expected


@pytest.mark.asyncio
async def test_background_return_does_not_sign_and_keeps_order_for_batch():
    seller_id, order_id = _create_cancelled_order()

    with patch(f"{CRYPTO}.settings", _no_mock_settings()), \
         patch("app.agents.cz_return._do_return", new_callable=AsyncMock) as do_return:
        return_order_kiz(seller_id=seller_id, order_id=order_id, kiz_code=KIZ)

    do_return.assert_not_called()
    with Session(sync_engine) as db:
        order = db.get(Order, order_id)
        assert order.kiz_status == KizStatus.WITHDRAWN
        assert order.kiz_code == KIZ
        op = db.query(KizOperation).filter(KizOperation.order_id == order_id).one()
        assert op.status == "AWAITING_SIGNATURE"
        audit = db.query(AuditLog).filter(AuditLog.seller_id == seller_id, AuditLog.agent == "cz_return").one()
        assert audit.action == "AWAITING_SIGNATURE"

    async with AsyncSessionLocal() as db:
        seller = await db.get(Seller, seller_id)
        _, returns = await collect_auto_kiz_candidates(seller, db)
        assert [o.id for o in returns] == [order_id]
        stored = await db.get(Order, order_id)
        assert stored.kiz_code == KIZ


def test_background_return_signs_when_server_signing_is_enabled():
    seller_id, order_id = _create_cancelled_order(auto_sign=True)

    with patch(f"{CRYPTO}.settings", _no_mock_settings()), \
         patch(f"{CRYPTO}.is_cryptopro_available", return_value=True), \
         patch("app.agents.cz_return._do_return", new_callable=AsyncMock, return_value="doc-1") as do_return:
        return_order_kiz(seller_id=seller_id, order_id=order_id, kiz_code=KIZ)

    do_return.assert_called_once()
    with Session(sync_engine) as db:
        assert db.get(Order, order_id).kiz_status == KizStatus.RETURNED


@pytest.mark.asyncio
async def test_batch_is_not_completed_by_server_mode_when_disabled():
    seller_id, _ = _create_cancelled_order()
    async with AsyncSessionLocal() as db:
        batch = KizSignatureBatch(
            id=str(uuid.uuid4()), seller_id=seller_id, status=BatchStatus.PENDING_SIGNATURE,
            data_payload={"withdrawals": [], "returns": []},
        )
        db.add(batch)
        await db.commit()
        batch_id = batch.id

        with patch(f"{CRYPTO}.settings", _no_mock_settings()), pytest.raises(HTTPException) as err:
            await submit_signed_batch(seller_id=seller_id, batch_id=batch_id, payload={"sign_mode": "server"}, db=db)

        assert err.value.status_code == 400
        assert (await db.get(KizSignatureBatch, batch_id)).status == BatchStatus.PENDING_SIGNATURE


@pytest.mark.asyncio
async def test_archive_server_mode_is_rejected_when_disabled():
    seller_id, _ = _create_cancelled_order()
    async with AsyncSessionLocal() as db:
        with patch(f"{CRYPTO}.settings", _no_mock_settings()), pytest.raises(HTTPException) as err:
            await process_wb_archive(seller_id=seller_id, payload={"sign_mode": "server", "returns": []}, db=db)

        assert err.value.status_code == 400
