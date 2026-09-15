"""
Unit and Integration Tests for CZ Background Sync & Token Keep-Alive (Option 1).
"""
import base64
import json
import random
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import init_db, AsyncSessionLocal
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.models.kiz import KizProductInfo
from app.services.encryption import encrypt
from app.services.cz_client import parse_cz_token_expiration, CZUnauthorizedError
from app.agents.cz_token_refresher import sync_active_orders_cz_status, sync_active_orders_cz_status_async
from app.config import settings


def _make_dummy_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode("ascii").rstrip("=")
    payload_json = json.dumps(payload).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_json).decode("ascii").rstrip("=")
    sig = base64.urlsafe_b64encode(b"dummysignaturebytes").decode("ascii").rstrip("=")
    return f"{header}.{payload_b64}.{sig}"


def test_parse_cz_token_expiration():
    now_utc = datetime.now(timezone.utc)
    future_ts = int((now_utc + timedelta(hours=5)).timestamp())
    token = _make_dummy_jwt({"sub": "seller-123", "exp": future_ts, "inn": "7701234567"})

    dt = parse_cz_token_expiration(token)
    assert dt is not None
    assert abs((dt - (now_utc + timedelta(hours=5))).total_seconds()) < 5

    # Invalid formats
    assert parse_cz_token_expiration(None) is None
    assert parse_cz_token_expiration("") is None
    assert parse_cz_token_expiration("not-a-jwt") is None
    assert parse_cz_token_expiration("a.b") is None
    assert parse_cz_token_expiration(_make_dummy_jwt({"sub": "no-exp"})) is None


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
async def test_get_cz_token_status_jwt_exp():
    await init_db()
    seller_id = f"test-cz-status-{uuid.uuid4().hex[:8]}"

    # 1. Fresh token (8 hours remaining -> needs_refresh = False)
    now_utc = datetime.now(timezone.utc)
    fresh_exp = int((now_utc + timedelta(hours=8)).timestamp())
    fresh_token = _make_dummy_jwt({"exp": fresh_exp, "inn": "7701234567"})

    async with AsyncSessionLocal() as db:
        seller = Seller(
            id=seller_id,
            name="CZ Status Test Shop",
            wb_api_token_encrypted=encrypt("wb-token-123"),
            cz_inn="7701234567",
            cz_token_encrypted=encrypt(fresh_token),
            is_active=True,
        )
        db.add(seller)
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _get_auth_headers(client)
        res = await client.get(f"/api/v1/sellers/{seller_id}/cz-token-status", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["has_token"] is True
        assert data["needs_refresh"] is False
        assert data["expires_in_seconds"] > 7 * 3600
        assert data["expires_at"] is not None

        # 2. Expiring soon token (2 hours remaining -> needs_refresh = True)
        soon_exp = int((now_utc + timedelta(hours=2)).timestamp())
        soon_token = _make_dummy_jwt({"exp": soon_exp, "inn": "7701234567"})
        async with AsyncSessionLocal() as db:
            s = await db.get(Seller, seller_id)
            s.cz_token_encrypted = encrypt(soon_token)
            await db.commit()

        res2 = await client.get(f"/api/v1/sellers/{seller_id}/cz-token-status", headers=headers)
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["needs_refresh"] is True
        assert data2["expires_in_seconds"] <= 2 * 3600 + 5


@pytest.mark.asyncio
async def test_sync_active_orders_cz_status_success():
    await init_db()
    seller_id = f"test-bg-sync-{uuid.uuid4().hex[:8]}"
    dummy_token = _make_dummy_jwt({"exp": int((datetime.now(timezone.utc) + timedelta(hours=6)).timestamp())})

    kiz_code_1 = f"010463001234567821test01{uuid.uuid4().hex[:6]}"
    kiz_code_2 = f"010463001234567821test02{uuid.uuid4().hex[:6]}"

    base_id = random.randint(1000000, 9000000)
    now_ts = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        seller = Seller(
            id=seller_id,
            name="Background Sync Test Shop",
            wb_api_token_encrypted=encrypt("wb-token-123"),
            cz_inn="7701234567",
            cz_token_encrypted=encrypt(dummy_token),
            is_active=True,
        )
        db.add(seller)

        # Active orders with KIZ (should be checked)
        order1 = Order(
            id=base_id + 1,
            seller_id=seller_id,
            article="TEST-1",
            status=OrderStatus.NEW,
            wb_created_at=now_ts,
            kiz_code=kiz_code_1,
            kiz_status=KizStatus.ATTACHED,
            kiz_required=True,
        )
        order2 = Order(
            id=base_id + 2,
            seller_id=seller_id,
            article="TEST-2",
            status=OrderStatus.ASSEMBLING,
            wb_created_at=now_ts,
            kiz_code=kiz_code_2,
            kiz_status=KizStatus.VALIDATED,
            kiz_required=True,
        )
        # Cancelled order (should NOT be checked)
        order_cancelled = Order(
            id=base_id + 3,
            seller_id=seller_id,
            article="TEST-3",
            status=OrderStatus.CANCELLED,
            wb_created_at=now_ts,
            kiz_code=f"010463001234567821testcancelled{uuid.uuid4().hex[:6]}",
            kiz_status=KizStatus.ATTACHED,
            kiz_required=True,
        )
        # Withdrawn order (should NOT be checked)
        order_withdrawn = Order(
            id=base_id + 4,
            seller_id=seller_id,
            article="TEST-4",
            status=OrderStatus.DELIVERED,
            wb_created_at=now_ts,
            kiz_code=f"010463001234567821testwithdrawn{uuid.uuid4().hex[:6]}",
            kiz_status=KizStatus.WITHDRAWN,
            kiz_required=True,
        )
        db.add_all([order1, order2, order_cancelled, order_withdrawn])
        await db.commit()

    mock_synced_map = {
        kiz_code_1: KizProductInfo(kiz_code=kiz_code_1, cz_status="IN_CIRCULATION"),
        kiz_code_2: KizProductInfo(kiz_code=kiz_code_2, cz_status="IN_CIRCULATION"),
    }

    with patch("app.agents.cz_token_refresher.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_sync:
        mock_sync.return_value = mock_synced_map
        result = await sync_active_orders_cz_status_async()

        assert result["status"] == "success"
        assert result["sellers_checked"] >= 1
        assert result["kiz_synced"] >= 2
        assert mock_sync.called
        call_args = mock_sync.call_args
        checked_codes = call_args.kwargs.get("kiz_codes")
        assert kiz_code_1 in checked_codes
        assert kiz_code_2 in checked_codes
        assert not any("cancelled" in c for c in checked_codes)
        assert not any("withdrawn" in c for c in checked_codes)


@pytest.mark.asyncio
async def test_sync_active_orders_cz_status_handles_401_gracefully():
    await init_db()
    seller_id = f"test-401-{uuid.uuid4().hex[:8]}"
    dummy_token = _make_dummy_jwt({"exp": int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())})
    kiz_code = f"010463001234567821test401{uuid.uuid4().hex[:6]}"
    base_id = random.randint(1000000, 9000000)

    async with AsyncSessionLocal() as db:
        seller = Seller(
            id=seller_id,
            name="401 Test Shop",
            wb_api_token_encrypted=encrypt("wb-token-123"),
            cz_inn="7701234599",
            cz_token_encrypted=encrypt(dummy_token),
            is_active=True,
        )
        db.add(seller)
        order = Order(
            id=base_id + 5,
            seller_id=seller_id,
            article="TEST-401",
            status=OrderStatus.NEW,
            wb_created_at=datetime.now(timezone.utc),
            kiz_code=kiz_code,
            kiz_status=KizStatus.ATTACHED,
            kiz_required=True,
        )
        db.add(order)
        await db.commit()

    with patch("app.agents.cz_token_refresher.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_sync:
        mock_sync.side_effect = CZUnauthorizedError("Token expired (401)")

        # Must not raise an exception, must return cleanly
        result = await sync_active_orders_cz_status_async()
        assert result["status"] == "success"
        assert result["expired_tokens"] >= 1


def test_sync_active_orders_cz_status_celery_task_wrapper():
    """Verify synchronous Celery task wrapper executes and returns result."""
    with patch("app.agents.cz_token_refresher.sync_active_orders_cz_status_async", new_callable=AsyncMock) as mock_async:
        mock_async.return_value = {"status": "success", "kiz_synced": 5}
        result = sync_active_orders_cz_status()
        assert result["status"] == "success"
        assert result["kiz_synced"] == 5

