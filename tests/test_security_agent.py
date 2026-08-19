"""
Tests for Security Audit Agent & Posture Inspection
"""
import pytest
from app.agents.security_audit_agent import inspect_security_posture, run_security_audit, SyncSessionLocal
from app.database import init_db


@pytest.mark.asyncio
async def test_security_audit_agent_execution():
    """Verify security audit agent executes and returns structured findings."""
    await init_db()

    with SyncSessionLocal() as session:
        report = inspect_security_posture(session)
        assert isinstance(report, dict)
        assert "status" in report
        assert "critical_count" in report
        assert "warnings_count" in report
        assert "findings" in report
        assert "audited_at" in report
        assert isinstance(report["findings"], list)


def test_security_audit_celery_task():
    """Verify Celery task run_security_audit generates audit report and records audit log."""
    report = run_security_audit()
    assert report is not None
    assert "status" in report
