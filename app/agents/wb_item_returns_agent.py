"""
WB Item Returns Celery Agent — WB FBS Manager
Periodic background reconciliation of Wildberries client item returns
and True API status checks to generate LP_RETURN batches and update order states.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import inspect
import logging
from typing import Any, Dict, Optional
import uuid

from celery import Task
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.models.audit import AuditLog
from app.models.seller import Seller
from app.services import wb_item_returns_service

logger = logging.getLogger(__name__)

# In-memory run tracker: seller_id -> datetime (UTC) of last sync
_item_returns_last_runs: Dict[str, datetime] = {}

# A failed sync is retried sooner than the regular interval, but not on every Beat tick
ERROR_RETRY_COOLDOWN = timedelta(minutes=30)


class AsyncTask(Task):
    """Custom Celery Task supporting both awaitable pytest runs and synchronous Celery execution."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        res = self.run(*args, **kwargs)
        if inspect.iscoroutine(res):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            return res if (loop and loop.is_running()) else asyncio.run(res)
        return res


@asynccontextmanager
async def _get_session(db: Optional[AsyncSession] = None):
    """Resolve database session from explicit argument or default sessionmaker."""
    if db is not None:
        yield db
    else:
        async with AsyncSessionLocal() as session:
            yield session


async def _record_audit(
    session: AsyncSession, seller_id: str, action: str,
    entity_type: Optional[str] = None, entity_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None, error: Optional[str] = None,
) -> None:
    audit = AuditLog(
        seller_id=seller_id, agent="wb_item_returns_agent", action=action,
        entity_type=entity_type, entity_id=entity_id, payload=payload, error=error,
        trace_id=str(uuid.uuid4()),
    )
    session.add(audit)
    await session.commit()


async def _sync_seller(session: AsyncSession, seller: Seller, days: int = 14) -> Dict[str, Any]:
    seller_id = str(seller.id)
    res = await wb_item_returns_service.sync_seller_returns(db=session, seller_id=seller_id, days=days)
    now_utc = datetime.now(timezone.utc)
    _item_returns_last_runs[seller_id] = now_utc
    if hasattr(seller, "last_item_returns_at"):
        setattr(seller, "last_item_returns_at", now_utc)
    batch_id = res.get("batch_id")
    await _record_audit(
        session, seller_id, "ITEM_RETURNS_SYNC",
        "KizSignatureBatch" if batch_id else "Seller", str(batch_id or seller_id), res, None,
    )
    return res


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


async def _is_sync_due(session: AsyncSession, seller: Seller, seller_id: str, now_utc: datetime) -> bool:
    """Check the per-seller interval and the cooldown after the latest failed attempt."""
    interval_h = int(getattr(seller, "item_returns_interval_hours", 6) or 6)
    last_run = getattr(seller, "last_item_returns_at", None) or _item_returns_last_runs.get(seller_id)
    if last_run and (now_utc - _as_utc(last_run)) < timedelta(hours=interval_h):
        return False

    last_error = await session.scalar(
        select(func.max(AuditLog.created_at)).where(
            AuditLog.seller_id == seller_id, AuditLog.action == "ITEM_RETURNS_SYNC_ERROR",
        )
    )
    return not (last_error and (now_utc - _as_utc(last_error)) < ERROR_RETRY_COOLDOWN)


@celery_app.task(name="app.agents.wb_item_returns_agent.check_and_sync_item_returns", queue="cz_operations", base=AsyncTask)
async def check_and_sync_item_returns(db: Optional[AsyncSession] = None) -> Dict[str, Any]:
    """Periodic Celery Beat task (every 300s). Reconciles returns for active sellers."""
    now_utc = datetime.now(timezone.utc)
    processed: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    async with _get_session(db) as session:
        stmt = select(Seller.id).where(Seller.is_active == True, Seller.wb_api_token_encrypted.isnot(None))
        seller_ids = [str(sid) for sid in (await session.execute(stmt)).scalars().all()]

        for seller_id in seller_ids:
            seller = await session.get(Seller, seller_id)
            if not seller or not getattr(seller, "item_returns_enabled", True):
                continue

            if not await _is_sync_due(session, seller, seller_id, now_utc):
                continue

            try:
                res = await _sync_seller(session, seller, days=14)
                processed[seller_id] = res
            except Exception as exc:
                logger.error(f"[Item Returns Agent] Sync failed for seller {seller_id}: {exc}")
                try:
                    await session.rollback()
                    await _record_audit(
                        session, seller_id, "ITEM_RETURNS_SYNC_ERROR",
                        "Seller", seller_id, None, str(exc),
                    )
                except Exception as audit_err:
                    logger.warning(f"[Item Returns Agent] Audit failed for {seller_id}: {audit_err}")
                    try:
                        await session.rollback()
                    except Exception:
                        pass
                errors[seller_id] = str(exc)

    return {"checked_at": now_utc.isoformat(), "dispatched": len(processed), "errors": len(errors)}


@celery_app.task(name="app.agents.wb_item_returns_agent.sync_seller_item_returns_task", queue="cz_operations", base=AsyncTask, bind=True, max_retries=2, default_retry_delay=120)
async def sync_seller_item_returns_task(self, seller_id: str, days: int = 14, db: Optional[AsyncSession] = None) -> Dict[str, Any]:
    """Single-seller on-demand or dispatched returns reconciliation task."""
    async with _get_session(db) as session:
        seller = await session.get(Seller, seller_id)
        if not seller or not seller.is_active or not seller.wb_api_token_encrypted:
            return {"success": False, "skipped": True, "reason": "not_eligible"}
        if not getattr(seller, "item_returns_enabled", True):
            return {"success": False, "skipped": True, "reason": "disabled"}

        try:
            return await _sync_seller(session, seller, days=days)
        except Exception as exc:
            logger.error(f"[Item Returns Task] Failed for seller {seller_id}: {exc}")
            try:
                await session.rollback()
                await _record_audit(session, seller_id, "ITEM_RETURNS_SYNC_ERROR", "Seller", seller_id, None, str(exc))
            except Exception as audit_err:
                logger.warning(f"[Item Returns Task] Audit failed for {seller_id}: {audit_err}")
            return {"success": False, "error": str(exc)}
