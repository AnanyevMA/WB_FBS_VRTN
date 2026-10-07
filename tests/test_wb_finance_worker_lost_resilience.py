"""
Tests for WB Finance OOM Resilience and Celery WorkerLostError Alerting.
Verifies:
1. Default period reduced to days=14 in agent and service.
2. Status STARTED -> COMPLETED/FAILED lifecycle tracking in AuditLog.
3. Garbage collection (gc.collect) and batch cleanup in wb_finance_service.
4. celery.signals.task_failure interceptor for WorkerLostError alerting via Telegram Bot API.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from celery.exceptions import WorkerLostError
from celery.signals import task_failure
from sqlalchemy import select

from app.database import AsyncSessionLocal, init_db
from app.models.seller import Seller
from app.models.audit import AuditLog
from app.services.encryption import encrypt
from app.celery_app import handle_celery_task_failure, send_worker_lost_telegram_alert
from app.agents.wb_finance_agent import (
    sync_all_sellers_financial_reports,
    sync_seller_financial_reports_task,
)
from app.services.wb_finance_service import sync_seller_financial_reports
from app.services.wb_finance_client import WBFinanceClient


@pytest.fixture(autouse=True)
async def setup_test_db():
    await init_db()


@pytest.mark.asyncio
async def test_finance_agent_dispatches_with_14_days_and_audits():
    """Verify sync_all_sellers_financial_reports dispatches with days=14 and logs STARTED/COMPLETED."""
    seller_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name="14 Days Test Shop",
            wb_api_token_encrypted=encrypt("token_14_days"),
            is_active=True,
        )
        session.add(seller)
        await session.commit()

    with patch("app.agents.wb_finance_agent.sync_seller_financial_reports_task.delay") as mock_delay:
        res = sync_all_sellers_financial_reports()
        assert res["status"] == "COMPLETED"
        assert res["dispatched"] >= 1

        # Verify days=14 was passed
        mock_delay.assert_called_with(seller_id=seller_id, days=14)

    # Verify AuditLog has status COMPLETED and days=14
    async with AsyncSessionLocal() as session:
        audit = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.action == "SYNC_ALL_FINANCIAL_REPORTS")
                .order_by(AuditLog.created_at.desc())
            )
        ).scalars().first()

        assert audit is not None
        assert audit.payload["status"] == "COMPLETED"
        assert audit.payload["days"] == 14
        assert audit.payload["dispatched"] >= 1


@pytest.mark.asyncio
async def test_finance_service_status_lifecycle_and_gc():
    """Verify sync_seller_financial_reports records STARTED, runs gc.collect, and finishes with COMPLETED."""
    seller_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name="GC Test Shop",
            wb_api_token_encrypted=encrypt("token_gc"),
            cz_inn="190207495060",
            is_active=True,
        )
        session.add(seller)
        await session.commit()

    sample_page = [
        {
            "rrdId": 8801,
            "docTypeName": "Продажа",
            "sellerOperName": "Продажа",
            "retailAmount": "999.00",
            "kiz": "0104630199251844215test_gc_row",
            "rrDate": "2026-08-01",
        }
    ]

    async def mock_stream(*args, **kwargs):
        yield sample_page

    with patch.object(WBFinanceClient, "fetch_all_sales_reports", side_effect=mock_stream):
        with patch("gc.collect") as mock_gc:
            async with AsyncSessionLocal() as session:
                sel = await session.get(Seller, seller_id)
                res = await sync_seller_financial_reports(sel, session, days=14, verify_cz=False)

            assert res["success"] is True
            assert res["status"] == "COMPLETED"
            assert res["total_rows"] == 1
            # Verify gc.collect() was invoked after batch commit
            assert mock_gc.call_count >= 1

    # Verify audit log in DB has COMPLETED
    async with AsyncSessionLocal() as session:
        audit = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.seller_id == seller_id, AuditLog.action == "SYNC_WB_FINANCIAL_REPORTS")
                .order_by(AuditLog.created_at.desc())
            )
        ).scalar_one_or_none()

        assert audit is not None
        assert audit.payload["status"] == "COMPLETED"
        assert audit.payload["days"] == 14
        assert audit.payload["total_rows"] == 1


@pytest.mark.asyncio
async def test_finance_service_records_failed_status_on_error():
    """Verify that if an exception occurs during sync, audit log records status=FAILED."""
    seller_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name="Error Test Shop",
            wb_api_token_encrypted=encrypt("token_err"),
            is_active=True,
        )
        session.add(seller)
        await session.commit()

    async def mock_failing_stream(*args, **kwargs):
        raise RuntimeError("Simulated network timeout during stream")
        yield []

    with patch.object(WBFinanceClient, "fetch_all_sales_reports", side_effect=mock_failing_stream):
        async with AsyncSessionLocal() as session:
            sel = await session.get(Seller, seller_id)
            with pytest.raises(RuntimeError):
                await sync_seller_financial_reports(sel, session, days=14, verify_cz=False)

    # Check AuditLog has FAILED status
    async with AsyncSessionLocal() as session:
        audit = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.seller_id == seller_id, AuditLog.action == "SYNC_WB_FINANCIAL_REPORTS")
                .order_by(AuditLog.created_at.desc())
            )
        ).scalar_one_or_none()

        assert audit is not None
        assert audit.payload["status"] == "FAILED"
        assert "Simulated network timeout" in str(audit.error)


def test_worker_lost_error_telegram_alert():
    """Verify send_worker_lost_telegram_alert formats and sends emergency Telegram message."""
    seller_id = str(uuid.uuid4())
    async_db_init = False

    # Seed a seller with Telegram bot credentials
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.config import settings

    engine = create_engine(settings.database_url_sync)
    with Session(engine) as db:
        seller = Seller(
            id=seller_id,
            name="Alert Test Shop",
            wb_api_token_encrypted=encrypt("tok"),
            telegram_bot_token_encrypted=encrypt("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"),
            telegram_chat_ids=["987654321"],
            is_active=True,
        )
        db.add(seller)
        db.commit()

    fake_exc = WorkerLostError("Worker exited prematurely: signal 9 (SIGKILL) Job: 42.")

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp

    test_task_id = f"test-task-uuid-{uuid.uuid4()}"

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        sent = send_worker_lost_telegram_alert(
            task_name="app.agents.wb_finance_agent.sync_seller_financial_reports_task",
            task_id=test_task_id,
            exception=fake_exc,
            kwargs={"seller_id": seller_id, "days": 14},
        )

        assert sent is True
        assert mock_urlopen.called
        # Check HTTP request content
        req_arg = mock_urlopen.call_args[0][0]
        assert "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11" in req_arg.full_url
        req_data = req_arg.data.decode("utf-8")
        assert "WorkerLostError" in req_data
        assert "SIGKILL" in req_data
        assert test_task_id in req_data
        assert "987654321" in req_data

    # Check AuditLog written
    with Session(engine) as db:
        audit = db.execute(
            select(AuditLog)
            .where(AuditLog.action == "WORKER_LOST_ALERT", AuditLog.entity_id == test_task_id)
            .order_by(AuditLog.created_at.desc())
        ).scalars().first()
        assert audit is not None
        assert audit.error is not None
        assert "SIGKILL" in audit.error


def test_celery_task_failure_signal_trigger():
    """Verify handle_celery_task_failure triggers alert on WorkerLostError and ignores normal exceptions."""
    exc_lost = WorkerLostError("Worker exited prematurely: signal 9 (SIGKILL)")
    exc_normal = ValueError("Invalid argument")

    mock_sender = MagicMock()
    mock_sender.name = "app.agents.wb_finance_agent.sync_seller_financial_reports_task"

    with patch("app.celery_app.send_worker_lost_telegram_alert") as mock_alert:
        # 1. Normal exception should NOT trigger alert
        handle_celery_task_failure(
            sender=mock_sender,
            task_id="task-1",
            exception=exc_normal,
        )
        mock_alert.assert_not_called()

        # 2. WorkerLostError SHOULD trigger alert
        handle_celery_task_failure(
            sender=mock_sender,
            task_id="task-2",
            exception=exc_lost,
            args=("seller-123",),
            kwargs={"days": 14},
        )
        mock_alert.assert_called_once_with(
            task_name="app.agents.wb_finance_agent.sync_seller_financial_reports_task",
            task_id="task-2",
            exception=exc_lost,
            args=("seller-123",),
            kwargs={"days": 14},
        )
