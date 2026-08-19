"""
Unit & Integration tests for CZClient, SUZ 3.0.38 endpoints, Task Queues, and EncryptionService.
"""
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
import pytest

from app.agent_manifest import load_manifest
from app.celery_app import celery_app
from app.services.cz_client import CZClient
from app.services.encryption import EncryptionService, encrypt, decrypt
from app.models.seller import Seller
from app.schemas.seller import SellerCreate, SellerResponse


def test_encryption_service_compatibility():
    """Verify EncryptionService class methods work identically to standalone functions."""
    raw_secret = "test-wb-api-token-secret-12345"
    encrypted = EncryptionService.encrypt(raw_secret)
    assert encrypted != raw_secret

    decrypted = EncryptionService.decrypt(encrypted)
    assert decrypted == raw_secret

    # Verify standalone function interop
    assert decrypt(encrypted) == raw_secret
    assert EncryptionService.decrypt(encrypt(raw_secret)) == raw_secret


@pytest.mark.asyncio
async def test_cz_client_authenticate_flow():
    """Verify 2-step challenge-response authentication in CZClient."""
    client = CZClient(inn="7700000000", token=None, cert_thumbprint="mock_thumbprint")

    mock_key_resp = MagicMock()
    mock_key_resp.is_success = True
    mock_key_resp.content = True
    mock_key_resp.json.return_value = {
        "uuid": "test-uuid-1234",
        "data": "test-challenge-data",
    }

    mock_signin_resp = MagicMock()
    mock_signin_resp.is_success = True
    mock_signin_resp.content = True
    mock_signin_resp.json.return_value = {
        "token": "generated-cz-jwt-session-token",
    }

    mock_http_client = AsyncMock()
    mock_http_client.get.return_value = mock_key_resp
    mock_http_client.post.return_value = mock_signin_resp
    client._client = mock_http_client

    with patch("app.services.cz_client.sign_document", new_callable=AsyncMock) as mock_sign:
        mock_sign.return_value = "mock_cms_detached_signature_base64"

        token = await client.authenticate()

        assert token == "generated-cz-jwt-session-token"
        assert client.token == "generated-cz-jwt-session-token"
        assert mock_http_client.get.called
        assert mock_http_client.post.called
        assert mock_sign.called


@pytest.mark.asyncio
async def test_cz_client_suz_endpoints_and_cises_info():
    """Verify SUZ 3.0.38 emission endpoints and True API cises/info."""
    client = CZClient(inn="7700000000", token="valid-token", oms_id="mock-oms-id")

    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        # 1. get_order_status
        mock_req.return_value = {"orderId": "ord-1", "orderStatus": "READY"}
        status_res = await client.get_order_status("ord-1", gtin="04601234567890")
        assert status_res["orderStatus"] == "READY"
        mock_req.assert_called_with("GET", "/api/v3/order/status", params={"omsId": "mock-oms-id", "orderId": "ord-1", "gtin": "04601234567890"}, sign_request=True)

        # 2. get_emission_codes
        mock_req.return_value = {"codes": ["010460123456789021TEST1", "010460123456789021TEST2"]}
        codes = await client.get_emission_codes("ord-1", gtin="04601234567890", quantity=2)
        assert len(codes) == 2
        assert codes[0] == "010460123456789021TEST1"

        # 3. report_utilisation
        mock_req.return_value = {"reportId": "rep-1", "status": "ACCEPTED"}
        rep_res = await client.report_utilisation("lp", ["010460123456789021TEST1"])
        assert rep_res["status"] == "ACCEPTED"

        # 4. get_cises_info
        mock_req.return_value = [{"cis": "010460123456789021TEST1", "status": "INTRODUCED", "ownerInn": "7700000000", "ogvs": []}]
        cis_info = await client.get_cises_info(["010460123456789021TEST1"])
        assert len(cis_info) == 1
        assert cis_info[0]["status"] == "INTRODUCED"

        # 5. get_cises_short_list (True API v719.0)
        mock_req.return_value = [{"cis": "010460123456789021TEST1", "status": "INTRODUCED"}]
        short_info = await client.get_cises_short_list(["010460123456789021TEST1"])
        assert len(short_info) == 1
        mock_req.assert_called_with("POST", "/api/v3/true-api/cises/short/list", json_body=["010460123456789021TEST1"], sign_request=False)

        # 6. get_document_receipt (True API v719.0)
        mock_req.return_value = {"receiptId": "rec-123", "status": "SUCCESS"}
        receipt = await client.get_document_receipt("doc-999")
        assert receipt["receiptId"] == "rec-123"
        mock_req.assert_called_with("GET", "/api/v3/true-api/documents/receipts/doc-999", sign_request=False)

        # 7. get_seller_mod_list (True API v719.0)
        mock_req.return_value = [{"fiasId": "fias-uuid-1", "modName": "Основной склад"}]
        mods = await client.get_seller_mod_list("7700000000")
        assert len(mods) == 1
        assert mods[0]["fiasId"] == "fias-uuid-1"
        mock_req.assert_called_with("GET", "/api/v3/true-api/organizations/7700000000/mod", sign_request=False)


def test_agent_task_queue_decorators_match_manifest():
    """Verify all Celery task objects have queue decorator parameters matching agents_config.json."""
    celery_app.loader.import_default_modules()
    manifest_path = Path(__file__).resolve().parent.parent / "agents_config.json"
    manifest = load_manifest(manifest_path)

    for agent in manifest.agents:
        task_obj = celery_app.tasks.get(agent.celery_task)
        assert task_obj is not None, f"Task {agent.celery_task} not registered in celery_app!"
        # Check task.queue property
        assert task_obj.queue == agent.queue, (
            f"Queue mismatch for task {agent.celery_task}: "
            f"task has queue='{task_obj.queue}', manifest specifies queue='{agent.queue}'"
        )


def test_seller_oms_and_cert_thumbprint_fields():
    """Verify Seller model and schemas support cz_oms_id and cryptopro_cert_thumbprint."""
    create_payload = {
        "name": "Seller Test OMS",
        "wb_api_token": "test-wb-token",
        "cz_inn": "7712345678",
        "cz_oms_id": "8ed74f90-0119-48f2-b289-379707934e2f",
        "cryptopro_cert_thumbprint": "a1b2c3d4e5f67890",
        "polling_interval_minutes": 5,
    }
    schema = SellerCreate(**create_payload)
    assert schema.cz_oms_id == "8ed74f90-0119-48f2-b289-379707934e2f"
    assert schema.cryptopro_cert_thumbprint == "a1b2c3d4e5f67890"

    seller_model = Seller(
        name=schema.name,
        wb_api_token_encrypted=encrypt(schema.wb_api_token),
        cz_inn=schema.cz_inn,
        cz_oms_id=schema.cz_oms_id,
        cryptopro_cert_thumbprint=schema.cryptopro_cert_thumbprint,
    )
    assert seller_model.cz_oms_id == "8ed74f90-0119-48f2-b289-379707934e2f"
    assert seller_model.cryptopro_cert_thumbprint == "a1b2c3d4e5f67890"
