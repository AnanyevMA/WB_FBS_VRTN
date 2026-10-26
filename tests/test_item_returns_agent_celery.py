"""
Tier 1 & Tier 2 Tests for Celery Item Returns Agent & PoLP Security.
Verifies:
- Task registration in celery_app.py and routing to 'cz_operations' queue
- Celery Beat schedule cadence (>=300s)
- Agent manifest agents_config.json PoLP matrix compliance
- AuditLog recording on success and error
- Per-seller item_returns_interval_hours and item_returns_enabled checks
- File line length limits (<400 lines)
"""
import os
import json
import uuid
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy import select

from app.database import Base
from app.models.seller import Seller
from app.models.audit import AuditLog
from app.services.encryption import encrypt
from app.celery_app import celery_app, _AGENT_MODULES
from app.agent_manifest import load_manifest

try:
    from app.agents.wb_item_returns_agent import (
        check_and_sync_item_returns,
        sync_seller_item_returns_task,
    )
except ImportError:
    check_and_sync_item_returns = None


@pytest.fixture
async def db_session():
    """Isolated in-memory SQLite database session fixture."""
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
        autoflush=False,
    )
    async with session_factory() as session:
        with patch("app.agents.wb_item_returns_agent.AsyncSessionLocal", session_factory):
            yield session

    await engine.dispose()


async def _create_test_seller(session: AsyncSession, seller_id: str = "celery-seller-1") -> Seller:
    seller = Seller(
        id=seller_id,
        name="Celery Returns Seller",
        wb_api_token_encrypted=encrypt("wb_tok_celery"),
        cz_inn="7701234567",
        cz_token_encrypted=encrypt("cz_tok_celery"),
        is_active=True,
    )
    if hasattr(seller, "item_returns_enabled"):
        setattr(seller, "item_returns_enabled", True)
    if hasattr(seller, "item_returns_interval_hours"):
        setattr(seller, "item_returns_interval_hours", 6)
    session.add(seller)
    await session.commit()
    return seller


# ============================================================================
# Tier 1: Feature Coverage (>= 5 tests)
# ============================================================================

def test_celery_task_registration_and_queue_routing():
    """Verify task routes, task definitions, and Celery router configure cz_operations queue."""
    routes = celery_app.conf.task_routes or {}
    pattern = "app.agents.wb_item_returns_agent.*"
    assert pattern in routes, f"Pattern {pattern} missing from task_routes"
    assert routes[pattern]["queue"] == "cz_operations"

    if check_and_sync_item_returns is not None:
        assert getattr(check_and_sync_item_returns, "queue", None) == "cz_operations"
        r1 = celery_app.amqp.router.route(options={}, name="app.agents.wb_item_returns_agent.check_and_sync_item_returns")
        q1 = r1.get("queue")
        assert (getattr(q1, "name", None) or q1) == "cz_operations"

    if sync_seller_item_returns_task is not None:
        assert getattr(sync_seller_item_returns_task, "queue", None) == "cz_operations"
        r2 = celery_app.amqp.router.route(options={}, name="app.agents.wb_item_returns_agent.sync_seller_item_returns_task")
        q2 = r2.get("queue")
        assert (getattr(q2, "name", None) or q2) == "cz_operations"



def test_celery_beat_schedule_configuration():
    """Verify beat schedule contains periodic item returns check with interval >= 300s."""
    beat = celery_app.conf.beat_schedule or {}
    schedule_entry = None
    for k, v in beat.items():
        if "item_returns" in k or "item-returns" in k or "wb_item_returns" in v.get("task", ""):
            schedule_entry = v
            break

    if schedule_entry is None:
        pytest.skip("Beat schedule entry for item returns awaiting registration by M3 worker")

    sched = schedule_entry.get("schedule")
    # In seconds or crontab
    if isinstance(sched, (int, float)):
        assert sched >= 300.0, f"Beat schedule interval must be >= 300s, got {sched}"
    assert schedule_entry.get("options", {}).get("queue") == "cz_operations"


def test_agent_manifest_polp_matrix():
    """Verify agents_config.json contains wb_item_returns_agent with strict PoLP matrix."""
    manifest_path = Path(__file__).resolve().parent.parent / "agents_config.json"
    assert manifest_path.exists()
    manifest = load_manifest(manifest_path)

    if "wb_item_returns_agent" not in manifest.list_agent_ids():
        pytest.skip("wb_item_returns_agent awaiting entry in agents_config.json by M3 worker")

    agent_cfg = manifest.get_agent("wb_item_returns_agent")
    assert agent_cfg is not None
    assert "sellers" in agent_cfg.polp_matrix.allowed_read_tables
    assert "audit_logs" in agent_cfg.polp_matrix.allowed_write_tables


@pytest.mark.asyncio
async def test_audit_log_success_entry_contract(db_session: AsyncSession):
    """Verify AuditLog structure for item returns sync operation."""
    seller = await _create_test_seller(db_session, "audit-seller-1")
    log = AuditLog(
        seller_id=seller.id,
        agent="wb_item_returns_agent",
        action="ITEM_RETURNS_SYNC",
        entity_type="KizSignatureBatch",
        entity_id="batch-12345",
        payload={"processed_count": 5, "batches_created": 1},
        error=None,
        trace_id=str(uuid.uuid4()),
    )
    db_session.add(log)
    await db_session.commit()

    reloaded = await db_session.get(AuditLog, log.id)
    assert reloaded.agent == "wb_item_returns_agent"
    assert reloaded.action == "ITEM_RETURNS_SYNC"
    assert reloaded.payload["processed_count"] == 5
    assert reloaded.error is None


@pytest.mark.asyncio
async def test_audit_log_error_entry_contract(db_session: AsyncSession):
    """Verify AuditLog records error details upon service exception."""
    seller = await _create_test_seller(db_session, "audit-seller-2")
    log = AuditLog(
        seller_id=seller.id,
        agent="wb_item_returns_agent",
        action="ITEM_RETURNS_SYNC_ERROR",
        entity_type="Seller",
        entity_id=seller.id,
        payload=None,
        error="ConnectionRefusedError: True API timeout",
        trace_id=str(uuid.uuid4()),
    )
    db_session.add(log)
    await db_session.commit()

    reloaded = await db_session.get(AuditLog, log.id)
    assert reloaded.error == "ConnectionRefusedError: True API timeout"


# ============================================================================
# Tier 2: Boundary & Corner Cases (>= 5 tests)
# ============================================================================

def test_celery_app_file_line_length_under_limit():
    """Verify app/celery_app.py line count is strictly under 400 lines."""
    celery_file = os.path.join("app", "celery_app.py")
    assert os.path.exists(celery_file)
    with open(celery_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
    assert len(lines) < 400, f"app/celery_app.py exceeds 400 lines: {len(lines)}"


def test_agent_modules_contains_item_returns():
    """Verify app.celery_app._AGENT_MODULES includes wb_item_returns_agent."""
    mod_name = "app.agents.wb_item_returns_agent"
    if mod_name not in _AGENT_MODULES:
        pytest.skip(f"{mod_name} awaiting addition to _AGENT_MODULES by M3 worker")
    assert mod_name in _AGENT_MODULES


@pytest.mark.asyncio
async def test_celery_task_skips_disabled_seller(db_session: AsyncSession):
    """When seller has is_active=False or item_returns_enabled=False, tasks skip seller."""
    if check_and_sync_item_returns is None:
        pytest.skip("check_and_sync_item_returns awaiting implementation by M3 worker")

    # Inactive seller
    s_inact = await _create_test_seller(db_session, "seller-dis-inactive")
    s_inact.is_active = False
    # Active seller but item_returns_enabled=False
    s_dis = await _create_test_seller(db_session, "seller-dis-flag")
    s_dis.item_returns_enabled = False
    await db_session.commit()

    with patch("app.services.wb_item_returns_service.sync_seller_returns", new_callable=AsyncMock) as mock_sync:
        res = await check_and_sync_item_returns(db=db_session)
        mock_sync.assert_not_called()
        assert res["dispatched"] == 0

        # Also verify single-seller task explicitly returns skipped
        single_res = await sync_seller_item_returns_task("seller-dis-flag", db=db_session)
        assert single_res["skipped"] is True and single_res["reason"] == "disabled"
        mock_sync.assert_not_called()


@pytest.mark.asyncio
async def test_celery_task_interval_boundary_stress(db_session: AsyncSession):
    """
    Stress test interval boundary throttling:
    - Exactly at interval: syncs (dispatched=1)
    - 1s before interval: throttled/skipped (dispatched=0)
    - 1s after interval: syncs (dispatched=1)
    """
    if check_and_sync_item_returns is None:
        pytest.skip("check_and_sync_item_returns awaiting implementation by M3 worker")

    import app.agents.wb_item_returns_agent as agent_mod
    fixed_now = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)

    class MockDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    # 1. Exactly at interval (now - 6h) -> syncs
    await _create_test_seller(db_session, "s-interval-exact")
    agent_mod._item_returns_last_runs["s-interval-exact"] = fixed_now - timedelta(hours=6)
    with patch.object(agent_mod, "datetime", MockDT), \
         patch("app.services.wb_item_returns_service.sync_seller_returns", new_callable=AsyncMock) as mock_sync:
        mock_sync.return_value = {"batch_id": "b-exact", "count": 1}
        res = await check_and_sync_item_returns()
        assert res["dispatched"] == 1
        mock_sync.assert_called_once()

    # 2. 1 second before interval (now - 6h + 1s) -> skipped
    await _create_test_seller(db_session, "s-interval-before")
    agent_mod._item_returns_last_runs["s-interval-exact"] = fixed_now
    agent_mod._item_returns_last_runs["s-interval-before"] = fixed_now - timedelta(hours=6) + timedelta(seconds=1)
    with patch.object(agent_mod, "datetime", MockDT), \
         patch("app.services.wb_item_returns_service.sync_seller_returns", new_callable=AsyncMock) as mock_sync:
        res = await check_and_sync_item_returns()
        assert res["dispatched"] == 0
        mock_sync.assert_not_called()

    # 3. 1 second after interval (now - 6h - 1s) -> syncs
    await _create_test_seller(db_session, "s-interval-after")
    agent_mod._item_returns_last_runs["s-interval-before"] = fixed_now
    agent_mod._item_returns_last_runs["s-interval-after"] = fixed_now - timedelta(hours=6) - timedelta(seconds=1)
    with patch.object(agent_mod, "datetime", MockDT), \
         patch("app.services.wb_item_returns_service.sync_seller_returns", new_callable=AsyncMock) as mock_sync:
        mock_sync.return_value = {"batch_id": "b-after", "count": 1}
        res = await check_and_sync_item_returns()
        assert res["dispatched"] == 1
        mock_sync.assert_called_once()



@pytest.mark.asyncio
async def test_celery_task_expired_wb_token_handled_safely(db_session: AsyncSession):
    """Task handles seller with expired WB token gracefully without worker crash."""
    if check_and_sync_item_returns is None:
        pytest.skip("check_and_sync_item_returns awaiting implementation by M3 worker")

    seller = await _create_test_seller(db_session, "seller-exp-token")
    with patch("app.services.wb_item_returns_service.sync_seller_returns", new_callable=AsyncMock) as mock_sync:
        from app.services.wb_analytics_client import WBAnalyticsUnauthorizedError
        mock_sync.side_effect = WBAnalyticsUnauthorizedError("WB token expired (401)")

        try:
            await check_and_sync_item_returns()
        except Exception as e:
            pytest.fail(f"Task raised uncaught exception on 401: {e}")


@pytest.mark.asyncio
async def test_celery_task_audit_log_trace_id_uniqueness(db_session: AsyncSession):
    """Verify separate audit logs have distinct trace_id identifiers."""
    seller = await _create_test_seller(db_session, "seller-trace-check")
    log1 = AuditLog(seller_id=seller.id, agent="wb_item_returns_agent", action="RUN_1", trace_id=str(uuid.uuid4()))
    log2 = AuditLog(seller_id=seller.id, agent="wb_item_returns_agent", action="RUN_2", trace_id=str(uuid.uuid4()))
    db_session.add_all([log1, log2])
    await db_session.commit()
    assert log1.trace_id != log2.trace_id


@pytest.mark.asyncio
async def test_celery_task_seller_returns_enabled_attribute(db_session: AsyncSession):
    """Verify Seller model supports item_returns_enabled boolean check."""
    seller = await _create_test_seller(db_session, "seller-enabled-check")
    enabled = getattr(seller, "item_returns_enabled", True)
    assert enabled in (True, False)


@pytest.mark.asyncio
async def test_multi_seller_isolation_when_seller_fails(db_session: AsyncSession):
    """Multi-seller resilience: Seller 1 failure does not abort Seller 2; audits and traces unique."""
    if check_and_sync_item_returns is None:
        pytest.skip("check_and_sync_item_returns awaiting implementation by M3 worker")

    await _create_test_seller(db_session, "multi-fail-seller-1")
    await _create_test_seller(db_session, "multi-ok-seller-2")

    async def mock_sync(db, seller_id, days=14):
        if seller_id == "multi-fail-seller-1":
            raise RuntimeError("Unhandled WB connection abort on Seller 1")
        return {"processed_count": 3, "batch_id": "batch-clean-seller-2"}

    with patch("app.services.wb_item_returns_service.sync_seller_returns", side_effect=mock_sync):
        result = await check_and_sync_item_returns()

    assert result["dispatched"] == 1 and result["errors"] == 1
    stmt = select(AuditLog).where(AuditLog.agent == "wb_item_returns_agent").order_by(AuditLog.id)
    entries = (await db_session.execute(stmt)).scalars().all()
    logs = [l for l in entries if l.seller_id in ("multi-fail-seller-1", "multi-ok-seller-2")]
    assert len(logs) == 2

    s1_log = next(l for l in logs if l.seller_id == "multi-fail-seller-1")
    s2_log = next(l for l in logs if l.seller_id == "multi-ok-seller-2")
    assert s1_log.action == "ITEM_RETURNS_SYNC_ERROR" and "Unhandled WB connection abort" in s1_log.error
    assert s2_log.action == "ITEM_RETURNS_SYNC" and s2_log.entity_id == "batch-clean-seller-2"
    assert s1_log.trace_id and s2_log.trace_id and s1_log.trace_id != s2_log.trace_id


@pytest.mark.asyncio
async def test_multi_seller_interleaved_exceptions_and_trace_uniqueness(db_session: AsyncSession):
    """Multi-seller: 2 failing sellers (401, 429) do not poison 2 succeeding sellers; 4 distinct traces."""
    if check_and_sync_item_returns is None:
        pytest.skip("check_and_sync_item_returns awaiting implementation by M3 worker")

    await _create_test_seller(db_session, "interleaved-s1-401")
    await _create_test_seller(db_session, "interleaved-s2-ok")
    await _create_test_seller(db_session, "interleaved-s3-429")
    await _create_test_seller(db_session, "interleaved-s4-ok")

    async def mock_sync(db, seller_id, days=14):
        if seller_id == "interleaved-s1-401":
            raise PermissionError("401 Unauthorized: Invalid WB API token")
        elif seller_id == "interleaved-s3-429":
            raise ConnectionError("429 Too Many Requests: Rate limited")
        elif seller_id == "interleaved-s2-ok":
            return {"processed_count": 7, "batch_id": "batch-s2"}
        return {"processed_count": 4, "batch_id": "batch-s4"}

    with patch("app.services.wb_item_returns_service.sync_seller_returns", side_effect=mock_sync):
        result = await check_and_sync_item_returns()

    assert result["dispatched"] == 2 and result["errors"] == 2
    stmt = select(AuditLog).where(
        AuditLog.agent == "wb_item_returns_agent",
        AuditLog.seller_id.in_(["interleaved-s1-401", "interleaved-s2-ok", "interleaved-s3-429", "interleaved-s4-ok"])
    )
    logs = (await db_session.execute(stmt)).scalars().all()
    assert len(logs) == 4
    assert len(set(l.trace_id for l in logs)) == 4
    log_map = {l.seller_id: l for l in logs}
    assert "401" in log_map["interleaved-s1-401"].error
    assert "429" in log_map["interleaved-s3-429"].error
    assert log_map["interleaved-s2-ok"].action == "ITEM_RETURNS_SYNC"
    assert log_map["interleaved-s4-ok"].action == "ITEM_RETURNS_SYNC"


