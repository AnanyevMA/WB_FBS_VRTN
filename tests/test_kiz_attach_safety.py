"""
Regression tests for attaching a scanned KIZ.
The endpoint ignored order_id from the request body and, with no order waiting for a KIZ,
overwrote the code of the seller's latest order, even a sold one.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.kiz.attach import attach_kiz
from app.models.order import KizStatus, Order, OrderStatus
from app.schemas.order import KIZAttachRequest

SELLER = "seller-1"
NEW_CODE = "0104600000000009215TESTSCANK9"


def _order(order_id=1, seller_id=SELLER, kiz_status=KizStatus.PENDING, kiz_code=None, doc=None, status=OrderStatus.NEW):
    order = MagicMock(spec=Order)
    order.id = order_id
    order.seller_id = seller_id
    order.kiz_status = kiz_status
    order.kiz_code = kiz_code
    order.cz_withdrawal_doc_id = doc
    order.status = status
    return order


def _db(order=None, pending=None) -> AsyncMock:
    """db.get returns the seller, then the requested order; execute finds pending orders."""
    db = AsyncMock()
    db.add = MagicMock()
    db.get = AsyncMock(side_effect=[MagicMock(), order])
    result = MagicMock()
    result.scalars.return_value.first.return_value = pending
    db.execute = AsyncMock(return_value=result)
    return db


def _kiz_info():
    return MagicMock(
        is_valid=True, cz_status="INTRODUCED", cz_status_ex=None, gtin="04600000000009", product_name="Товар",
        article="ART", tech_size="M", wb_size="46", raw_cz_payload={}, validation_message="",
    )


async def _attach(db, req: KIZAttachRequest, order_id=None):
    with patch("app.services.kiz_service.resolve_kiz_product_info", new_callable=AsyncMock, return_value=_kiz_info()):
        return await attach_kiz(seller_id=SELLER, order_id=order_id, req=req, db=db)


@pytest.mark.asyncio
async def test_order_id_from_body_is_used():
    order = _order(order_id=9004, kiz_status=KizStatus.VALIDATED)
    res = await _attach(_db(order=order), KIZAttachRequest(kiz_code=NEW_CODE, order_id=9004))
    assert res["order_id"] == 9004
    assert order.kiz_code == NEW_CODE


@pytest.mark.asyncio
async def test_order_id_from_path_is_used():
    order = _order(order_id=77)
    res = await _attach(_db(order=order), KIZAttachRequest(kiz_code=NEW_CODE), order_id=77)
    assert res["order_id"] == 77


@pytest.mark.asyncio
async def test_without_order_id_takes_only_pending_order():
    pending = _order(order_id=5)
    db = AsyncMock()
    db.add = MagicMock()
    db.get = AsyncMock(return_value=MagicMock())
    result = MagicMock()
    result.scalars.return_value.first.return_value = pending
    db.execute = AsyncMock(return_value=result)
    res = await _attach(db, KIZAttachRequest(kiz_code=NEW_CODE))
    assert res["order_id"] == 5
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_no_pending_order_does_not_touch_any_order():
    db = AsyncMock()
    db.add = MagicMock()
    db.get = AsyncMock(return_value=MagicMock())
    result = MagicMock()
    result.scalars.return_value.first.return_value = None
    db.execute = AsyncMock(return_value=result)
    with pytest.raises(HTTPException) as exc:
        await _attach(db, KIZAttachRequest(kiz_code=NEW_CODE))
    assert exc.value.status_code == 404
    assert db.execute.await_count == 1, "must not fall back to an arbitrary latest order"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_order_of_another_seller_is_rejected():
    order = _order(seller_id="other-seller", kiz_code="OLD")
    with pytest.raises(HTTPException) as exc:
        await _attach(_db(order=order), KIZAttachRequest(kiz_code=NEW_CODE, order_id=1))
    assert exc.value.status_code == 404
    assert order.kiz_code == "OLD"


@pytest.mark.asyncio
async def test_unknown_order_id_does_not_fall_back_to_another_order():
    with pytest.raises(HTTPException) as exc:
        await _attach(_db(order=None, pending=_order(order_id=5)), KIZAttachRequest(kiz_code=NEW_CODE, order_id=404))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("kiz_status,doc", [(KizStatus.WITHDRAWN, None), (KizStatus.VALIDATED, "doc-1")])
async def test_withdrawn_order_keeps_its_code(kiz_status, doc):
    order = _order(kiz_status=kiz_status, kiz_code="OLD", doc=doc, status=OrderStatus.DELIVERED)
    db = _db(order=order)
    with pytest.raises(HTTPException) as exc:
        await _attach(db, KIZAttachRequest(kiz_code=NEW_CODE, order_id=1))
    assert exc.value.status_code == 409
    assert order.kiz_code == "OLD"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_rescanning_same_code_on_withdrawn_order_is_allowed():
    order = _order(kiz_status=KizStatus.WITHDRAWN, kiz_code=NEW_CODE, status=OrderStatus.DELIVERED)
    res = await _attach(_db(order=order), KIZAttachRequest(kiz_code=NEW_CODE, order_id=1))
    assert res["success"] is True
