"""
Regression tests for the item returns sync.
WB answers 204 No Content when there are no returns: this is a success, not a parse error.
A real failure is retried after a cooldown instead of on every Beat tick.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.agents import wb_item_returns_agent
from app.agents.wb_item_returns_agent import ERROR_RETRY_COOLDOWN, check_and_sync_item_returns
from app.database import Base
from app.models.audit import AuditLog
from app.models.seller import Seller
from app.services import wb_item_returns_service
from app.services.encryption import encrypt
from app.services.wb_analytics_client import WBAnalyticsAPIError, WBAnalyticsClient

BASE = "https://seller-analytics-api.wildberries.ru"


@pytest.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    wb_item_returns_agent._item_returns_last_runs.clear()
    async with factory() as session:
        with patch("app.agents.wb_item_returns_agent.AsyncSessionLocal", factory):
            yield session
    await engine.dispose()


async def _create_seller(session: AsyncSession, seller_id: str) -> Seller:
    seller = Seller(
        id=seller_id, name="Returns Seller", wb_api_token_encrypted=encrypt("wb_tok"),
        cz_inn="7701234567", cz_token_encrypted=encrypt("cz_tok"), is_active=True,
    )
    session.add(seller)
    await session.commit()
    return seller


def _add_error(session: AsyncSession, seller_id: str, age: timedelta) -> None:
    session.add(AuditLog(
        seller_id=seller_id, agent="wb_item_returns_agent", action="ITEM_RETURNS_SYNC_ERROR",
        error="boom", trace_id="trace-" + seller_id, created_at=datetime.now(timezone.utc) - age,
    ))


@pytest.mark.asyncio
async def test_item_returns_204_means_no_returns():
    req = Request("GET", f"{BASE}/api/analytics/v1/item-returns")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=Response(204, request=req)):
        assert await WBAnalyticsClient("token").get_item_returns("2026-10-16", "2026-10-30") == []


@pytest.mark.asyncio
async def test_excise_report_204_means_no_data():
    req = Request("POST", f"{BASE}/api/v1/analytics/excise-report")
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=Response(204, request=req)):
        assert await WBAnalyticsClient("token").get_excise_report("2026-10-16", "2026-10-30") == []


@pytest.mark.asyncio
async def test_empty_200_is_still_an_error_and_names_the_status():
    """Only 204 promises an empty body. An empty 200 is a broken response."""
    req = Request("GET", f"{BASE}/api/analytics/v1/item-returns")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=Response(200, content=b"", request=req)):
        with pytest.raises(WBAnalyticsAPIError, match="HTTP 200"):
            await WBAnalyticsClient("token").get_item_returns("2026-10-16", "2026-10-30")


@pytest.mark.asyncio
async def test_204_from_wb_updates_last_sync_and_writes_no_error(db_session: AsyncSession):
    """Full agent run: nothing to process, so the next run waits for the regular interval."""
    seller = await _create_seller(db_session, "seller-no-returns")
    req = Request("GET", f"{BASE}/api/analytics/v1/item-returns")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=Response(204, request=req)):
        result = await check_and_sync_item_returns()

    assert result["errors"] == 0 and result["dispatched"] == 1
    await db_session.refresh(seller)
    assert seller.last_item_returns_at is not None
    actions = (await db_session.execute(
        select(AuditLog.action).where(AuditLog.seller_id == seller.id)
    )).scalars().all()
    assert actions == ["ITEM_RETURNS_SYNC"]


@pytest.mark.asyncio
async def test_recent_failure_is_not_retried_during_cooldown(db_session: AsyncSession):
    await _create_seller(db_session, "seller-cooling")
    _add_error(db_session, "seller-cooling", ERROR_RETRY_COOLDOWN - timedelta(minutes=5))
    await db_session.commit()

    with patch("app.services.wb_item_returns_service.sync_seller_returns", new_callable=AsyncMock) as sync:
        result = await check_and_sync_item_returns()

    sync.assert_not_awaited()
    assert result["dispatched"] == 0 and result["errors"] == 0


@pytest.mark.asyncio
async def test_failure_is_retried_after_cooldown(db_session: AsyncSession):
    await _create_seller(db_session, "seller-cooled")
    _add_error(db_session, "seller-cooled", ERROR_RETRY_COOLDOWN + timedelta(minutes=5))
    await db_session.commit()

    with patch(
        "app.services.wb_item_returns_service.sync_seller_returns",
        new_callable=AsyncMock, return_value={"processed_count": 0},
    ) as sync:
        result = await check_and_sync_item_returns()

    sync.assert_awaited_once()
    assert result["dispatched"] == 1


@pytest.mark.asyncio
async def test_cooldown_is_per_seller(db_session: AsyncSession):
    await _create_seller(db_session, "seller-a-failed")
    await _create_seller(db_session, "seller-b-healthy")
    _add_error(db_session, "seller-a-failed", timedelta(minutes=1))
    await db_session.commit()

    with patch(
        "app.services.wb_item_returns_service.sync_seller_returns",
        new_callable=AsyncMock, return_value={"processed_count": 0},
    ) as sync:
        await check_and_sync_item_returns()

    assert [call.kwargs["seller_id"] for call in sync.await_args_list] == ["seller-b-healthy"]


@pytest.mark.asyncio
async def test_failed_sync_leaves_no_unretrieved_future(db_session: AsyncSession):
    """The shared future must not log "Future exception was never retrieved"."""
    await _create_seller(db_session, "seller-future")
    leaked = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, ctx: leaked.append(ctx))
    original_create_future = loop.create_future
    futures = []

    def tracking_create_future():
        fut = original_create_future()
        futures.append(fut)
        return fut

    try:
        with patch.object(loop, "create_future", tracking_create_future), patch(
            "app.services.wb_item_returns_service.process_item_returns_for_seller",
            new_callable=AsyncMock, side_effect=RuntimeError("WB down"),
        ):
            with pytest.raises(RuntimeError):
                await wb_item_returns_service.sync_seller_returns(db=db_session, seller_id="seller-future")
    finally:
        loop.set_exception_handler(previous)

    assert futures and all(f.done() and not f._log_traceback for f in futures)
    assert leaked == []
