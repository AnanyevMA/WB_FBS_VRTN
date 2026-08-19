"""
Cleanup Agent — WB FBS Manager

Deletes old system audit log records to manage database storage.
"""
from datetime import datetime, timedelta, timezone
import logging
from typing import Dict, Any

from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine, delete, text
from sqlalchemy.orm import sessionmaker

from app.celery_app import celery_app
from app.config import settings

logger = logging.getLogger(__name__)

# Synchronous engine and session factory
sync_engine = create_engine(
    settings.database_url_sync,
    pool_pre_ping=True,
)
SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autoflush=False,
    expire_on_commit=False,
)

# AuditLog import or fallback definition
try:
    from app.models import AuditLog
except (ImportError, ModuleNotFoundError):
    from app.database import Base

    class AuditLog(Base):  # type: ignore[no-redef]
        __tablename__ = "audit_logs"

        id = Column(Integer, primary_key=True, autoincrement=True)
        seller_id = Column(String, nullable=True, index=True)
        event_type = Column(String, nullable=False)
        details = Column(Text, nullable=True)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)


@celery_app.task(name="app.agents.cleanup.cleanup_old_audit_logs", queue="maintenance")
def cleanup_old_audit_logs(days_to_keep: int = 90) -> Dict[str, Any]:
    """Delete audit log entries older than 90 days (or specified days_to_keep)."""
    logger.info(f"Starting audit logs cleanup (deleting records older than {days_to_keep} days)")
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_to_keep)
    deleted_count = 0

    with SyncSessionLocal() as session:
        try:
            # Try ORM deletion first
            stmt = delete(AuditLog).where(AuditLog.created_at < cutoff_date)
            result = session.execute(stmt)
            deleted_count = result.rowcount
            session.commit()
            logger.info(f"Successfully deleted {deleted_count} audit log entries older than {cutoff_date.isoformat()}")
        except Exception as exc:
            session.rollback()
            logger.warning(f"ORM delete failed for audit logs, attempting direct SQL: {exc}")
            try:
                sql = text("DELETE FROM audit_logs WHERE created_at < :cutoff")
                res = session.execute(sql, {"cutoff": cutoff_date})
                deleted_count = res.rowcount
                session.commit()
                logger.info(f"Successfully deleted {deleted_count} audit log entries using SQL fallback")
            except Exception as sql_exc:
                session.rollback()
                logger.error(f"Failed to clean up audit logs: {sql_exc}")
                raise sql_exc

        # Log new cleanup audit record
        try:
            import uuid
            log_record = AuditLog(
                agent="cleanup",
                action="AUDIT_LOG_CLEANUP",
                entity_type="system",
                entity_id="audit_logs",
                payload={"deleted_count": deleted_count, "cutoff_date": cutoff_date.isoformat()},
                trace_id=str(uuid.uuid4()),
                created_at=datetime.now(timezone.utc),
            )
            session.add(log_record)
            session.commit()
        except Exception as e:
            logger.warning(f"Failed to log cleanup audit record: {e}")

        return {"status": "success", "deleted_count": deleted_count, "cutoff_date": cutoff_date.isoformat()}
