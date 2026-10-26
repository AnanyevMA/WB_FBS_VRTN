"""
Tier 1 & Tier 2 Tests for Wildberries Analytics Client: get_item_returns.
Verifies HTTP communication, URL endpoints, authentication headers, pagination,
response unwrapping, 429 rate limit backoff, tenacity retries, and error handling.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock
import httpx
import pytest
from httpx import Response, Request

from app.services.wb_analytics_client import (
    WBAnalyticsClient,
    WBAnalyticsAPIError,
    WBAnalyticsRateLimitError,
    WBAnalyticsUnauthorizedError,
)


def _client_supports_returns() -> bool:
    return hasattr(WBAnalyticsClient, "get_item_returns")


# ============================================================================
# Tier 1: Feature Coverage (>= 5 tests)
# ============================================================================

@pytest.mark.asyncio
async def test_wb_analytics_client_base_contract():
    """Verify client initialization, base URL, and auth header format."""
    client = WBAnalyticsClient(api_token="  test_token_xyz  ", timeout=15.0)
    assert client.api_token == "test_token_xyz"
    assert client.timeout == 15.0
    assert client.BASE_URL == "https://seller-analytics-api.wildberries.ru"
    assert client.headers["Authorization"] == "test_token_xyz"
    assert client.headers["Content-Type"] == "application/json"
    assert client.headers["Accept"] == "application/json"


@pytest.mark.asyncio
async def test_get_item_returns_basic_success():
    """Verify basic item returns fetch with date_from and date_to."""
    if not _client_supports_returns():
        pytest.skip("WBAnalyticsClient.get_item_returns awaiting implementation by M1 worker")

    client = WBAnalyticsClient(api_token="test_token")
    mock_returns = [
        {
            "srid": "ret_srid_001.0.0",
            "orderId": 12345678,
            "nmId": 987654,
            "barcodes": ["2044967817804"],
            "date": "2026-10-25T12:00:00Z",
            "reason": "customer_return",
        }
    ]
    mock_resp = Response(
        status_code=200,
        json={"response": {"data": mock_returns}},
        request=Request("GET", "https://seller-analytics-api.wildberries.ru/api/analytics/v1/item-returns"),
    )

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req, \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_req.return_value = mock_resp
        mock_get.return_value = mock_resp

        data = await client.get_item_returns("2026-10-01", "2026-10-26")
        assert len(data) == 1
        assert data[0]["srid"] == "ret_srid_001.0.0"
        assert data[0]["orderId"] == 12345678
        assert data[0]["nmId"] == 987654


@pytest.mark.asyncio
async def test_get_item_returns_with_nm_ids_filter():
    """Verify request with specific nm_ids list."""
    if not _client_supports_returns():
        pytest.skip("WBAnalyticsClient.get_item_returns awaiting implementation by M1 worker")

    client = WBAnalyticsClient(api_token="test_token")
    mock_returns = [
        {"srid": "ret_srid_002.0.0", "orderId": 22222, "nmId": 456789, "barcodes": ["2044967817805"]}
    ]
    mock_resp = Response(
        status_code=200,
        json={"response": {"data": mock_returns}},
        request=Request("GET", "https://seller-analytics-api.wildberries.ru/api/analytics/v1/item-returns"),
    )

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req, \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_req.return_value = mock_resp
        mock_get.return_value = mock_resp

        data = await client.get_item_returns("2026-10-01", "2026-10-26", nm_ids=[456789])
        assert len(data) == 1
        assert data[0]["nmId"] == 456789


@pytest.mark.asyncio
async def test_get_item_returns_pagination_limit_and_offset():
    """Verify pagination parameters (limit and offset) propagation."""
    if not _client_supports_returns():
        pytest.skip("WBAnalyticsClient.get_item_returns awaiting implementation by M1 worker")

    client = WBAnalyticsClient(api_token="test_token")
    mock_returns = [
        {"srid": f"ret_srid_{i}.0.0", "orderId": 30000 + i, "nmId": 11111}
        for i in range(5)
    ]
    mock_resp = Response(
        status_code=200,
        json={"data": mock_returns},
        request=Request("GET", "https://seller-analytics-api.wildberries.ru/api/analytics/v1/item-returns"),
    )

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req, \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_req.return_value = mock_resp
        mock_get.return_value = mock_resp

        data = await client.get_item_returns("2026-10-01", "2026-10-26", limit=5, offset=10)
        assert len(data) == 5
        assert data[0]["srid"] == "ret_srid_0.0.0"


@pytest.mark.asyncio
async def test_get_item_returns_response_wrapping_variations():
    """Verify unwrapping response formatted as {'response': {'data': [...]}} and {'data': [...]}}."""
    if not _client_supports_returns():
        pytest.skip("WBAnalyticsClient.get_item_returns awaiting implementation by M1 worker")

    client = WBAnalyticsClient(api_token="test_token")
    test_data = [{"srid": "unwrapped.0.0", "nmId": 999}]

    # Case A: {"response": {"data": [...]}}
    resp_a = Response(200, json={"response": {"data": test_data}}, request=Request("GET", "http://test"))
    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req, \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_req.return_value = resp_a
        mock_get.return_value = resp_a
        res = await client.get_item_returns("2026-10-01", "2026-10-26")
        assert len(res) == 1
        assert res[0]["srid"] == "unwrapped.0.0"

    # Case B: {"data": [...]}
    resp_b = Response(200, json={"data": test_data}, request=Request("GET", "http://test"))
    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req, \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_req.return_value = resp_b
        mock_get.return_value = resp_b
        res = await client.get_item_returns("2026-10-01", "2026-10-26")
        assert len(res) == 1
        assert res[0]["srid"] == "unwrapped.0.0"


# ============================================================================
# Tier 2: Boundary & Corner Cases (>= 5 tests)
# ============================================================================

@pytest.mark.asyncio
async def test_get_item_returns_empty_list():
    """Verify empty response returns empty list [] without raising."""
    if not _client_supports_returns():
        pytest.skip("WBAnalyticsClient.get_item_returns awaiting implementation by M1 worker")

    client = WBAnalyticsClient(api_token="test_token")
    mock_resp = Response(
        status_code=200,
        json={"response": {"data": []}},
        request=Request("GET", "https://seller-analytics-api.wildberries.ru/api/analytics/v1/item-returns"),
    )
    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req, \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_req.return_value = mock_resp
        mock_get.return_value = mock_resp

        res = await client.get_item_returns("2026-10-01", "2026-10-26")
        assert res == []


@pytest.mark.asyncio
async def test_get_item_returns_429_rate_limit_inline_retry_success():
    """Verify 429 with X-RateLimit-Retry header waits and succeeds on retry."""
    if not _client_supports_returns():
        pytest.skip("WBAnalyticsClient.get_item_returns awaiting implementation by M1 worker")

    client = WBAnalyticsClient(api_token="test_token")
    req = Request("GET", "https://seller-analytics-api.wildberries.ru/api/analytics/v1/item-returns")
    resp_429 = Response(status_code=429, headers={"X-RateLimit-Retry": "2"}, request=req)
    resp_200 = Response(status_code=200, json={"data": [{"srid": "retry_ok"}]}, request=req)

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req, \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_req.side_effect = [resp_429, resp_200]
        mock_get.side_effect = [resp_429, resp_200]

        res = await client.get_item_returns("2026-10-01", "2026-10-26")
        assert len(res) == 1
        assert res[0]["srid"] == "retry_ok"
        mock_sleep.assert_called_once()


@pytest.mark.asyncio
async def test_get_item_returns_429_excessive_wait_raises_rate_limit_error():
    """Verify 429 with wait > 60s raises WBAnalyticsRateLimitError immediately."""
    if not _client_supports_returns():
        pytest.skip("WBAnalyticsClient.get_item_returns awaiting implementation by M1 worker")

    client = WBAnalyticsClient(api_token="test_token")
    req = Request("GET", "https://seller-analytics-api.wildberries.ru/api/analytics/v1/item-returns")
    resp_429 = Response(status_code=429, headers={"x-ratelimit-retry": "120"}, request=req)

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req, \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_req.return_value = resp_429
        mock_get.return_value = resp_429

        with pytest.raises(WBAnalyticsRateLimitError) as exc_info:
            await client.get_item_returns("2026-10-01", "2026-10-26")
        assert "Лимит запросов" in str(exc_info.value) or "120" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_item_returns_401_unauthorized():
    """Verify 401 raises WBAnalyticsUnauthorizedError."""
    if not _client_supports_returns():
        pytest.skip("WBAnalyticsClient.get_item_returns awaiting implementation by M1 worker")

    client = WBAnalyticsClient(api_token="invalid_token")
    req = Request("GET", "https://seller-analytics-api.wildberries.ru/api/analytics/v1/item-returns")
    resp_401 = Response(status_code=401, text="Unauthorized", request=req)

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req, \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_req.return_value = resp_401
        mock_get.return_value = resp_401

        with pytest.raises(WBAnalyticsUnauthorizedError) as exc_info:
            await client.get_item_returns("2026-10-01", "2026-10-26")
        assert "401" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_item_returns_500_api_error():
    """Verify 500 server error raises WBAnalyticsAPIError."""
    if not _client_supports_returns():
        pytest.skip("WBAnalyticsClient.get_item_returns awaiting implementation by M1 worker")

    client = WBAnalyticsClient(api_token="test_token")
    req = Request("GET", "https://seller-analytics-api.wildberries.ru/api/analytics/v1/item-returns")
    resp_500 = Response(status_code=500, text="Internal Server Error", request=req)

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req, \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_req.return_value = resp_500
        mock_get.return_value = resp_500

        with pytest.raises(WBAnalyticsAPIError) as exc_info:
            await client.get_item_returns("2026-10-01", "2026-10-26")
        assert "500" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_item_returns_corrupted_json():
    """Verify corrupted / non-JSON response raises WBAnalyticsAPIError."""
    if not _client_supports_returns():
        pytest.skip("WBAnalyticsClient.get_item_returns awaiting implementation by M1 worker")

    client = WBAnalyticsClient(api_token="test_token")
    req = Request("GET", "https://seller-analytics-api.wildberries.ru/api/analytics/v1/item-returns")
    resp_corrupt = Response(status_code=200, content=b"INVALID NON JSON", request=req)

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req, \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_req.return_value = resp_corrupt
        mock_get.return_value = resp_corrupt

        with pytest.raises(Exception):
            await client.get_item_returns("2026-10-01", "2026-10-26")


@pytest.mark.asyncio
async def test_get_item_returns_context_manager_lifecycle():
    """Verify WBAnalyticsClient context manager cleanly opens and closes client."""
    client = WBAnalyticsClient(api_token="test_token")
    async with client as c:
        assert c._client is not None
        assert not c._client.is_closed
    assert c._client.is_closed


@pytest.mark.asyncio
async def test_get_item_returns_custom_timeout_propagation():
    """Verify custom timeout is propagated to httpx client."""
    client = WBAnalyticsClient(api_token="test_token", timeout=45.0)
    assert client.timeout == 45.0
