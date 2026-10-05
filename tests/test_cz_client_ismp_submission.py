import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx
from app.services.cz_client import CZClient, CZAPIError, CZUnauthorizedError

@pytest.mark.asyncio
async def test_post_ismp_document_success():
    client = CZClient(inn="190207495060", token="test-token")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '"doc-uuid-12345"'

    with patch.object(client, "_ensure_client", new_callable=AsyncMock):
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_resp)

        doc_id = await client._post_ismp_document(
            path="/api/v3/lk/documents/create?pg=lp",
            payload={"test": "data"},
            pg="lp",
        )
        assert doc_id == "doc-uuid-12345"
        assert client._client.post.call_count == 1
        call_url = client._client.post.call_args[0][0]
        assert "ismp.crpt.ru" in call_url

@pytest.mark.asyncio
async def test_post_ismp_document_retries_on_network_error():
    client = CZClient(inn="190207495060", token="test-token")
    mock_success = MagicMock()
    mock_success.status_code = 200
    mock_success.text = "doc-uuid-retry-ok"

    with patch.object(client, "_ensure_client", new_callable=AsyncMock), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        client._client = MagicMock()
        # Fail first with timeout, then succeed on retry 2
        client._client.post = AsyncMock(side_effect=[
            httpx.ReadTimeout("Timeout connecting to ismp"),
            mock_success,
        ])

        doc_id = await client._post_ismp_document(
            path="/api/v3/lk/documents/create?pg=lp",
            payload={"test": "data"},
            pg="lp",
        )
        assert doc_id == "doc-uuid-retry-ok"
        assert client._client.post.call_count == 2

@pytest.mark.asyncio
async def test_post_ismp_document_raises_422():
    client = CZClient(inn="190207495060", token="test-token")
    mock_422 = MagicMock()
    mock_422.status_code = 422
    mock_422.text = '{"error": "Invalid format"}'

    with patch.object(client, "_ensure_client", new_callable=AsyncMock):
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_422)

        with pytest.raises(CZAPIError) as exc_info:
            await client._post_ismp_document(
                path="/api/v3/lk/documents/create?pg=lp",
                payload={"test": "data"},
                pg="lp",
            )
        assert exc_info.value.status_code == 422

@pytest.mark.asyncio
async def test_post_ismp_document_raises_401():
    client = CZClient(inn="190207495060", token="test-token")
    mock_401 = MagicMock()
    mock_401.status_code = 401
    mock_401.text = '{"error": "Unauthorized"}'

    with patch.object(client, "_ensure_client", new_callable=AsyncMock):
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_401)

        with pytest.raises(CZUnauthorizedError):
            await client._post_ismp_document(
                path="/api/v3/lk/documents/create?pg=lp",
                payload={"test": "data"},
                pg="lp",
            )
