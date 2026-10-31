"""
Regression tests for attaching a KIZ from the order card.
The removed scanner route could overwrite the code of the seller's latest order, even a sold one.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.kiz.attach import attach_kiz, router
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


def _db(order=None) -> AsyncMock:
    """db.get returns the seller, then the requested order."""
    db = AsyncMock()
    db.add = MagicMock()
    db.get = AsyncMock(side_effect=[MagicMock(), order])
    return db


def _kiz_info():
    return MagicMock(
        is_valid=True, cz_status="INTRODUCED", cz_status_ex=None, gtin="04600000000009", product_name="Товар",
        article="ART", tech_size="M", wb_size="46", raw_cz_payload={}, validation_message="",
    )


async def _attach(db, order_id=1, code=NEW_CODE):
    with patch("app.services.kiz_service.resolve_kiz_product_info", new_callable=AsyncMock, return_value=_kiz_info()):
        return await attach_kiz(seller_id=SELLER, order_id=order_id, req=KIZAttachRequest(kiz_code=code), db=db)


def test_scanner_route_is_removed():
    paths = {route.path for route in router.routes}
    assert "/kiz/attach" not in paths
    assert "/orders/{order_id}/kiz" in paths


@pytest.mark.asyncio
async def test_kiz_goes_to_the_requested_order():
    order = _order(order_id=77)
    res = await _attach(_db(order), order_id=77)
    assert res["order_id"] == 77
    assert order.kiz_code == NEW_CODE


@pytest.mark.asyncio
async def test_order_of_another_seller_is_rejected():
    order = _order(seller_id="other-seller", kiz_code="OLD")
    db = _db(order)
    with pytest.raises(HTTPException) as exc:
        await _attach(db)
    assert exc.value.status_code == 404
    assert order.kiz_code == "OLD"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_order_is_rejected():
    with pytest.raises(HTTPException) as exc:
        await _attach(_db(None), order_id=404)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("kiz_status,doc", [(KizStatus.WITHDRAWN, None), (KizStatus.VALIDATED, "doc-1")])
async def test_withdrawn_order_keeps_its_code(kiz_status, doc):
    order = _order(kiz_status=kiz_status, kiz_code="OLD", doc=doc, status=OrderStatus.DELIVERED)
    db = _db(order)
    with pytest.raises(HTTPException) as exc:
        await _attach(db)
    assert exc.value.status_code == 409
    assert order.kiz_code == "OLD"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_rescanning_same_code_on_withdrawn_order_is_allowed():
    order = _order(kiz_status=KizStatus.WITHDRAWN, kiz_code=NEW_CODE, status=OrderStatus.DELIVERED)
    res = await _attach(_db(order))
    assert res["success"] is True
