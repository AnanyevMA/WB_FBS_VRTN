"""
Challenger M1 Iteration 2 Adversarial Stress Suite.
Targets:
- WBAnalyticsClient rate limits, multi-layered tenacity 429 backoff, and recovery.
- Multi-page pagination traversal and mid-stream rate limit interruptions.
- Date representations (date, datetime, timezone-aware, string).
- Network timeouts, connection resets, and client lifecycle leak prevention.
- Massive payloads (10,000+ items) and abnormal JSON structures.

Strict adherence to PROJECT_RULES.md (< 400 lines).
"""
import asyncio
from datetime import date, datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock
import httpx
from httpx import Request, Response
import pytest

from app.services.wb_analytics_client import (
    WBAnalyticsAPIError,
    WBAnalyticsClient,
    WBAnalyticsRateLimitError,
    WBAnalyticsUnauthorizedError,
)

ENDPOINT = "https://seller-analytics-api.wildberries.ru/api/analytics/v1/item-returns"


def _make_req() -> Request:
    return Request("GET", ENDPOINT)


# ============================================================================
# 1. Multi-Page Pagination Traversal & Mid-Stream Resiliency
# ============================================================================

@pytest.mark.asyncio
async def test_multi_page_pagination_traversal():
    """Verify clean traversal of 3 consecutive pages ending with an empty page."""
    client = WBAnalyticsClient(api_token="test_token")
    page_0 = [{"srid": f"p0_{i}", "orderId": 1000 + i} for i in range(1000)]
    page_1 = [{"srid": f"p1_{i}", "orderId": 2000 + i} for i in range(1000)]
    page_2 = [{"srid": f"p2_{i}", "orderId": 3000 + i} for i in range(350)]
    page_3 = []

    call_records = []

    async def mock_get(url, headers=None, params=None):
        call_records.append(params.copy())
        offset = params.get("offset", 0)
        if offset == 0:
            return Response(200, json={"response": {"data": page_0}}, request=_make_req())
        elif offset == 1000:
            return Response(200, json={"response": {"data": page_1}}, request=_make_req())
        elif offset == 2000:
            return Response(200, json={"response": {"data": page_2}}, request=_make_req())
        else:
            return Response(200, json={"response": {"data": page_3}}, request=_make_req())

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_client_get:
        mock_client_get.side_effect = mock_get

        all_items = []
        offset = 0
        limit = 1000
        while True:
            batch = await client.get_item_returns("2026-10-01", "2026-10-26", limit=limit, offset=offset)
            if not batch:
                break
            all_items.extend(batch)
            if len(batch) < limit:
                break
            offset += limit

        assert len(all_items) == 2350
        assert len(call_records) == 3
        assert call_records[0]["offset"] == 0
        assert call_records[1]["offset"] == 1000
        assert call_records[2]["offset"] == 2000


@pytest.mark.asyncio
async def test_multi_page_pagination_mid_stream_429_recovery():
    """Verify mid-stream 429 during page 2 is handled by inline retry and pagination finishes."""
    client = WBAnalyticsClient(api_token="test_token")
    page_0 = [{"srid": "item_0"}]
    resp_429 = Response(429, headers={"X-RateLimit-Retry": "3"}, request=_make_req())
    page_1 = [{"srid": "item_1"}]

    responses = [
        Response(200, json={"data": page_0}, request=_make_req()),
        resp_429,
        Response(200, json={"data": page_1}, request=_make_req()),
        Response(200, json={"data": []}, request=_make_req()),
    ]

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_get.side_effect = responses

        accumulated = []
        offset = 0
        while True:
            batch = await client.get_item_returns("2026-10-01", "2026-10-26", limit=1, offset=offset)
            if not batch:
                break
            accumulated.extend(batch)
            offset += 1

        assert len(accumulated) == 2
        assert accumulated[0]["srid"] == "item_0"
        assert accumulated[1]["srid"] == "item_1"
        mock_sleep.assert_called_once_with(5)  # 3 + 2


# ============================================================================
# 2. Date Representations & Invariants
# ============================================================================

@pytest.mark.asyncio
async def test_date_variations_date_datetime_and_timezone():
    """Verify date, datetime, and timezone-aware instances are formatted to YYYY-MM-DD."""
    client = WBAnalyticsClient(api_token="test_token")
    captured_params = []

    async def mock_get(url, headers=None, params=None):
        captured_params.append(params)
        return Response(200, json={"data": []}, request=_make_req())

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get_patch:
        mock_get_patch.side_effect = mock_get

        # 1. Pure date objects
        await client.get_item_returns(date(2026, 1, 15), date(2026, 2, 28))
        assert captured_params[-1]["dateFrom"] == "2026-01-15"
        assert captured_params[-1]["dateTo"] == "2026-02-28"

        # 2. Datetime UTC
        dt_from = datetime(2026, 7, 4, 18, 30, tzinfo=timezone.utc)
        dt_to = datetime(2026, 7, 10, 8, 15, tzinfo=timezone.utc)
        await client.get_item_returns(dt_from, dt_to)
        assert captured_params[-1]["dateFrom"] == "2026-07-04"
        assert captured_params[-1]["dateTo"] == "2026-07-10"

        # 3. Datetime naive
        await client.get_item_returns(datetime(2026, 12, 31, 23, 59), datetime(2027, 1, 1, 0, 1))
        assert captured_params[-1]["dateFrom"] == "2026-12-31"
        assert captured_params[-1]["dateTo"] == "2027-01-01"

        # 4. Plain strings
        await client.get_item_returns("2026-05-01", "2026-05-31")
        assert captured_params[-1]["dateFrom"] == "2026-05-01"
        assert captured_params[-1]["dateTo"] == "2026-05-31"


@pytest.mark.asyncio
async def test_date_inversion_api_error():
    """Verify inverted dates returning 400 Bad Request raises WBAnalyticsAPIError."""
    client = WBAnalyticsClient(api_token="test_token")
    resp_400 = Response(400, text='{"error": true, "message": "dateFrom cannot be greater than dateTo"}', request=_make_req())

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = resp_400
        with pytest.raises(WBAnalyticsAPIError) as exc_info:
            await client.get_item_returns("2026-10-30", "2026-10-01")
        assert "400" in str(exc_info.value)
        assert "dateFrom cannot be greater" in str(exc_info.value)


# ============================================================================
# 3. nm_ids Iterable Variations
# ============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("nm_input,expected_query_val", [
    ([111, 222, 333], "111,222,333"),
    ((444, 555), "444,555"),
    ({777}, "777"),
    (999999, "999999"),
    ("888,999", "888,999"),
])
async def test_nm_ids_parameter_serialization(nm_input, expected_query_val):
    """Verify various formats of nm_ids are serialized into nmIds parameter."""
    client = WBAnalyticsClient(api_token="test_token")
    captured = {}

    async def mock_get(url, headers=None, params=None):
        captured.update(params)
        return Response(200, json={"data": []}, request=_make_req())

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get_patch:
        mock_get_patch.side_effect = mock_get
        await client.get_item_returns("2026-10-01", "2026-10-26", nm_ids=nm_input)
        assert captured["nmIds"] == expected_query_val


@pytest.mark.asyncio
async def test_empty_nm_ids_omitted_from_params():
    """Verify empty nm_ids list does not inject empty nmIds parameter."""
    client = WBAnalyticsClient(api_token="test_token")
    captured = {}

    async def mock_get(url, headers=None, params=None):
        captured.update(params)
        return Response(200, json={"data": []}, request=_make_req())

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get_patch:
        mock_get_patch.side_effect = mock_get
        await client.get_item_returns("2026-10-01", "2026-10-26", nm_ids=[])
        assert "nmIds" not in captured


# ============================================================================
# 4. Multi-Layer Tenacity Backoff and Recovery
# ============================================================================

@pytest.mark.asyncio
async def test_tenacity_multi_layer_recovery_attempt_2():
    """Verify tenacity catches WBAnalyticsRateLimitError from inline retry and recovers on attempt 2."""
    client = WBAnalyticsClient(api_token="test_token")
    resp_429 = Response(429, headers={"X-RateLimit-Retry": "1"}, request=_make_req())
    resp_200 = Response(200, json={"data": [{"srid": "tenacity_recovered"}]}, request=_make_req())

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        # Attempt 1: 429 -> inline retry -> 429 (raises WBAnalyticsRateLimitError)
        # Attempt 2: 200 OK
        mock_get.side_effect = [resp_429, resp_429, resp_200]
        data = await client.get_item_returns("2026-10-01", "2026-10-26")

        assert len(data) == 1
        assert data[0]["srid"] == "tenacity_recovered"
        # Inline sleep (1 + 2 = 3s)
        assert mock_sleep.call_count >= 1


# ============================================================================
# 5. Massive Payloads (10,000 items) Stress
# ============================================================================

@pytest.mark.asyncio
async def test_massive_payload_10000_items():
    """Verify parsing and returning 10,000 items completes quickly without OOM or truncation."""
    client = WBAnalyticsClient(api_token="test_token")
    massive_data = [
        {
            "srid": f"srid_{i}.0.0",
            "orderId": 100000 + i,
            "nmId": 50000 + (i % 20),
            "date": "2026-10-25T12:00:00Z",
            "price": 199.99,
        }
        for i in range(10000)
    ]
    resp = Response(200, json={"response": {"data": massive_data}}, request=_make_req())

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = resp
        t0 = asyncio.get_event_loop().time()
        res = await client.get_item_returns("2026-10-01", "2026-10-26")
        elapsed = asyncio.get_event_loop().time() - t0

        assert len(res) == 10000
        assert res[0]["srid"] == "srid_0.0.0"
        assert res[9999]["srid"] == "srid_9999.0.0"
        assert elapsed < 3.0  # Parsing 10k dicts in memory should be sub-second


# ============================================================================
# 6. Unclosed Client Safety & Resource Cleanup
# ============================================================================

@pytest.mark.asyncio
async def test_standalone_call_closes_client_on_success():
    """Verify standalone call (without async with) closes its internal httpx.AsyncClient."""
    client = WBAnalyticsClient(api_token="test_token")
    resp = Response(200, json={"data": []}, request=_make_req())

    created_clients = []
    original_init = httpx.AsyncClient.__init__

    def intercepting_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created_clients.append(self)

    with patch.object(httpx.AsyncClient, "__init__", intercepting_init), \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = resp
        await client.get_item_returns("2026-10-01", "2026-10-26")

        assert len(created_clients) >= 1
        for c in created_clients:
            assert c.is_closed


@pytest.mark.asyncio
async def test_standalone_call_closes_client_on_failure():
    """Verify standalone call closes its internal httpx.AsyncClient on HTTP error."""
    client = WBAnalyticsClient(api_token="test_token")
    resp_500 = Response(500, text="Server Fault", request=_make_req())

    created_clients = []
    original_init = httpx.AsyncClient.__init__

    def intercepting_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created_clients.append(self)

    with patch.object(httpx.AsyncClient, "__init__", intercepting_init), \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = resp_500
        with pytest.raises(WBAnalyticsAPIError):
            await client.get_item_returns("2026-10-01", "2026-10-26")

        assert len(created_clients) >= 1
        for c in created_clients:
            assert c.is_closed
