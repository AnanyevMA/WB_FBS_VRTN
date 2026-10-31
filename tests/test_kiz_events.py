"""
Tests for the KIZ event log and its background collectors.
The log only gathers facts: it makes no decisions, creates no batches and must not lose or repeat events.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.audit import AuditLog
from app.models.kiz import KizSignatureBatch
from app.models.kiz_event import (
    EVENT_RETURN,
    EVENT_SALE,
    SOURCE_ARCHIVE_FILE,
    SOURCE_EXCISE,
    SOURCE_FINANCE,
    SOURCE_ITEM_RETURNS,
    SOURCE_ORDERS,
    KizEvent,
)
from app.models.order import KizStatus, Order, OrderStatus
from app.models.seller import Seller
from app.models.wb_finance import WbSalesReportRow
from app.services import kiz_event_collectors as collectors
from app.services.encryption import encrypt
from app.services.kiz_event_runner import run_collector
from app.services.kiz_event_service import save_events
from app.services.wb_analytics_client import WBAnalyticsClient
from app.services.wb_archive_service import get_archive_months

SELLER_ID = "seller-events"
CIS_A = "0104600000000001215TESTKIZAAAAA"
CIS_B = "0104600000000002215TESTKIZBBBBB"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        session.add(Seller(
            id=SELLER_ID, name="Events Seller", wb_api_token_encrypted=encrypt("wb"), cz_inn="7701234567", is_active=True,
        ))
        await session.commit()
        yield session
    await engine.dispose()


def _seller():
    return SimpleNamespace(id=SELLER_ID, wb_api_token_encrypted=encrypt("wb"))


def _finance_row(rrd_id, cis=CIS_A, oper="Продажа", doc="Продажа", sale_dt=NOW, srid="s1"):
    return WbSalesReportRow(
        seller_id=SELLER_ID, rrd_id=rrd_id, clean_cis=cis, kiz=cis, seller_oper_name=oper, doc_type_name=doc,
        sale_dt=sale_dt, rr_date=date(2026, 10, 25), srid=srid, retail_amount=Decimal("100.00"),
    )


def _order(order_id, status=OrderStatus.DELIVERED, kiz_status=KizStatus.VALIDATED, wb_status="sold", kiz_code=CIS_A, doc=None):
    return Order(
        id=order_id, seller_id=SELLER_ID, status=status, kiz_status=kiz_status, wb_status=wb_status, kiz_code=kiz_code,
        cz_withdrawal_doc_id=doc, price=Decimal("100.00"), article="ART", name="Товар", wb_created_at=NOW,
    )


async def _events(db, source=None):
    query = select(KizEvent).order_by(KizEvent.occurred_at)
    if source:
        query = query.where(KizEvent.source == source)
    return (await db.execute(query)).scalars().all()


def _draft(external_id="e1", **overrides):
    return {
        "source": SOURCE_EXCISE, "external_id": external_id, "clean_cis": CIS_A, "event_type": EVENT_SALE,
        "occurred_at": NOW, **overrides,
    }


# --- Saving ----------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_repeated_save_adds_nothing(db):
    assert await save_events(db, SELLER_ID, [_draft()]) == {"inserted": 1, "updated": 0}
    await db.commit()
    assert await save_events(db, SELLER_ID, [_draft()]) == {"inserted": 0, "updated": 0}
    await db.commit()
    assert len(await _events(db)) == 1


@pytest.mark.asyncio
async def test_duplicates_within_one_save_become_one_event(db):
    assert (await save_events(db, SELLER_ID, [_draft(), _draft()]))["inserted"] == 1


@pytest.mark.asyncio
async def test_later_poll_fills_receipt_but_never_rewrites_the_fact(db):
    await save_events(db, SELLER_ID, [_draft()])
    await db.commit()
    later = NOW + timedelta(days=3)
    counts = await save_events(db, SELLER_ID, [_draft(receipt_number="R-1", occurred_at=later, event_type=EVENT_RETURN)])
    await db.commit()
    event = (await _events(db))[0]
    assert counts["updated"] == 1
    assert event.receipt_number == "R-1"
    assert event.event_type == EVENT_SALE
    assert event.occurred_at.replace(tzinfo=timezone.utc) == NOW


@pytest.mark.asyncio
async def test_same_external_id_in_another_source_is_a_separate_event(db):
    await save_events(db, SELLER_ID, [_draft(), _draft(source=SOURCE_FINANCE)])
    await db.commit()
    assert len(await _events(db)) == 2


# --- Finance ---------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finance_keeps_only_sales_and_returns(db):
    db.add_all([
        _finance_row(1),
        _finance_row(2, oper="Возврат", doc="Возврат", sale_dt=NOW + timedelta(days=1)),
        _finance_row(3, oper="Логистика", doc=None, sale_dt=NOW + timedelta(days=2)),
        _finance_row(4, oper="Доставка", doc=None, sale_dt=NOW + timedelta(days=2)),
        _finance_row(5, oper="Компенсация скидки по программе лояльности"),
        _finance_row(6, oper="Добровольная компенсация при возврате"),
        _finance_row(7, cis=None),
    ])
    await db.commit()
    result = await collectors.collect_finance(db, _seller())
    assert sorted((d["external_id"], d["event_type"]) for d in result.drafts) == [("1", EVENT_SALE), ("2", EVENT_RETURN)]


def test_finance_uses_report_date_when_sale_date_is_missing():
    row = SimpleNamespace(
        seller_oper_name="Продажа", sale_dt=None, rr_date=date(2026, 10, 25), clean_cis=CIS_A, rrd_id=9, kiz=CIS_A,
        srid="s", retail_amount=None, retail_price=None, vendor_code="V", nm_id=1, subject_name="S",
    )
    assert collectors.finance_row_to_draft(row)["occurred_at"] == datetime(2026, 10, 25, tzinfo=timezone.utc)


# --- Excise ----------------------------------------------------------------------------------------------------------

def _excise_row(**overrides):
    return {
        "excise_short": CIS_A, "srid": "sr1", "fiscal_doc_number": 123, "fiscal_drive_number": "FN1",
        "fiscal_dt": "2026-10-20T10:00:00", "price": 500, "nm_id": 77, "name": "Товар", "operation_type_id": 1, **overrides,
    }


@pytest.mark.asyncio
async def test_excise_sale_keeps_receipt_details(db):
    with patch.object(collectors, "fetch_wb_excise_data", new_callable=AsyncMock, return_value=[_excise_row()]):
        result = await collectors.collect_excise(db, _seller())
    draft = result.drafts[0]
    assert draft["event_type"] == EVENT_SALE
    assert (draft["receipt_number"], draft["fn_number"], draft["receipt_date"]) == ("123", "FN1", "2026-10-20")


@pytest.mark.asyncio
async def test_excise_unknown_operation_type_is_not_recorded(db):
    rows = [_excise_row(operation_type_id=2), _excise_row(excise_short="")]
    with patch.object(collectors, "fetch_wb_excise_data", new_callable=AsyncMock, return_value=rows):
        result = await collectors.collect_excise(db, _seller())
    assert result.drafts == []
    assert result.skipped == {"operation_type_2": 1, "no_kiz": 1}


# --- Item returns ----------------------------------------------------------------------------------------------------

def _return_row(**overrides):
    return {
        "returnStatus": "Выдано", "kiz": "", "srid": "", "orderId": 0, "shkId": 555, "nmId": 77, "subjectName": "Товар",
        "completedDt": "2026-10-20T10:00:00Z", **overrides,
    }


def test_item_return_prefers_kiz_of_the_row():
    draft = collectors.item_return_to_draft(_return_row(kiz=CIS_A, orderId=9), {}, {9: CIS_B})
    assert draft["clean_cis"] == CIS_A
    assert draft["event_type"] == EVENT_RETURN


def test_item_return_falls_back_to_order_then_srid():
    assert collectors.item_return_to_draft(_return_row(orderId=9), {}, {9: CIS_B})["clean_cis"] == CIS_B
    assert collectors.item_return_to_draft(_return_row(srid="x"), {"x": CIS_A}, {})["clean_cis"] == CIS_A


def test_item_return_is_never_matched_by_article():
    assert collectors.item_return_to_draft(_return_row(nmId=77), {}, {}) is None


@pytest.mark.asyncio
async def test_only_delivered_returns_are_recorded(db):
    rows = {
        "archive": [_return_row(kiz=CIS_A), _return_row(shkId=556, kiz="")],
        "active": [_return_row(shkId=557, kiz=CIS_B, returnStatus="Погружен в машину до СЦ/РЦ")],
    }

    async def report(self, date_from, date_to, status, page_size=1000):
        return rows[status]

    with patch.object(WBAnalyticsClient, "get_item_returns_report", report):
        result = await collectors.collect_item_returns(db, _seller())
    assert [d["clean_cis"] for d in result.drafts] == [CIS_A]
    assert result.seen == 3
    assert result.skipped == {"not_delivered_yet": 1, "unmatched": 1}


@pytest.mark.asyncio
async def test_report_client_reads_report_field_page_by_page():
    req = Request("GET", "https://seller-analytics-api.wildberries.ru/api/analytics/v1/item-returns")
    pages = [
        Response(200, json={"count": 3, "report": [{"shkId": 1}, {"shkId": 2}]}, request=req),
        Response(200, json={"count": 3, "report": [{"shkId": 3}]}, request=req),
    ]
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=pages) as get:
        rows = await WBAnalyticsClient("t").get_item_returns_report("2026-08-01", "2026-10-31", "archive", page_size=2)
    assert [r["shkId"] for r in rows] == [1, 2, 3]
    assert [c.kwargs["params"]["offset"] for c in get.await_args_list] == [0, 2]
    assert get.await_args_list[0].kwargs["params"]["status"] == "archive"


@pytest.mark.asyncio
async def test_report_client_treats_204_as_no_rows():
    req = Request("GET", "https://seller-analytics-api.wildberries.ru/api/analytics/v1/item-returns")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=Response(204, request=req)):
        assert await WBAnalyticsClient("t").get_item_returns_report("2026-08-01", "2026-10-31", "active") == []


# --- FBS orders ------------------------------------------------------------------------------------------------------

def test_sold_order_is_a_sale():
    drafts = collectors.order_to_drafts(_order(1), NOW)
    assert [d["event_type"] for d in drafts] == [EVENT_SALE]
    assert drafts[0]["occurred_at"] == NOW


def test_order_in_progress_is_not_an_event():
    order = _order(1, status=OrderStatus.DELIVERING, wb_status="on_way")
    assert collectors.order_to_drafts(order, NOW) == []


def test_cancelled_order_without_withdrawal_is_only_a_return():
    drafts = collectors.order_to_drafts(_order(1, status=OrderStatus.CANCELLED, wb_status="canceled"), NOW)
    assert [d["event_type"] for d in drafts] == [EVENT_RETURN]


def test_cancelled_withdrawn_order_returns_after_its_own_sale():
    """This pair used to be sorted the other way, so the fake sale won over the return."""
    order = _order(1, status=OrderStatus.CANCELLED, kiz_status=KizStatus.WITHDRAWN, wb_status="canceled", doc="d1")
    drafts = {d["event_type"]: d["occurred_at"] for d in collectors.order_to_drafts(order, NOW)}
    assert drafts[EVENT_RETURN] > drafts[EVENT_SALE]


def test_later_real_sale_outranks_a_cancelled_order():
    order = _order(1, status=OrderStatus.CANCELLED, wb_status="canceled")
    cancel = collectors.order_to_drafts(order, NOW)[0]
    assert cancel["occurred_at"] < NOW + timedelta(days=1)


@pytest.mark.asyncio
async def test_orders_collector_skips_orders_without_kiz(db):
    db.add_all([_order(1), _order(2, kiz_code=None)])
    await db.commit()
    result = await collectors.collect_orders(db, _seller())
    assert result.seen == 1
    assert [d["order_id"] for d in result.drafts] == [1]


def _wb_client_with(statuses):
    client = MagicMock()
    client.get_orders_status = AsyncMock(return_value=statuses)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_status_refresh_sees_cancellation_after_delivery(db):
    now = datetime.now(timezone.utc)
    order = _order(1, wb_status=None)
    order.wb_created_at = now
    db.add(order)
    await db.commit()
    client = _wb_client_with([{"id": 1, "wbStatus": "canceled_by_client", "supplierStatus": "complete"}])
    with patch("app.services.wb_client.WBClient", return_value=client):
        result = await collectors.refresh_delivered_order_statuses(db, _seller())
    assert result.changed == 1
    assert order.status == OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_status_refresh_does_not_touch_withdrawn_order(db):
    order = _order(1, kiz_status=KizStatus.WITHDRAWN, doc="d1")
    order.wb_created_at = datetime.now(timezone.utc)
    db.add(order)
    await db.commit()
    client = _wb_client_with([{"id": 1, "wbStatus": "canceled_by_client", "supplierStatus": "complete"}])
    with patch("app.services.wb_client.WBClient", return_value=client):
        result = await collectors.refresh_delivered_order_statuses(db, _seller())
    assert result.changed == 0
    assert order.status == OrderStatus.DELIVERED


# --- Archive file ----------------------------------------------------------------------------------------------------

def _analysis():
    return {
        "withdrawals": [{"order_id": 1, "kiz_code": CIS_A, "receipt_number": "R1", "fn_number": "F1", "receipt_date": "2026-10-20", "price": 100, "article": "A", "name": "N"}],
        "returns": [{"order_id": 2, "sticker_id": "S2", "kiz_code": CIS_B, "receipt_date": "2026-10-21"}, {"kiz_code": ""}],
    }


@pytest.mark.asyncio
async def test_archive_upload_records_events_once(db):
    assert await collectors.record_archive_file_events(db, SELLER_ID, _analysis()) == {"inserted": 2, "updated": 0}
    await db.commit()
    assert (await collectors.record_archive_file_events(db, SELLER_ID, _analysis()))["inserted"] == 0
    await db.commit()
    events = {e.event_type: e for e in await _events(db, SOURCE_ARCHIVE_FILE)}
    assert events[EVENT_SALE].receipt_number == "R1"
    assert events[EVENT_RETURN].clean_cis == CIS_B


@pytest.mark.asyncio
async def test_journal_failure_does_not_break_archive_upload(db):
    with patch.object(collectors, "save_events", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
        assert await collectors.record_archive_file_events(db, SELLER_ID, _analysis()) is None


# --- Runner ----------------------------------------------------------------------------------------------------------

async def _audit(db):
    return (await db.execute(select(AuditLog).where(AuditLog.action == "KIZ_EVENTS_SYNC"))).scalars().all()


@pytest.mark.asyncio
async def test_runner_saves_events_and_records_result(db):
    db.add(_finance_row(1))
    await db.commit()
    payload = await run_collector(db, await db.get(Seller, SELLER_ID), SOURCE_FINANCE)
    assert (payload["status"], payload["inserted"], payload["seen"]) == ("ok", 1, 1)
    assert len(await _events(db, SOURCE_FINANCE)) == 1
    assert (await _audit(db))[0].payload["source"] == SOURCE_FINANCE


@pytest.mark.asyncio
async def test_runner_reports_zero_rows_as_no_data(db):
    payload = await run_collector(db, await db.get(Seller, SELLER_ID), SOURCE_FINANCE)
    assert payload["status"] == "no_data"
    assert (await _audit(db))[0].payload["status"] == "no_data"


@pytest.mark.asyncio
async def test_runner_records_source_failure_and_saves_nothing(db):
    failing = {SOURCE_EXCISE: AsyncMock(side_effect=RuntimeError("WB is down"))}
    with patch.dict("app.services.kiz_event_runner.COLLECTORS", failing):
        payload = await run_collector(db, await db.get(Seller, SELLER_ID), SOURCE_EXCISE)
    assert payload["status"] == "error"
    assert "WB is down" in (await _audit(db))[0].error
    assert await _events(db) == []


@pytest.mark.asyncio
async def test_frequent_orders_run_leaves_no_audit_noise_when_nothing_changed(db):
    db.add(_order(1))
    await db.commit()
    seller = await db.get(Seller, SELLER_ID)
    first = await run_collector(db, seller, SOURCE_ORDERS)
    second = await run_collector(db, seller, SOURCE_ORDERS)
    assert (first["inserted"], second["inserted"]) == (1, 0)
    assert len(await _audit(db)) == 1


@pytest.mark.asyncio
async def test_collectors_create_no_batches_and_keep_order_status(db):
    """On this stage the log only gathers data: the old reconciliation keeps deciding."""
    order = _order(1, status=OrderStatus.CANCELLED, wb_status="canceled")
    db.add(order)
    db.add(_finance_row(1))
    await db.commit()
    seller = await db.get(Seller, SELLER_ID)
    await run_collector(db, seller, SOURCE_ORDERS)
    await run_collector(db, seller, SOURCE_FINANCE)
    assert await db.scalar(select(func.count()).select_from(KizSignatureBatch)) == 0
    assert order.status == OrderStatus.CANCELLED and order.kiz_code == CIS_A


# --- Archive window --------------------------------------------------------------------------------------------------

def test_archive_months_start_three_months_back():
    """WB only keeps orders created more than 3 months ago in the archive."""
    assert get_archive_months(3, NOW) == [(2026, 7), (2026, 6), (2026, 5)]
    assert get_archive_months(1, datetime(2026, 2, 10, tzinfo=timezone.utc)) == [(2025, 11)]


# --- KIZ restoring ---------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_does_not_restore_kiz_of_cancelled_orders(db):
    from app.api.orders import sync_all_orders_cz_status

    active = _order(10, status=OrderStatus.ASSEMBLING, kiz_status=KizStatus.PENDING, wb_status=None, kiz_code=None)
    cancelled = _order(11, status=OrderStatus.CANCELLED, kiz_status=KizStatus.PENDING, wb_status="canceled", kiz_code=None)
    db.add_all([active, cancelled])
    await db.commit()

    with patch("app.services.wb_client.WBClient.get_orders_meta", new_callable=AsyncMock, return_value={"orders": []}) as meta:
        try:
            await sync_all_orders_cz_status(seller_id=SELLER_ID, db=db)
        except Exception:
            pass  # Only the request sent to WB matters here
    meta.assert_awaited_once_with([10])
