"""
Adversarial Stress Test Suite for Item Returns Celery Agent (Milestone 3 Iteration 2).
Author: challenger_m3_iter2_1
Empirical Stress Tests:
1. Sub-second and second-level interval boundary throttling (exact, -1s, +1s, -100ms, +100ms).
2. Disabled seller (item_returns_enabled=False) skipping in periodic agent and single-seller task.
3. Inactive seller (is_active=False) skipping.
4. Beat schedule cadence (>= 300s) and AMQP router configuration to cz_operations.
5. Timezone awareness (UTC aware, Moscow offset, and naive timestamps).
"""
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.seller import Seller
from app.services.encryption import encrypt
from app.celery_app import celery_app
import app.agents.wb_item_returns_agent as agent_mod
from app.agents.wb_item_returns_agent import (
    check_and_sync_item_returns,
    sync_seller_item_returns_task,
    _item_returns_last_runs,
)


@pytest.fixture
async def adv_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        with patch("app.agents.wb_item_returns_agent.AsyncSessionLocal", session_factory):
            yield session
    await engine.dispose()


async def _add_seller(
    db: AsyncSession,
    sid: str,
    active: bool = True,
    token: str = "tok",
) -> Seller:
    s = Seller(
        id=sid,
        name=f"Seller {sid}",
        wb_api_token_encrypted=encrypt(token) if token else encrypt("empty"),
        is_active=active,
    )
    db.add(s)
    await db.commit()
    return s


@pytest.mark.asyncio
async def test_adv_interval_boundary_fine_granularity(adv_db: AsyncSession):
    """Stress test boundary at exact, -1s, +1s, -100ms, +100ms."""
    fixed_now = datetime(2026, 10, 26, 15, 0, 0, tzinfo=timezone.utc)

    class MockDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    # Exactly 6h ago -> Should sync
    await _add_seller(adv_db, "adv_exact")
    agent_mod._item_returns_last_runs["adv_exact"] = fixed_now - timedelta(hours=6)

    # 1 second before 6h ago (5h 59m 59s elapsed) -> Should throttle
    await _add_seller(adv_db, "adv_minus_1s")
    agent_mod._item_returns_last_runs["adv_minus_1s"] = fixed_now - timedelta(hours=6) + timedelta(seconds=1)

    # 1 second after 6h ago (6h 00m 01s elapsed) -> Should sync
    await _add_seller(adv_db, "adv_plus_1s")
    agent_mod._item_returns_last_runs["adv_plus_1s"] = fixed_now - timedelta(hours=6) - timedelta(seconds=1)

    # 100 milliseconds before 6h ago -> Should throttle
    await _add_seller(adv_db, "adv_minus_100ms")
    agent_mod._item_returns_last_runs["adv_minus_100ms"] = fixed_now - timedelta(hours=6) + timedelta(milliseconds=100)

    # 100 milliseconds after 6h ago -> Should sync
    await _add_seller(adv_db, "adv_plus_100ms")
    agent_mod._item_returns_last_runs["adv_plus_100ms"] = fixed_now - timedelta(hours=6) - timedelta(milliseconds=100)

    synced = []
    async def mock_sync(db, seller_id, days=14):
        synced.append(seller_id)
        return {"batch_id": f"b-{seller_id}", "count": 1}

    with patch.object(agent_mod, "datetime", MockDT), \
         patch("app.services.wb_item_returns_service.sync_seller_returns", side_effect=mock_sync):
        res = await check_and_sync_item_returns(db=adv_db)

    assert set(synced) == {"adv_exact", "adv_plus_1s", "adv_plus_100ms"}
    assert "adv_minus_1s" not in synced
    assert "adv_minus_100ms" not in synced
    assert res["dispatched"] == 3


@pytest.mark.asyncio
async def test_adv_disabled_seller_skipping(adv_db: AsyncSession):
    """Stress test disabled seller (item_returns_enabled=False) and inactive seller."""
    s_dis = await _add_seller(adv_db, "s_adv_disabled", active=True)
    s_dis.item_returns_enabled = False
    await adv_db.commit()

    s_inact = await _add_seller(adv_db, "s_adv_inactive", active=False)
    s_inact.item_returns_enabled = True
    await adv_db.commit()

    with patch("app.services.wb_item_returns_service.sync_seller_returns", new_callable=AsyncMock) as mock_sync:
        res = await check_and_sync_item_returns(db=adv_db)
        mock_sync.assert_not_called()
        assert res["dispatched"] == 0

        # Verify individual task returns skipped
        r_dis = await sync_seller_item_returns_task("s_adv_disabled", db=adv_db)
        assert r_dis == {"success": False, "skipped": True, "reason": "disabled"}

        r_inact = await sync_seller_item_returns_task("s_adv_inactive", db=adv_db)
        assert r_inact == {"success": False, "skipped": True, "reason": "not_eligible"}


@pytest.mark.asyncio
async def test_adv_timezone_naive_and_offset_awareness(adv_db: AsyncSession):
    """Ensure naive timestamps and non-UTC offsets compute difference accurately without exception."""
    fixed_now = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)

    class MockDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    # Naive timestamp in memory representing 6 hours ago
    await _add_seller(adv_db, "adv_naive_ok")
    naive_6h_ago = (fixed_now - timedelta(hours=6)).replace(tzinfo=None)
    agent_mod._item_returns_last_runs["adv_naive_ok"] = naive_6h_ago

    # Offset aware (+03:00 Moscow time) timestamp in memory representing 6 hours ago
    await _add_seller(adv_db, "adv_msk_ok")
    moscow_tz = timezone(timedelta(hours=3))
    aware_msk_6h_ago = (fixed_now - timedelta(hours=6)).astimezone(moscow_tz)
    agent_mod._item_returns_last_runs["adv_msk_ok"] = aware_msk_6h_ago

    synced = []
    async def mock_sync(db, seller_id, days=14):
        synced.append(seller_id)
        return {"batch_id": "b-tz"}

    with patch.object(agent_mod, "datetime", MockDT), \
         patch("app.services.wb_item_returns_service.sync_seller_returns", side_effect=mock_sync):
        res = await check_and_sync_item_returns(db=adv_db)

    assert "adv_naive_ok" in synced
    assert "adv_msk_ok" in synced
    assert res["dispatched"] == 2


def test_adv_celery_task_routing_and_beat_spec():
    """Verify beat cadence, options queue, and celery router resolution."""
    beat = celery_app.conf.beat_schedule
    assert "check-item-returns-schedule" in beat
    entry = beat["check-item-returns-schedule"]
    assert entry["task"] == "app.agents.wb_item_returns_agent.check_and_sync_item_returns"
    assert entry["schedule"] == 300.0
    assert entry["options"]["queue"] == "cz_operations"

    # AMQP router wildcard and exact route resolution
    r_periodic = celery_app.amqp.router.route(
        options={},
        name="app.agents.wb_item_returns_agent.check_and_sync_item_returns",
    )
    assert getattr(r_periodic.get("queue"), "name", r_periodic.get("queue")) == "cz_operations"

    r_single = celery_app.amqp.router.route(
        options={},
        name="app.agents.wb_item_returns_agent.sync_seller_item_returns_task",
    )
    assert getattr(r_single.get("queue"), "name", r_single.get("queue")) == "cz_operations"
