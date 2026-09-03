"""
Tests for Seller API Endpoints (PATCH, POST, GET) & Token/ChatID Preservation.

Ensures:
- PATCH /sellers/{id} preserves wb_api_token, cz_token, telegram_bot_token when passed as empty/whitespace/null.
- PATCH /sellers/{id} preserves telegram_chat_ids when passed as null or omitted.
- PATCH /sellers/{id} properly updates notification_mode, notification_schedule, and timezone.
- Validates notification_schedule format (HH:MM) and rejects invalid schedules with 422.
- Validates notification_mode ("instant" vs "scheduled") and rejects invalid values with 422.
- Validates IANA timezone and rejects invalid values with 422.
"""
import uuid
import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import init_db, AsyncSessionLocal
from app.models.seller import Seller
from app.services.encryption import encrypt, decrypt
from app.config import settings


async def _get_auth_headers(client: AsyncClient) -> dict:
    async with AsyncSessionLocal() as session:
        from app.services.auth_service import ensure_initial_admin
        await ensure_initial_admin(session)

    res = await client.post(
        "/api/v1/auth/login",
        json={"username": settings.admin_username, "password": settings.admin_password}
    )
    assert res.status_code == 200, f"Login failed: {res.text}"
    token = res.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_patch_seller_notification_settings_preserves_tokens_and_chat_ids():
    await init_db()
    seller_id = f"test-preserv-{uuid.uuid4().hex[:8]}"

    # 1. Insert existing seller with secret tokens and chat IDs
    async with AsyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name="Preservation Test Shop",
            wb_api_token_encrypted=encrypt("secret-wb-token-123"),
            cz_token_encrypted=encrypt("secret-cz-token-456"),
            telegram_bot_token_encrypted=encrypt("secret-tg-token-789"),
            telegram_chat_ids=["100200300", "400500600"],
            notification_mode="instant",
            notification_schedule=["10:00", "14:00", "18:00"],
            timezone="Europe/Moscow",
            is_active=True,
            polling_enabled=True,
        )
        session.add(seller)
        await session.commit()

    # 2. Issue PATCH with empty/whitespace tokens and null chat_ids
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _get_auth_headers(client)
        patch_payload = {
            "wb_api_token": "",
            "cz_token": "   ",
            "telegram_bot_token": None,
            "telegram_chat_ids": None,
            "notification_mode": "scheduled",
            "notification_schedule": ["09:00", "13:30", "17:00", "21:00"],
            "timezone": "Asia/Yekaterinburg",
        }
        res = await client.patch(
            f"/api/v1/sellers/{seller_id}",
            json=patch_payload,
            headers=headers,
        )
        assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
        data = res.json()

        assert data["notification_mode"] == "scheduled"
        assert data["notification_schedule"] == ["09:00", "13:30", "17:00", "21:00"]
        assert data["timezone"] == "Asia/Yekaterinburg"
        assert data["has_wb_token"] is True
        assert data["has_cz_token"] is True
        assert data["has_telegram_token"] is True
        assert data["telegram_chat_ids"] == ["100200300", "400500600"]

    # 3. Direct DB verification of encrypted values
    async with AsyncSessionLocal() as session:
        db_seller = await session.get(Seller, seller_id)
        assert db_seller is not None
        assert decrypt(db_seller.wb_api_token_encrypted) == "secret-wb-token-123"
        assert decrypt(db_seller.cz_token_encrypted) == "secret-cz-token-456"
        assert decrypt(db_seller.telegram_bot_token_encrypted) == "secret-tg-token-789"
        assert db_seller.telegram_chat_ids == ["100200300", "400500600"]
        assert db_seller.notification_mode == "scheduled"
        assert db_seller.notification_schedule == ["09:00", "13:30", "17:00", "21:00"]
        assert db_seller.timezone == "Asia/Yekaterinburg"


@pytest.mark.asyncio
async def test_patch_seller_updates_tokens_when_non_empty_values_provided():
    await init_db()
    seller_id = f"test-tok-upd-{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name="Token Update Shop",
            wb_api_token_encrypted=encrypt("old-wb-token"),
            cz_token_encrypted=encrypt("old-cz-token"),
            telegram_bot_token_encrypted=encrypt("old-tg-token"),
            telegram_chat_ids=["111"],
            is_active=True,
        )
        session.add(seller)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _get_auth_headers(client)
        res = await client.patch(
            f"/api/v1/sellers/{seller_id}",
            json={
                "wb_api_token": "brand-new-wb-token",
                "telegram_chat_ids": ["999888777"],
            },
            headers=headers,
        )
        assert res.status_code == 200

    async with AsyncSessionLocal() as session:
        db_seller = await session.get(Seller, seller_id)
        assert decrypt(db_seller.wb_api_token_encrypted) == "brand-new-wb-token"
        assert decrypt(db_seller.cz_token_encrypted) == "old-cz-token"  # preserved
        assert decrypt(db_seller.telegram_bot_token_encrypted) == "old-tg-token"  # preserved
        assert db_seller.telegram_chat_ids == ["999888777"]  # updated


@pytest.mark.asyncio
async def test_patch_seller_schedule_validation_errors():
    await init_db()
    seller_id = f"test-val-err-{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name="Validation Test Shop",
            wb_api_token_encrypted=encrypt("some-token"),
            is_active=True,
        )
        session.add(seller)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _get_auth_headers(client)
        # Invalid time format (hour > 23)
        res1 = await client.patch(
            f"/api/v1/sellers/{seller_id}",
            json={"notification_schedule": ["25:00"]},
            headers=headers,
        )
        assert res1.status_code == 422

        # Invalid time format (random string)
        res2 = await client.patch(
            f"/api/v1/sellers/{seller_id}",
            json={"notification_schedule": ["invalid_time"]},
            headers=headers,
        )
        assert res2.status_code == 422

        # Invalid notification mode
        res3 = await client.patch(
            f"/api/v1/sellers/{seller_id}",
            json={"notification_mode": "random_mode"},
            headers=headers,
        )
        assert res3.status_code == 422

        # Invalid timezone
        res4 = await client.patch(
            f"/api/v1/sellers/{seller_id}",
            json={"timezone": "Invalid/Timezone_123"},
            headers=headers,
        )
        assert res4.status_code == 422


@pytest.mark.asyncio
async def test_create_seller_with_notification_schedule():
    await init_db()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _get_auth_headers(client)
        payload = {
            "name": "New Scheduled Seller",
            "wb_api_token": "secret-create-token",
            "notification_mode": "scheduled",
            "notification_schedule": ["11:00", "16:00"],
            "timezone": "Asia/Krasnoyarsk",
        }
        res = await client.post("/api/v1/sellers", json=payload, headers=headers)
        assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
        data = res.json()
        assert data["notification_mode"] == "scheduled"
        assert data["notification_schedule"] == ["11:00", "16:00"]
        assert data["timezone"] == "Asia/Krasnoyarsk"
        created_id = data["id"]

    async with AsyncSessionLocal() as session:
        db_seller = await session.get(Seller, created_id)
        assert db_seller is not None
        assert db_seller.notification_mode == "scheduled"
        assert db_seller.notification_schedule == ["11:00", "16:00"]
        assert db_seller.timezone == "Asia/Krasnoyarsk"
