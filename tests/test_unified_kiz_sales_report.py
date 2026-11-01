"""
Tests for WB returns in the unified KIZ reconciliation.
Returns from the WB sales report are linked by srid to KIZ events and to the rid of FBS orders,
a past return does not block a new one, and a later sale keeps the code out of returns.
"""
import random
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.database import AsyncSessionLocal, init_db
from app.models.kiz import BatchStatus, KizOperation, KizOperationType, KizProductInfo, KizSignatureBatch
from app.models.kiz_event import KizEvent
from app.models.order import KizStatus, Order, OrderStatus
from app.models.seller import Seller
from app.services.auto_kiz_queue_service import collect_auto_kiz_candidates
from app.services.signature_batch_executor import build_batch_signing_payloads
from app.services.unified_kiz_batch_service import create_unified_kiz_signature_batch

INN = "190207495060"
BATCH_SERVICE = "app.services.unified_kiz_batch_service"
REPORT = "app.services.unified_kiz_sales_report"
CIS = "0104630199251318215RETURNTEST1"


@pytest.fixture(autouse=True)
async def setup_test_db():
    await init_db()


def _days_ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _report_row(sale_id: str, srid: str, days: int, price: float = 3732) -> dict:
    return {
        "saleID": sale_id,
        "srid": srid,
        "date": _days_ago(days).strftime("%Y-%m-%dT%H:%M:%S"),
        "finishedPrice": -price if sale_id.startswith("R") else price,
        "nmId": 216098224,
        "subject": "Юбки",
    }


def _excise_row(days: int, srid: str, receipt) -> dict:
    return {
        "excise_short": CIS,
        "fiscal_doc_number": receipt,
        "fiscal_drive_number": "7380441003992427",
        "fiscal_dt": _days_ago(days).strftime("%Y-%m-%d"),
        "price": 3732,
        "nm_id": 216098224,
        "srid": srid,
    }


def _record(status: str, checked_at: datetime, owner: str = INN) -> KizProductInfo:
    return KizProductInfo(
        kiz_code=CIS, clean_cis=CIS, cz_status=status, cz_owner_inn=owner,
        raw_cz_payload={"ownerInn": owner, "status": status}, checked_at=checked_at,
    )


async def _seller(db) -> Seller:
    seller = Seller(id=str(uuid.uuid4()), name="Returns", wb_api_token_encrypted="mock", cz_inn=INN)
    db.add(seller)
    await db.commit()
    return seller


async def _op(db, seller_id: str, operation: KizOperationType, days: int) -> None:
    db.add(KizOperation(
        seller_id=seller_id, kiz_code=CIS, operation=operation, status="SUCCESS",
        cz_doc_status="CHECKED_OK", created_at=_days_ago(days),
    ))
    await db.commit()


def _sold_order(seller_id: str, **kwargs) -> Order:
    fields = dict(
        id=random.randint(10**9, 10**12), seller_id=seller_id, status=OrderStatus.DELIVERED, wb_status="sold",
        kiz_code=CIS, kiz_status=KizStatus.WITHDRAWN, wb_created_at=_days_ago(12), price=3732,
    )
    fields.update(kwargs)
    return Order(**fields)


async def _run(seller, db, report_rows, excise_rows=None, status="RETIRED", live_status=None, rids=None, report_error=None):
    calls = []

    async def fake_verify(db, seller, kiz_codes, force_refresh=False):
        calls.append(force_refresh)
        if not force_refresh:
            return {c: _record(status, _days_ago(0) - timedelta(hours=6)) for c in kiz_codes}
        return {c: _record(live_status or status, datetime.now(timezone.utc) + timedelta(seconds=1)) for c in kiz_codes}

    fetch_rows = AsyncMock(return_value=report_rows, side_effect=report_error)
    with patch(f"{BATCH_SERVICE}.fetch_wb_excise_data", return_value=excise_rows or []), \
         patch(f"{BATCH_SERVICE}.batch_verify_and_sync_cises", side_effect=fake_verify), \
         patch(f"{REPORT}.fetch_wb_sales_rows", fetch_rows), \
         patch(f"{REPORT}.fetch_wb_order_rids", AsyncMock(return_value=rids or {})):
        res = await create_unified_kiz_signature_batch(seller=seller, db=db, days=30)
    batch = await db.get(KizSignatureBatch, res["batch_id"])
    return batch.data_payload, calls


@pytest.mark.asyncio
async def test_return_from_sales_report_is_linked_by_excise_srid_and_uses_sale_receipt():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        rows = [_report_row("S1", "srid-sale", 5), _report_row("R1", "srid-sale", 3)]

        payload, calls = await _run(seller, db, rows, [_excise_row(4, "srid-sale", 178496)])

        assert True in calls
        assert payload["withdrawals"] == []
        assert payload["summary"]["returns_needing_cz_return"] == 1
        item = payload["returns"][0]
        assert item["needs_cz_return"] is True
        assert item["selected"] is True
        assert item["receipt_number"] == "178496"
        assert item["primary_document_type"] == "RECEIPT"
        assert item["order_id"] is None


@pytest.mark.asyncio
async def test_previous_return_does_not_block_new_wb_return():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        await _op(db, seller.id, KizOperationType.RETURN, days=20)
        rows = [_report_row("S1", "srid-new", 5), _report_row("R1", "srid-new", 3)]

        payload, _ = await _run(seller, db, rows, [_excise_row(4, "srid-new", 178496)])

        assert payload["summary"]["returns_needing_cz_return"] == 1
        assert payload["returns"][0]["receipt_number"] == "178496"


@pytest.mark.asyncio
async def test_fbs_return_is_linked_by_backfilled_rid_without_excise():
    """A returned FBS order keeps wb_status sold and has no excise row: rid is the only link."""
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        order = _sold_order(seller.id)
        db.add(order)
        await db.commit()
        order_id = order.id
        rows = [_report_row("S1", "rid-fbs", 5), _report_row("R1", "rid-fbs", 2)]

        payload, _ = await _run(seller, db, rows, rids={order_id: "rid-fbs"})

        stored = await db.get(Order, order_id)
        assert stored.rid == "rid-fbs"
        assert payload["summary"]["returns_needing_cz_return"] == 1
        item = payload["returns"][0]
        assert item["order_id"] == order_id
        assert item["receipt_number"] == str(order_id)
        assert item["primary_document_type"] == "OTHER"


@pytest.mark.asyncio
async def test_return_document_without_receipt_is_built_as_other():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        seller.mod_fias = "1f06b72d-5b8d-4f0c-a3ee-e0479498b901"
        order = _sold_order(seller.id, rid="rid-fbs")
        db.add(order)
        await db.commit()
        rows = [_report_row("S1", "rid-fbs", 5), _report_row("R1", "rid-fbs", 2)]

        payload, _ = await _run(seller, db, rows)
        batch = KizSignatureBatch(
            id=str(uuid.uuid4()), seller_id=seller.id, status=BatchStatus.PENDING_SIGNATURE, data_payload=payload,
        )
        db.add(batch)
        await db.commit()

        signing = build_batch_signing_payloads(seller=seller, batch=batch)

        doc = next(d for d in signing["documents"] if d["action"] == "RETURN")
        assert '"primary_document_type": "OTHER"' in doc["inner_json"]
        assert "primary_document_custom_name" in doc["inner_json"]


@pytest.mark.asyncio
async def test_sale_after_return_keeps_code_out_of_returns():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        await _op(db, seller.id, KizOperationType.WITHDRAWAL, days=1)
        rows = [
            _report_row("S1", "srid-old", 10), _report_row("R1", "srid-old", 8),
            _report_row("S2", "srid-new", 2),
        ]
        excise = [_excise_row(9, "srid-old", 100), _excise_row(1, "srid-new", 200)]

        payload, _ = await _run(seller, db, rows, excise)

        assert payload["returns"] == []
        assert payload["summary"]["return_candidates_count"] == 0


@pytest.mark.asyncio
async def test_withdrawal_after_return_requires_review():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        await _op(db, seller.id, KizOperationType.WITHDRAWAL, days=1)
        rows = [_report_row("S1", "srid-sale", 5), _report_row("R1", "srid-sale", 3)]

        payload, _ = await _run(seller, db, rows, [_excise_row(4, "srid-sale", 178496)])

        item = payload["returns"][0]
        assert item["selected"] is False
        assert item["needs_review"] is True
        assert payload["summary"]["returns_needing_review"] == 1


@pytest.mark.asyncio
async def test_live_status_in_circulation_is_not_returned_again():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        await _op(db, seller.id, KizOperationType.RETURN, days=1)
        rows = [_report_row("S1", "srid-sale", 5), _report_row("R1", "srid-sale", 3)]

        payload, calls = await _run(seller, db, rows, [_excise_row(4, "srid-sale", 178496)], live_status="INTRODUCED")

        assert True in calls
        assert payload["returns"] == []


@pytest.mark.asyncio
async def test_sales_report_failure_is_reported_and_batch_is_still_built():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)

        payload, _ = await _run(seller, db, [], report_error=RuntimeError("429"))

        assert payload["summary"]["sales_report_failed"] is True
        assert payload["returns"] == []


@pytest.mark.asyncio
async def test_malformed_report_row_is_skipped():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        rows = [{"saleID": "R1", "srid": "srid-sale", "date": "not-a-date"}]

        payload, _ = await _run(seller, db, rows, [_excise_row(4, "srid-sale", 178496)])

        assert payload["summary"]["sales_report_events"] == 0


@pytest.mark.asyncio
async def test_auto_queue_does_not_return_code_sold_later():
    """A signed batch marks the old cancelled order as withdrawn: its later sale must block the return."""
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        order = _sold_order(seller.id, status=OrderStatus.CANCELLED, wb_status="canceled_by_client")
        db.add(order)
        db.add(KizEvent(
            id=str(uuid.uuid4()), seller_id=seller.id, clean_cis=CIS, event_type="SALE", occurred_at=_days_ago(3),
            source="wb_excise", external_id=str(uuid.uuid4()), srid="srid-new",
        ))
        await db.commit()

        _, returns = await collect_auto_kiz_candidates(seller, db)

        assert returns == []


@pytest.mark.asyncio
async def test_auto_queue_still_returns_cancelled_order_without_later_sale():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        order = _sold_order(seller.id, status=OrderStatus.CANCELLED, wb_status="canceled_by_client")
        db.add(order)
        await db.commit()

        _, returns = await collect_auto_kiz_candidates(seller, db)

        assert [o.id for o in returns] == [order.id]


@pytest.mark.asyncio
async def test_auto_queue_does_not_withdraw_returned_order_with_sold_status():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        db.add(_sold_order(seller.id, status=OrderStatus.CANCELLED, kiz_status=KizStatus.RETURNED))
        await db.commit()

        withdrawals, _ = await collect_auto_kiz_candidates(seller, db)

        assert withdrawals == []
