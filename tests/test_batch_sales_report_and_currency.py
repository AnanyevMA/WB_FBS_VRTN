"""
Batch 76138a74 (22.11.2026): the WB sales report hit the rate limit, the button still marked 11 withdrawals
for signing, and 7 of those sales had already been returned. One withdrawal also carried 120.65 BYN as rubles.
"""
from datetime import datetime, timezone

import httpx
import pytest

from app.services import wb_statistics_client
from app.services.kiz_event_collectors import excise_row_to_draft
from app.services.unified_kiz_payload_builder import (
    SALES_REPORT_MISSING_NOTE,
    build_unified_withdrawals_payload,
    hold_items_without_sales_report,
)
from app.services.wb_prices import excise_price_rub
from app.services.wb_statistics_client import WBStatisticsAPIError, WBStatisticsClient
from app.services.wb_warehouse_sales_builder import build_warehouse_sale_item

CIS = "0104630199254144215Z+DVYM)pVecX"
SELLER_INN = "190207495060"
BELARUS_ROW = {
    "excise_short": CIS, "currency_name_short": "BYN", "name": "Беларусь", "price": 120.65,
    "srid": "eBU.r374daf4955534fe080b91a8e5bb53eb4.1.0", "fiscal_dt": "2026-11-08", "nm_id": 473414499, "operation_type_id": 1,
}


# --- excise-report currency ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("currency,price,expected", [
    ("руб", 3836, 3836.0), ("RUB", 100, 100.0), ("", 500, 500.0), (None, 500, 500.0),
    ("BYN", 120.65, None), ("KZT", 20640, None), ("AMD", 15370, None), ("руб", 0, None),
])
def test_excise_price_rub(currency, price, expected):
    assert excise_price_rub({"currency_name_short": currency, "price": price}) == expected


def test_journal_does_not_store_foreign_excise_price():
    draft = excise_row_to_draft(BELARUS_ROW, datetime.now(timezone.utc))
    assert draft["price"] is None
    assert draft["srid"] == BELARUS_ROW["srid"]


def test_warehouse_item_does_not_take_foreign_excise_price():
    item = build_warehouse_sale_item(record=BELARUS_ROW, clean_cis=CIS)
    assert item["price"] != 120.65


def _candidate(price: float) -> dict:
    return {"type": "SALE", "source": "wb_finance", "date": datetime(2026, 11, 8, tzinfo=timezone.utc),
            "srid": BELARUS_ROW["srid"], "price": price, "raw_kiz": CIS}


def _withdrawals(history_price: float):
    history = {CIS: [
        _candidate(3298.86),
        {"source": "wb_excise_report", "type": "SALE", "date": datetime(2026, 11, 8, tzinfo=timezone.utc),
         "srid": BELARUS_ROW["srid"], "price": history_price, "receipt_number": "", "receipt_date": "2026-11-08"},
    ]}
    items, *_ = build_unified_withdrawals_payload(
        sales_candidates={CIS: _candidate(3298.86)},
        cz_info_map={CIS: {"status": "INTRODUCED", "ownerInn": SELLER_INN}},
        fbs_order_lookup={}, history_by_cis=history, seller_inn=SELLER_INN,
        now_utc=datetime(2026, 11, 22, tzinfo=timezone.utc),
    )
    return items


def test_batch_falls_back_to_ruble_price_when_excise_is_in_another_currency():
    # the batch service now stores 0 for a non-ruble excise row
    [item] = _withdrawals(history_price=excise_price_rub(BELARUS_ROW) or 0.0)
    assert item["price"] == 3298.86
    assert item["price_kopecks"] == 329886


def test_batch_still_prefers_ruble_excise_price():
    [item] = _withdrawals(history_price=3400.0)
    assert item["price"] == 3400.0


# --- missing WB sales report --------------------------------------------------------------------------------------

def test_items_are_unselected_when_sales_report_is_missing():
    withdrawals = [
        {"clean_cis": "a", "selected": True, "needs_review": False, "action_recommended": "✅ Готов к выводу"},
        {"clean_cis": "b", "selected": False, "needs_review": False, "action_recommended": "Уже выбыл"},
    ]
    returns = [{"clean_cis": "c", "selected": True, "needs_review": False, "action_recommended": "Вернуть"}]

    held = hold_items_without_sales_report(withdrawals, returns)

    assert held == 2
    assert [w["selected"] for w in withdrawals] == [False, False]
    assert withdrawals[0]["needs_review"] is True
    assert withdrawals[0]["action_recommended"].startswith(SALES_REPORT_MISSING_NOTE)
    assert withdrawals[1]["needs_review"] is False  # already unselected items keep their own reason
    assert returns[0]["selected"] is False and returns[0]["needs_review"] is True


# --- statistics API rate limit ------------------------------------------------------------------------------------

@pytest.fixture
def no_sleep(monkeypatch):
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(wb_statistics_client.asyncio, "sleep", fake_sleep)
    return waits


@pytest.mark.asyncio
async def test_sales_report_retries_once_after_rate_limit(no_sleep):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"X-Ratelimit-Retry": "41"}, json={"status": 429})
        return httpx.Response(200, json=[{"srid": "s1", "saleID": "R1"}])

    client = WBStatisticsClient("token", transport=httpx.MockTransport(handler))
    rows = await client.get_sales("2026-11-01")

    assert rows == [{"srid": "s1", "saleID": "R1"}]
    assert len(calls) == 2
    assert no_sleep == [42]


@pytest.mark.asyncio
async def test_sales_report_gives_up_on_long_rate_limit(no_sleep):
    client = WBStatisticsClient("token", transport=httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"X-Ratelimit-Retry": "600"}, json={"status": 429})))
    with pytest.raises(WBStatisticsAPIError, match="429"):
        await client.get_sales("2026-11-01")
    assert no_sleep == []


@pytest.mark.asyncio
async def test_sales_report_second_rate_limit_is_an_error(no_sleep):
    client = WBStatisticsClient("token", transport=httpx.MockTransport(
        lambda r: httpx.Response(429, json={"status": 429})))
    with pytest.raises(WBStatisticsAPIError, match="429"):
        await client.get_sales("2026-11-01")
    assert no_sleep == [61]
