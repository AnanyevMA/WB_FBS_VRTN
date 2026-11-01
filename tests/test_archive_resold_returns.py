"""
Tests for old refusals in an uploaded WB archive.
A refusal row must not return a KIZ to circulation when WB sold that code after the refusal.
Telegram and web uploads share analyze_archive_data, so both paths are covered here.
"""
import random
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.database import AsyncSessionLocal, init_db
from app.models.kiz import KizProductInfo
from app.models.kiz_event import KizEvent
from app.models.order import KizStatus, Order, OrderStatus
from app.models.seller import Seller
from app.services.archive_service import analyze_archive_data

INN = "190207495060"
KIZ = "0104630199251332215ARCHIVETEST1"
OLD_SRID = "srid-of-the-refused-order"
NEW_SRID = "srid-of-the-later-sale"


@pytest.fixture(autouse=True)
async def setup_test_db():
    await init_db()


async def _seller(db) -> Seller:
    seller = Seller(id=str(uuid.uuid4()), name="Archive", wb_api_token_encrypted="mock", cz_inn=INN)
    db.add(seller)
    await db.commit()
    return seller


def _event(seller_id: str, event_type: str, day: int, srid: str, source: str = "wb_excise") -> KizEvent:
    return KizEvent(
        id=str(uuid.uuid4()), seller_id=seller_id, clean_cis=KIZ, event_type=event_type,
        occurred_at=datetime(2026, 10, day, tzinfo=timezone.utc), source=source,
        external_id=str(uuid.uuid4()), srid=srid,
    )


async def _analyze(seller, db, order_id=None, date="2026-10-09"):
    record = KizProductInfo(
        kiz_code=KIZ, clean_cis=KIZ, cz_status="RETIRED", cz_owner_inn=INN,
        raw_cz_payload={"ownerInn": INN, "status": "RETIRED"},
    )
    archive = {
        "kiz_rows": [{"№ задания": order_id, "КИЗ": KIZ, "Дата": date, "Стоимость": 3855, "Тип операции": "Возврат"}],
        "tasks_rows": [{"№ задания": order_id, "Статус задания": "Отказ покупателем"}],
    }
    with patch("app.services.archive_service.batch_verify_and_sync_cises", return_value={KIZ: record}):
        return await analyze_archive_data(seller=seller, archive_data=archive, db=db)


@pytest.mark.asyncio
async def test_refusal_is_not_returned_when_code_was_sold_later():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        db.add(_event(seller.id, "SALE", 31, NEW_SRID))
        await db.commit()

        analysis = await _analyze(seller, db)

        item = analysis["returns"][0]
        assert item["needs_cz_return"] is False
        assert item["selected"] is False
        assert analysis["summary"]["returns_needing_cz_return"] == 0
        assert analysis["summary"]["returns_already_in_circulation"] == 1


@pytest.mark.asyncio
async def test_refusal_is_returned_when_later_sale_was_also_returned():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        db.add(_event(seller.id, "SALE", 20, NEW_SRID))
        db.add(_event(seller.id, "RETURN", 22, NEW_SRID, source="wb_finance"))
        await db.commit()

        analysis = await _analyze(seller, db)

        assert analysis["returns"][0]["needs_cz_return"] is True
        assert analysis["returns"][0]["selected"] is True


@pytest.mark.asyncio
async def test_sale_of_the_refused_order_itself_does_not_block_return():
    """WB fiscalizes the next day, so the sale of this order can be dated after the refusal row."""
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        order_id = random.randint(10**9, 10**12)
        db.add(Order(
            id=order_id, seller_id=seller.id, status=OrderStatus.CANCELLED, wb_status="canceled_by_client",
            wb_created_at=datetime(2026, 10, 5, tzinfo=timezone.utc), kiz_code=KIZ,
            kiz_status=KizStatus.RETURNED, rid=OLD_SRID, price=3855,
        ))
        db.add(_event(seller.id, "SALE", 10, OLD_SRID))
        await db.commit()

        analysis = await _analyze(seller, db, order_id=order_id)

        assert analysis["returns"][0]["needs_cz_return"] is True


@pytest.mark.asyncio
async def test_refusal_without_later_sale_is_still_returned():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)

        analysis = await _analyze(seller, db)

        assert analysis["returns"][0]["needs_cz_return"] is True
        assert analysis["summary"]["returns_needing_cz_return"] == 1


@pytest.mark.asyncio
async def test_sale_before_the_refusal_does_not_block_return():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        db.add(_event(seller.id, "SALE", 3, NEW_SRID))
        await db.commit()

        analysis = await _analyze(seller, db)

        assert analysis["returns"][0]["needs_cz_return"] is True


@pytest.mark.asyncio
async def test_sale_is_detected_with_archive_events_from_other_sources_ignored():
    """Events recorded from the uploaded file itself are not WB sales."""
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        db.add(_event(seller.id, "SALE", 28, NEW_SRID, source="archive_file"))
        await db.commit()

        analysis = await _analyze(seller, db)

        assert analysis["returns"][0]["needs_cz_return"] is True
