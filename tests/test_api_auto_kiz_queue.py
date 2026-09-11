"""
API Tests for Auto KIZ Queue Endpoints (app/api/kiz/auto_queue.py)
"""
import pytest
import uuid
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import AsyncSessionLocal, init_db
from app.models.seller import Seller
from app.services.encryption import encrypt
from app.services.auth_service import create_access_token, ensure_initial_admin
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_auto_kiz_queue_api_endpoints():
    await init_db()

    async with AsyncSessionLocal() as session:
        admin_user = await ensure_initial_admin(session)
        auth_token = create_access_token(
            data={"sub": admin_user.id, "username": admin_user.username, "role": "admin", "is_superuser": True}
        )

        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="API Queue Seller",
            wb_api_token_encrypted=encrypt("test_wb_token"),
            cz_token_encrypted=encrypt("test_cz_token"),
            cz_inn="7709876543",
            auto_kiz_queue_enabled=True,
            auto_kiz_queue_hour=17,
            auto_kiz_queue_minute=0,
            auto_kiz_auto_sign_server=False,
            auto_kiz_manager_chat_id="12345678",
            is_active=True,
        )
        session.add(seller)
        await session.commit()

    headers = {"Authorization": f"Bearer {auth_token}"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. GET Settings
        get_res = await client.get(
            f"/api/v1/sellers/{seller_id}/kiz/auto-batch/settings",
            headers=headers
        )
        assert get_res.status_code == 200
        settings = get_res.json()
        assert settings["auto_kiz_queue_enabled"] is True
        assert settings["auto_kiz_queue_hour"] == 17
        assert settings["auto_kiz_queue_minute"] == 0
        assert settings["auto_kiz_auto_sign_server"] is False
        assert settings["auto_kiz_manager_chat_id"] == "12345678"

        # 2. PATCH Settings
        patch_payload = {
            "auto_kiz_queue_enabled": True,
            "auto_kiz_queue_hour": 18,
            "auto_kiz_queue_minute": 30,
            "auto_kiz_auto_sign_server": True,
            "auto_kiz_manager_chat_id": "87654321",
        }
        patch_res = await client.patch(
            f"/api/v1/sellers/{seller_id}/kiz/auto-batch/settings",
            json=patch_payload,
            headers=headers
        )
        assert patch_res.status_code == 200
        res_data = patch_res.json()
        assert res_data["success"] is True
        updated = res_data["settings"]
        assert updated["auto_kiz_queue_hour"] == 18
        assert updated["auto_kiz_queue_minute"] == 30
        assert updated["auto_kiz_auto_sign_server"] is True
        assert updated["auto_kiz_manager_chat_id"] == "87654321"

        # 3. POST Trigger (manual on-demand batch run)
        with patch("app.api.kiz.auto_queue.process_auto_kiz_queue_for_seller", new_callable=AsyncMock) as mock_process:
            mock_process.return_value = {
                "status": "created",
                "batch_id": "test-batch-123",
                "sales_count": 5,
                "returns_count": 2,
                "server_signed": False,
                "message": "Пакет успешно сформирован",
            }

            trigger_res = await client.post(
                f"/api/v1/sellers/{seller_id}/kiz/auto-batch/trigger",
                headers=headers
            )
            assert trigger_res.status_code == 200
            data = trigger_res.json()
            assert data["status"] == "created"
            assert data["batch_id"] == "test-batch-123"
            assert data["sales_count"] == 5
            assert data["returns_count"] == 2
