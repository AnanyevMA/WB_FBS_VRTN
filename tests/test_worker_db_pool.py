"""
Regression tests for the Celery worker database pool.
Pooled asyncpg connections stay bound to the event loop of the previous asyncio.run(),
which failed every second task run with "Event loop is closed".
"""
import asyncio
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.database import build_engine_kwargs

POSTGRES_URL = "postgresql+asyncpg://user:pass@postgres:5432/db"


def test_null_pool_mode_drops_all_pool_options():
    """NullPool rejects pool_size and pool_timeout, so they must not be passed."""
    kwargs = build_engine_kwargs(POSTGRES_URL, use_null_pool=True)
    assert kwargs["poolclass"] is NullPool
    for option in ("pool_size", "max_overflow", "pool_recycle", "pool_timeout", "pool_pre_ping"):
        assert option not in kwargs


def test_default_mode_keeps_pool_for_api_and_bot():
    kwargs = build_engine_kwargs(POSTGRES_URL, use_null_pool=False)
    assert "poolclass" not in kwargs
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_size"] > 0


def test_default_mode_sqlite_has_no_pool_sizing():
    kwargs = build_engine_kwargs("sqlite+aiosqlite:///./wbfbs.db", use_null_pool=False)
    assert "pool_size" not in kwargs


def test_engine_accepts_null_pool_kwargs_for_postgres():
    pytest.importorskip("asyncpg")
    engine = create_async_engine(POSTGRES_URL, **build_engine_kwargs(POSTGRES_URL, use_null_pool=True))
    assert isinstance(engine.pool, NullPool)


def test_null_pool_survives_consecutive_event_loops(tmp_path):
    """Each asyncio.run() opens its own connection, as Celery tasks do."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'pool.db'}"
    engine = create_async_engine(url, **build_engine_kwargs(url, use_null_pool=True))

    async def query():
        async with engine.connect() as conn:
            return (await conn.execute(text("SELECT 1"))).scalar()

    assert [asyncio.run(query()) for _ in range(4)] == [1, 1, 1, 1]


def test_setting_is_off_by_default(monkeypatch):
    monkeypatch.delenv("DB_USE_NULL_POOL", raising=False)
    assert Settings(_env_file=None).db_use_null_pool is False


def test_setting_reads_environment(monkeypatch):
    monkeypatch.setenv("DB_USE_NULL_POOL", "true")
    assert Settings(_env_file=None).db_use_null_pool is True


def test_prod_compose_enables_null_pool_for_worker_only():
    compose = yaml.safe_load(Path("docker-compose.prod.yml").read_text(encoding="utf-8"))
    enabled = {
        name for name, service in compose["services"].items()
        if "DB_USE_NULL_POOL=true" in (service.get("environment") or [])
    }
    assert enabled == {"worker"}
