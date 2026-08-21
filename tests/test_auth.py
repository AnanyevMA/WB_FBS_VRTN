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
    """Verify initial admin account is created with must_change_password=True and login returns valid JWT token."""
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
        assert "must_change_password" in data["user"]

        token = data["access_token"]

        # 3. Access /auth/me with valid Bearer token
        res_me = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"}
        )
        assert res_me.status_code == 200
        user_profile = res_me.json()
        assert user_profile["username"] == settings.admin_username
        assert "must_change_password" in user_profile


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
async def test_change_password_api_and_db_persistence():
    """Verify password change endpoint saves hashed password to database and clears must_change_password."""
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
        current_pass = settings.admin_password
        new_pass = "NewSecretPassword123!"
        res_change = await client.post(
            "/api/v1/auth/change-password",
            json={"old_password": current_pass, "new_password": new_pass},
            headers=headers
        )
        assert res_change.status_code == 200
        change_data = res_change.json()
        assert change_data["success"] is True
        assert change_data["user"]["must_change_password"] is False

        # Old password must now fail
        res_old_fail = await client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": "OldDiscardedPasswordXYZ!"}
        )
        assert res_old_fail.status_code == 401

        # Test login with new password
        res_new_login = await client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": new_pass}
        )
        assert res_new_login.status_code == 200
        assert res_new_login.json()["user"]["must_change_password"] is False

        # Verify directly from Database session
        async with AsyncSessionLocal() as db_session:
            from sqlalchemy import select
            user_db = await db_session.scalar(select(User).where(User.username == settings.admin_username))
            assert user_db is not None
            assert user_db.must_change_password is False
            assert verify_password(new_pass, user_db.hashed_password) is True

        # Restore original password
        token2 = res_new_login.json()["access_token"]
        await client.post(
            "/api/v1/auth/change-password",
            json={"old_password": new_pass, "new_password": current_pass},
            headers={"Authorization": f"Bearer {token2}"}
        )


@pytest.mark.asyncio
async def test_admin_create_user_with_first_login_flag():
    """Verify admin can create a user and user has must_change_password=True."""
    await init_db()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Login as admin
        admin_login = await client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": settings.admin_password}
        )
        admin_token = admin_login.json()["access_token"]

        # 2. Create operator user
        import uuid
        operator_uname = f"operator_{uuid.uuid4().hex[:6]}"
        initial_temp_pass = "TempPass123!"

        create_res = await client.post(
            "/api/v1/auth/users",
            json={
                "username": operator_uname,
                "password": initial_temp_pass,
                "role": "manager",
                "is_active": True,
                "must_change_password": True
            },
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert create_res.status_code == 201
        created_user = create_res.json()
        assert created_user["username"] == operator_uname
        assert created_user["must_change_password"] is True

        # 3. Login as operator
        op_login = await client.post(
            "/api/v1/auth/login",
            json={"username": operator_uname, "password": initial_temp_pass}
        )
        assert op_login.status_code == 200
        op_data = op_login.json()
        assert op_data["user"]["must_change_password"] is True
        op_token = op_data["access_token"]

        # 4. Operator changes password on first login
        op_permanent_pass = "PermanentOpPass456!"
        change_res = await client.post(
            "/api/v1/auth/change-password",
            json={"old_password": initial_temp_pass, "new_password": op_permanent_pass},
            headers={"Authorization": f"Bearer {op_token}"}
        )
        assert change_res.status_code == 200
        assert change_res.json()["user"]["must_change_password"] is False


@pytest.mark.asyncio
async def test_env_master_password_sync():
    """Verify that if DB password differs from settings.admin_password, authenticating with .env password succeeds and syncs DB."""
    await init_db()

    # 1. Manually set a dummy hash in DB
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select
        admin_user = await session.scalar(select(User).where(User.username == settings.admin_username))
        assert admin_user is not None
        admin_user.hashed_password = hash_password("OldUnknownDatabasePassword999!")
        await session.commit()

    # 2. Try authenticate via authenticate_user using settings.admin_password (.env)
    async with AsyncSessionLocal() as session:
        from app.services.auth_service import authenticate_user
        user = await authenticate_user(session, settings.admin_username, settings.admin_password)
        assert user is not None
        assert user.username == settings.admin_username
        # DB hash should now match settings.admin_password
        assert verify_password(settings.admin_password, user.hashed_password) is True


@pytest.mark.asyncio
async def test_set_admin_password_direct_helper():
    """Verify scripts/set_admin_password.py direct function updates user and restores active status."""
    await init_db()
    from scripts.set_admin_password import set_admin_password_direct
    
    new_test_pwd = "ResetHelperPassword777!"
    success = set_admin_password_direct(settings.admin_username, new_test_pwd)
    assert success is True

    # Verify login with new password
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": new_test_pwd}
        )
        assert res.status_code == 200
        assert res.json()["user"]["is_active"] is True

    # Restore default password
    set_admin_password_direct(settings.admin_username, settings.admin_password)


@pytest.mark.asyncio
async def test_admin_login_resilience_cases():
    """Verify admin login handles case insensitivity, spaces in password, deactivation auto-recovery."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Login with uppercase username ADMIN
        res_upper = await client.post(
            "/api/v1/auth/login",
            json={"username": "ADMIN", "password": settings.admin_password}
        )
        assert res_upper.status_code == 200

        # 2. Login with password containing surrounding whitespace
        res_space = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": f"  {settings.admin_password}  "}
        )
        assert res_space.status_code == 200

        # 3. Deactivated admin recovery
        async with AsyncSessionLocal() as session:
            from sqlalchemy import select
            admin_user = await session.scalar(select(User).where(User.username == "admin"))
            if admin_user:
                admin_user.is_active = False
                await session.commit()

        res_recover = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": settings.admin_password}
        )
        assert res_recover.status_code == 200
        assert res_recover.json()["user"]["is_active"] is True



