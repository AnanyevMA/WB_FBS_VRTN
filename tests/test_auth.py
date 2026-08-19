"""
Tests for Authentication, JWT, User Management, and Endpoint Security Protection
"""
import pytest
from httpx import AsyncClient, ASGITransport
from datetime import timedelta

from app.main import app
from app.config import settings
from app.models.user import User, UserRole
from app.services.auth_service import (
    hash_password,
    verify_password,
    create_access_token,
    decode_access_token,
    ensure_initial_admin
)
from app.database import AsyncSessionLocal, init_db


@pytest.mark.asyncio
async def test_password_hashing_and_verification():
    """Verify that password hashing produces valid hashes and verifies correctly."""
    plain = "SuperSecretPassword123!"
    hashed = hash_password(plain)
    assert hashed != plain
    assert verify_password(plain, hashed) is True
    assert verify_password("WrongPassword", hashed) is False
    assert verify_password("", hashed) is False


@pytest.mark.asyncio
async def test_jwt_token_generation_and_decode():
    """Verify JWT access token creation and decoding."""
    user_id = "test-user-id-12345"
    token = create_access_token(
        data={"sub": user_id, "username": "testadmin", "role": "admin", "is_superuser": True},
        expires_delta=timedelta(minutes=30)
    )
    assert isinstance(token, str)
    assert len(token) > 20

    payload = decode_access_token(token)
    assert payload is not None
    assert payload.sub == user_id
    assert payload.username == "testadmin"
    assert payload.role == "admin"
    assert payload.is_superuser is True


@pytest.mark.asyncio
async def test_admin_bootstrap_and_login_flow():
    """Verify initial admin account is created and login returns valid JWT token."""
    await init_db()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Try login with wrong password
        res_fail = await client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": "wrong_password_xyz"}
        )
        assert res_fail.status_code == 401

        # 2. Login with correct default credentials
        res_ok = await client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": settings.admin_password}
        )
        assert res_ok.status_code == 200
        data = res_ok.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["user"]["username"] == settings.admin_username

        token = data["access_token"]

        # 3. Access /auth/me with valid Bearer token
        res_me = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"}
        )
        assert res_me.status_code == 200
        user_profile = res_me.json()
        assert user_profile["username"] == settings.admin_username


@pytest.mark.asyncio
async def test_protected_routes_require_authentication():
    """Verify that business API routes return 401 without token and succeed with token."""
    await init_db()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Accessing /api/v1/sellers without token must return 401
        res_unauth = await client.get("/api/v1/sellers")
        assert res_unauth.status_code == 401

        # 2. Accessing orders without token must return 401
        res_unauth_orders = await client.get("/api/v1/sellers/fake-seller-id/orders/stats")
        assert res_unauth_orders.status_code == 401

        # 3. Login to get token
        login_res = await client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": settings.admin_password}
        )
        assert login_res.status_code == 200
        token = login_res.json()["access_token"]

        # 4. Accessing /api/v1/sellers with token must succeed (200)
        res_auth = await client.get(
            "/api/v1/sellers",
            headers={"Authorization": f"Bearer {token}"}
        )
        assert res_auth.status_code == 200
        assert isinstance(res_auth.json(), list)


@pytest.mark.asyncio
async def test_change_password_api():
    """Verify password change endpoint."""
    await init_db()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Login
        login_res = await client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": settings.admin_password}
        )
        token = login_res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Try changing password with wrong old password
        res_wrong_old = await client.post(
            "/api/v1/auth/change-password",
            json={"old_password": "completely_wrong", "new_password": "NewSecretPassword123!"},
            headers=headers
        )
        assert res_wrong_old.status_code == 400

        # Change password successfully
        new_pass = "NewSecretPassword123!"
        res_change = await client.post(
            "/api/v1/auth/change-password",
            json={"old_password": settings.admin_password, "new_password": new_pass},
            headers=headers
        )
        assert res_change.status_code == 200

        # Test login with new password
        res_new_login = await client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": new_pass}
        )
        assert res_new_login.status_code == 200

        # Restore original password
        token2 = res_new_login.json()["access_token"]
        await client.post(
            "/api/v1/auth/change-password",
            json={"old_password": new_pass, "new_password": settings.admin_password},
            headers={"Authorization": f"Bearer {token2}"}
        )
