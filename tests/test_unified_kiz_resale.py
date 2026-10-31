"""
Tests for repeat sales in the unified KIZ reconciliation.
A past withdrawal must not hide a sale made after the code came back to circulation,
and a returned code must not be withdrawn because of an old sale or a pending batch item.
"""
import random
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.database import AsyncSessionLocal, init_db
from app.models.kiz import BatchStatus, KizOperation, KizOperationType, KizProductInfo, KizSignatureBatch
from app.models.order import KizStatus, Order, OrderStatus
from app.models.seller import Seller
from app.models.wb_finance import WbSalesReportRow
from app.services.unified_kiz_batch_service import create_unified_kiz_signature_batch

INN = "190207495060"
SERVICE = "app.services.unified_kiz_batch_service"
CIS = "0104630199252674215RESALETEST1"


@pytest.fixture(autouse=True)
async def setup_test_db():
    await init_db()


def _days_ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _excise_row(days: int, srid: str, receipt, price: float = 4251) -> dict:
    return {
        "excise_short": CIS,
        "fiscal_doc_number": receipt,
        "fiscal_drive_number": f"FN{receipt}" if receipt else None,
        "fiscal_dt": _days_ago(days).strftime("%Y-%m-%d"),
        "price": price,
        "nm_id": 600627180,
        "srid": srid,
    }


def _record(status: str = "INTRODUCED", checked_at: datetime = None) -> KizProductInfo:
    return KizProductInfo(
        kiz_code=CIS,
        clean_cis=CIS,
        cz_status=status,
        cz_owner_inn=INN,
        raw_cz_payload={"ownerInn": INN, "status": status},
        checked_at=checked_at,
    )


async def _seller(db) -> Seller:
    seller = Seller(id=str(uuid.uuid4()), name="Resale", wb_api_token_encrypted="mock", cz_inn=INN)
    db.add(seller)
    await db.commit()
    return seller


async def _withdrawal_op(db, seller_id: str, days: int, status: str = "SUCCESS") -> None:
    db.add(KizOperation(
        seller_id=seller_id, kiz_code=CIS, operation=KizOperationType.WITHDRAWAL,
        status=status, cz_doc_status="CHECKED_OK", created_at=_days_ago(days),
    ))
    await db.commit()


async def _run(seller, db, excise_rows, live=None, live_error=None):
    calls = []

    async def fake_verify(db, seller, kiz_codes, force_refresh=False):
        calls.append(force_refresh)
        if not force_refresh:
            return {c: _record(checked_at=_days_ago(0) - timedelta(hours=6)) for c in kiz_codes}
        if live_error:
            raise live_error
        return {c: live or _record(checked_at=datetime.now(timezone.utc) + timedelta(seconds=1)) for c in kiz_codes}

    with patch(f"{SERVICE}.fetch_wb_excise_data", return_value=excise_rows), \
         patch(f"{SERVICE}.batch_verify_and_sync_cises", side_effect=fake_verify):
        res = await create_unified_kiz_signature_batch(seller=seller, db=db, days=30)
    batch = await db.get(KizSignatureBatch, res["batch_id"])
    return batch.data_payload, calls


@pytest.mark.asyncio
async def test_resale_after_old_withdrawal_uses_receipt_of_last_sale():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        await _withdrawal_op(db, seller.id, days=20)
        rows = [_excise_row(19, "srid-old", 243306, 4373), _excise_row(3, "srid-new", 245227, 4251)]

        payload, calls = await _run(seller, db, rows)

        assert True in calls
        assert payload["summary"]["sales_needing_withdrawal"] == 1
        assert payload["summary"]["sales_resold_confirmed"] == 1
        item = payload["withdrawals"][0]
        assert item["receipt_number"] == "245227"
        assert item["fn_number"] == "FN245227"
        assert item["price"] == 4251
        assert item["order_id"] is None
        assert item["selected"] is True
        assert item["needs_review"] is False


@pytest.mark.asyncio
async def test_resale_with_returned_order_does_not_link_old_order():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        db.add(Order(
            id=random.randint(10**9, 10**12), seller_id=seller.id, status=OrderStatus.CANCELLED,
            wb_status="sold", kiz_code=CIS, kiz_status=KizStatus.RETURNED, cz_return_doc_id="ret-doc",
            cz_doc_status="CHECKED_OK", wb_created_at=_days_ago(25), price=3683,
        ))
        await db.commit()

        payload, _ = await _run(seller, db, [_excise_row(1, "srid-new", 777)])

        assert payload["summary"]["sales_needing_withdrawal"] == 1
        assert payload["withdrawals"][0]["order_id"] is None
        assert payload["withdrawals"][0]["receipt_number"] == "777"


@pytest.mark.asyncio
async def test_resale_without_receipt_waits_and_does_not_reuse_old_receipt():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        await _withdrawal_op(db, seller.id, days=8)
        rows = [_excise_row(8, "srid-old", 39059), _excise_row(1, "srid-new", None, 3578)]
        db.add(WbSalesReportRow(
            seller_id=seller.id, rrd_id=random.randint(10**9, 10**12), srid="srid-old",
            doc_type_name="Возврат", rr_date=date.today() - timedelta(days=7),
            sale_dt=_days_ago(7), retail_amount=3482,
        ))
        await db.commit()

        payload, _ = await _run(seller, db, rows)

        assert payload["withdrawals"] == []
        assert payload["summary"]["sales_waiting_receipt"] == 1
        assert payload["summary"]["sales_needing_withdrawal"] == 0


@pytest.mark.asyncio
async def test_new_sale_without_previous_withdrawal_waits_for_receipt():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)

        payload, calls = await _run(seller, db, [_excise_row(1, "srid-new", None)])

        assert payload["withdrawals"] == []
        assert payload["summary"]["sales_waiting_receipt"] == 1
        assert True not in calls


@pytest.mark.asyncio
async def test_returned_sale_in_pending_auto_batch_is_not_withdrawn():
    """The auto queue proposes sold orders that were returned: their item must not count as a new sale."""
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        await _withdrawal_op(db, seller.id, days=6)
        db.add(WbSalesReportRow(
            seller_id=seller.id, rrd_id=random.randint(10**9, 10**12), srid="srid-old",
            doc_type_name="Возврат", rr_date=date.today() - timedelta(days=5),
            sale_dt=_days_ago(5), retail_amount=3722,
        ))
        db.add(KizSignatureBatch(
            id=str(uuid.uuid4()), seller_id=seller.id, source="auto", status=BatchStatus.PENDING_SIGNATURE,
            data_payload={"withdrawals": [{
                "kiz_code": CIS, "order_id": None, "price": 3722, "needs_withdrawal": True,
                "selected": True, "receipt_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            }], "returns": []},
        ))
        await db.commit()

        payload, calls = await _run(seller, db, [_excise_row(6, "srid-old", 111)])

        assert payload["withdrawals"] == []
        assert payload["summary"]["sales_resold_confirmed"] == 0
        assert True not in calls


@pytest.mark.asyncio
async def test_sale_covered_by_withdrawal_requires_review():
    """Sold, withdrawn, then returned to circulation without a WB return: propose it unchecked."""
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        await _withdrawal_op(db, seller.id, days=8)

        payload, _ = await _run(seller, db, [_excise_row(10, "srid-sale", 114433)])

        item = payload["withdrawals"][0]
        assert item["receipt_number"] == "114433"
        assert item["needs_withdrawal"] is True
        assert item["selected"] is False
        assert item["needs_review"] is True
        assert payload["summary"]["sales_needing_review"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [
    _record(status="RETIRED", checked_at=datetime(2100, 1, 1, tzinfo=timezone.utc)),
    _record(status="INTRODUCED", checked_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
])
async def test_resale_needs_fresh_introduced_status(live):
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        await _withdrawal_op(db, seller.id, days=20)

        payload, calls = await _run(seller, db, [_excise_row(3, "srid-new", 245227)], live=live)

        assert True in calls
        assert payload["withdrawals"] == []
        assert payload["summary"]["sales_resold_confirmed"] == 0


@pytest.mark.asyncio
async def test_true_api_failure_keeps_code_out_and_reports_it():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        await _withdrawal_op(db, seller.id, days=20)

        payload, _ = await _run(seller, db, [_excise_row(3, "srid-new", 245227)], live_error=RuntimeError("502"))

        assert payload["withdrawals"] == []
        assert payload["summary"]["resale_check_failed"] is True


@pytest.mark.asyncio
async def test_in_progress_withdrawal_is_not_sent_again():
    async with AsyncSessionLocal() as db:
        seller = await _seller(db)
        await _withdrawal_op(db, seller.id, days=20, status="IN_PROGRESS")

        payload, calls = await _run(seller, db, [_excise_row(3, "srid-new", 245227)])

        assert payload["withdrawals"] == []
        assert True not in calls
