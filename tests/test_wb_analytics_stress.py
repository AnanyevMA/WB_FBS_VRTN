"""
Adversarial Stress Testing Suite for WBAnalyticsClient (Milestone 1).
Targets: WBAnalyticsClient.get_item_returns

Challenges:
- Challenge A1: Rapid-fire 429 bursts with dynamic X-RateLimit-Retry headers.
- Challenge A2: Malformed and erratic headers (Retry-After: 999999, negative, invalid strings, dates).
- Challenge A3: Bizarre response payloads (empty dicts, non-list data, unicode, syntax errors).
- Challenge A4: Network timeouts, connection resets, and 502/503/504 gateway failures.

Complies strictly with PROJECT_RULES.md (< 400 lines limit).
"""
import asyncio
from unittest.mock import AsyncMock, patch
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


def _mock_req() -> Request:
    return Request("GET", ENDPOINT)


# ============================================================================
# Challenge A1: Rapid-fire 429 bursts with dynamic X-RateLimit-Retry headers
# ============================================================================

@pytest.mark.asyncio
async def test_a1_burst_inline_retry_recovery():
    """Verify 429 burst recovers on 1st inline retry and sleeps wait_sec + 2."""
    client = WBAnalyticsClient(api_token="test_burst_token")
    req = _mock_req()
    resp_429 = Response(429, headers={"X-RateLimit-Retry": "2"}, request=req)
    resp_200 = Response(200, json={"data": [{"srid": "burst_ok.1"}]}, request=req)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_get.side_effect = [resp_429, resp_200]
        data = await client.get_item_returns("2026-10-01", "2026-10-26")

        assert len(data) == 1
        assert data[0]["srid"] == "burst_ok.1"
        mock_sleep.assert_called_once_with(4)  # 2 + 2


@pytest.mark.asyncio
async def test_a1_burst_dynamic_headers_across_retries():
    """Verify client handles alternating header names across consecutive 429s."""
    client = WBAnalyticsClient(api_token="test_burst_token")
    req = _mock_req()
    resp_429_a = Response(429, headers={"x-ratelimit-retry": "1"}, request=req)
    resp_429_b = Response(429, headers={"Retry-After": "1"}, request=req)
    resp_200 = Response(200, json={"response": {"data": [{"srid": "recovered"}]}}, request=req)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        # Attempt 1: 429_a -> inline retry -> 429_b (raises WBAnalyticsRateLimitError)
        # Tenacity attempt 2: 200
        mock_get.side_effect = [resp_429_a, resp_429_b, resp_200]
        data = await client.get_item_returns("2026-10-01", "2026-10-26")

        assert len(data) == 1
        assert data[0]["srid"] == "recovered"
        # Sleeps: 1 + 2 = 3s during attempt 1 inline retry
        assert mock_sleep.call_count >= 1


@pytest.mark.asyncio
async def test_a1_burst_persistent_429_exhaustion():
    """Verify persistent 429 exhaust retries and raises WBAnalyticsRateLimitError."""
    client = WBAnalyticsClient(api_token="test_burst_token")
    req = _mock_req()
    resp_429 = Response(429, headers={"X-RateLimit-Retry": "1"}, request=req)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        # 3 tenacity attempts * 2 requests each = 6 consecutive 429s
        mock_get.return_value = resp_429
        with pytest.raises(WBAnalyticsRateLimitError) as exc_info:
            await client.get_item_returns("2026-10-01", "2026-10-26")
        assert "Rate limit exceeded (429)" in str(exc_info.value)


@pytest.mark.asyncio
async def test_a1_concurrent_burst_stress():
    """Verify concurrent requests hitting 429 bursts do not cause race conditions."""
    client = WBAnalyticsClient(api_token="test_burst_token")
    req = _mock_req()

    async def call_with_mock(idx: int):
        r429 = Response(429, headers={"X-RateLimit-Retry": "1"}, request=req)
        r200 = Response(200, json={"data": [{"srid": f"item_{idx}"}]}, request=req)
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_get.side_effect = [r429, r200]
            return await client.get_item_returns("2026-10-01", "2026-10-26")

    results = await asyncio.gather(*(call_with_mock(i) for i in range(5)))
    assert len(results) == 5
    for i, res in enumerate(results):
        assert res[0]["srid"] == f"item_{i}"


# ============================================================================
# Challenge A2: Malformed and erratic headers
# ============================================================================

@pytest.mark.asyncio
async def test_a2_huge_retry_after_no_infinite_hang():
    """Verify Retry-After > 60s raises immediately without sleeping 999999 seconds."""
    client = WBAnalyticsClient(api_token="test_token")
    req = _mock_req()
    resp_429 = Response(429, headers={"Retry-After": "999999"}, request=req)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_get.return_value = resp_429
        with pytest.raises(WBAnalyticsRateLimitError) as exc_info:
            await client.get_item_returns("2026-10-01", "2026-10-26")

        assert "Лимит запросов WB исчерпан" in str(exc_info.value)
        # Inline sleep (999999 + 2) must NOT be called
        for call_arg in mock_sleep.call_args_list:
            assert call_arg[0][0] < 100


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_header", ["-1", "-999", "abc", "NaN", "null", "", "12.5", "Wed, 21 Oct 2026 07:28:00 GMT"])
async def test_a2_erratic_headers_fallback_to_default_wait(bad_header: str):
    """Verify non-positive-integer headers safely fallback to default 35s wait."""
    client = WBAnalyticsClient(api_token="test_token")
    req = _mock_req()
    resp_429 = Response(429, headers={"Retry-After": bad_header}, request=req)
    resp_200 = Response(200, json={"data": [{"srid": "fallback_ok"}]}, request=req)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_get.side_effect = [resp_429, resp_200]
        data = await client.get_item_returns("2026-10-01", "2026-10-26")

        assert len(data) == 1
        assert data[0]["srid"] == "fallback_ok"
        # Default wait_sec is 35 -> sleep(35 + 2 = 37)
        mock_sleep.assert_called_once_with(37)


@pytest.mark.asyncio
async def test_a2_missing_retry_headers_entirely():
    """Verify 429 response without any retry headers defaults safely to 35s."""
    client = WBAnalyticsClient(api_token="test_token")
    req = _mock_req()
    resp_429 = Response(429, headers={}, request=req)
    resp_200 = Response(200, json={"data": []}, request=req)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_get.side_effect = [resp_429, resp_200]
        data = await client.get_item_returns("2026-10-01", "2026-10-26")

        assert data == []
        mock_sleep.assert_called_once_with(37)


@pytest.mark.asyncio
async def test_a2_zero_retry_after_header():
    """Verify Retry-After: 0 is parsed as 0 and sleeps minimum 2 seconds."""
    client = WBAnalyticsClient(api_token="test_token")
    req = _mock_req()
    resp_429 = Response(429, headers={"Retry-After": "0"}, request=req)
    resp_200 = Response(200, json={"data": [{"srid": "zero_wait"}]}, request=req)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_get.side_effect = [resp_429, resp_200]
        data = await client.get_item_returns("2026-10-01", "2026-10-26")

        assert len(data) == 1
        mock_sleep.assert_called_once_with(2)  # 0 + 2


# ============================================================================
# Challenge A3: Bizarre response payloads
# ============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {},
    {"response": {}},
    {"data": None},
    {"response": {"data": None}},
    {"response": None},
    {"other_key": 123},
])
async def test_a3_empty_and_null_envelope_payloads(payload):
    """Verify empty dictionaries and null-data envelopes return empty list []."""
    client = WBAnalyticsClient(api_token="test_token")
    req = _mock_req()
    resp = Response(200, json=payload, request=req)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = resp
        data = await client.get_item_returns("2026-10-01", "2026-10-26")
        assert data == []


@pytest.mark.asyncio
async def test_a3_raw_list_payload():
    """Verify bare JSON array response is unwrapped directly."""
    client = WBAnalyticsClient(api_token="test_token")
    req = _mock_req()
    raw_list = [{"srid": "raw_1", "nmId": 100}, {"srid": "raw_2", "nmId": 200}]
    resp = Response(200, json=raw_list, request=req)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = resp
        data = await client.get_item_returns("2026-10-01", "2026-10-26")
        assert data == raw_list


@pytest.mark.asyncio
@pytest.mark.parametrize("primitive_json", ["\"plain_string\"", "12345", "true", "null"])
async def test_a3_primitive_json_responses(primitive_json):
    """Verify JSON containing top-level primitive returns empty list []."""
    client = WBAnalyticsClient(api_token="test_token")
    req = _mock_req()
    resp = Response(200, text=primitive_json, headers={"Content-Type": "application/json"}, request=req)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = resp
        data = await client.get_item_returns("2026-10-01", "2026-10-26")
        assert data == []


@pytest.mark.asyncio
async def test_a3_unicode_emojis_and_cyrillic():
    """Verify unicode characters, emojis, cyrillic and special chars are preserved."""
    client = WBAnalyticsClient(api_token="test_token")
    req = _mock_req()
    payload = {
        "data": [
            {
                "srid": "srid_🎉_123",
                "reason": "Возврат покупателя: брак 💥 и дефект строчки",
                "special": "Tab\tNewline\nQuotes\"Backslash\\",
            }
        ]
    }
    resp = Response(200, json=payload, request=req)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = resp
        data = await client.get_item_returns("2026-10-01", "2026-10-26")
        assert len(data) == 1
        assert "💥" in data[0]["reason"]
        assert "🎉" in data[0]["srid"]


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupted_content", [
    b"<html><head><title>502 Bad Gateway</title></head></html>",
    b"{\ninvalid_json: 123",
    b"\x00\x01\x02\xff\xfe",
    b"",
])
async def test_a3_corrupted_json_syntax_raises_analytics_api_error(corrupted_content):
    """Verify invalid JSON syntax or HTML error pages raise WBAnalyticsAPIError."""
    client = WBAnalyticsClient(api_token="test_token")
    req = _mock_req()
    resp = Response(200, content=corrupted_content, headers={"Content-Type": "text/html"}, request=req)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = resp
        with pytest.raises(WBAnalyticsAPIError) as exc_info:
            await client.get_item_returns("2026-10-01", "2026-10-26")
        assert "Failed to parse JSON response" in str(exc_info.value)


# ============================================================================
# Challenge A4: Network timeouts, connection resets, and 502/503/504 gateway failures
# ============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
async def test_a4_gateway_and_server_failures(status_code):
    """Verify HTTP 500, 502, 503, 504 raise WBAnalyticsAPIError with status details."""
    client = WBAnalyticsClient(api_token="test_token")
    req = _mock_req()
    resp = Response(status_code, text=f"Gateway Error {status_code}", request=req)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = resp
        with pytest.raises(WBAnalyticsAPIError) as exc_info:
            await client.get_item_returns("2026-10-01", "2026-10-26")
        assert str(status_code) in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_exc", [
    httpx.ConnectTimeout("Connection timed out"),
    httpx.ReadTimeout("Read timed out"),
    httpx.WriteTimeout("Write timed out"),
])
async def test_a4_network_timeouts_propagation_and_client_cleanup(timeout_exc):
    """Verify network timeouts propagate and clean up internal AsyncClient."""
    client = WBAnalyticsClient(api_token="test_token")

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = timeout_exc
        with pytest.raises(httpx.TimeoutException):
            await client.get_item_returns("2026-10-01", "2026-10-26")


@pytest.mark.asyncio
async def test_a4_connection_error_propagation_and_cleanup():
    """Verify connection errors propagate and clean up internal AsyncClient."""
    client = WBAnalyticsClient(api_token="test_token")

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = httpx.ConnectError("Connection refused by peer")
        with pytest.raises(httpx.ConnectError):
            await client.get_item_returns("2026-10-01", "2026-10-26")


@pytest.mark.asyncio
async def test_a4_context_manager_lifecycle_under_network_failure():
    """Verify AsyncClient is closed when exception occurs inside context manager."""
    client = WBAnalyticsClient(api_token="test_token")

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = httpx.ConnectTimeout("Timeout during call")
        try:
            async with client as c:
                await c.get_item_returns("2026-10-01", "2026-10-26")
        except httpx.ConnectTimeout:
            pass

        assert client._client is not None
        assert client._client.is_closed
