"""
Knowledge Base & Codebase Synchronization Agent — WB FBS Manager

Monitors the freshness, integrity, and validity of documentation in docs/
and automatically maintains the AST-based codebase index (codebase_index.json & CODEBASE_MAP.md).
Ensures all AI agents can query symbols, models, and endpoints with minimal token consumption.
"""
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.celery_app import celery_app
from app.config import settings
from app.services.codebase_indexer import CodebaseIndexer
from app.services.kb_service import KBService

logger = logging.getLogger(__name__)

# Synchronous SQLAlchemy engine and session factory for Celery tasks
sync_engine = create_engine(
    settings.database_url_sync,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)
SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autoflush=False,
    expire_on_commit=False,
)

try:
    from app.models import AuditLog
except (ImportError, ModuleNotFoundError):
    from app.database import Base
    from sqlalchemy import Column, DateTime, Integer, JSON, String, Text

    class AuditLog(Base):  # type: ignore[no-redef]
        __tablename__ = "audit_logs"
        id = Column(Integer, primary_key=True, autoincrement=True)
        seller_id = Column(String, nullable=True)
        event_type = Column(String, nullable=False)
        entity_type = Column(String, nullable=True)
        entity_id = Column(String, nullable=True)
        details = Column(JSON, nullable=True)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


@celery_app.task(
    name="app.agents.kb_sync_agent.sync_knowledge_base",
    queue="maintenance",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def sync_knowledge_base(self, force_rebuild: bool = False) -> Dict[str, Any]:
    """
    Periodic task to validate and maintain the project Knowledge Base and Codebase Index.
    1. Validates integrity of markdown files and relative links in docs/.
    2. Rebuilds/syncs docs/INDEX.json and docs/INDEX.md.
    3. Re-indexes AST symbols and regenerates codebase_index.json and CODEBASE_MAP.md.
    4. Writes an audit log record.
    """
    logger.info("Starting Knowledge Base & Codebase synchronization task...")
    kb_service = KBService()
    code_indexer = CodebaseIndexer()

    try:
        # 1. Check docs integrity
        integrity_report = kb_service.validate_integrity()
        
        # 2. Rebuild docs index if forced or issues detected
        if force_rebuild or integrity_report.get("status") != "HEALTHY":
            kb_service.rebuild_index()
            rebuilt = True
        else:
            rebuilt = False

        # 3. Update Codebase Symbol Index
        code_indexer.save_index()
        indexed_files_count = len(code_indexer.load_index().get("files", []))

        status = integrity_report.get("status", "HEALTHY")
        checked_count = integrity_report.get("checked_documents_count", 0)
        issues = integrity_report.get("issues", [])

        # 4. Log to audit_logs
        session = SyncSessionLocal()
        try:
            import uuid
            audit_entry = AuditLog(
                seller_id=None,
                agent="kb_sync_agent",
                action="KB_AND_CODE_SYNC_COMPLETED",
                entity_type="knowledge_base",
                entity_id="docs_and_codebase_index",
                payload={
                    "status": status,
                    "docs_checked_count": checked_count,
                    "codebase_files_count": indexed_files_count,
                    "rebuilt": rebuilt,
                    "issues_count": len(issues),
                    "issues": issues,
                    "synced_at": datetime.now(timezone.utc).isoformat(),
                },
                trace_id=str(uuid.uuid4()),
                created_at=datetime.now(timezone.utc),
            )
            session.add(audit_entry)
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to record audit log for KB/Code sync: {e}")
        finally:
            session.close()

        logger.info(f"KB & Code sync finished. Status: {status}, Docs: {checked_count}, Code files: {indexed_files_count}")
        return {
            "status": status,
            "checked_count": checked_count,
            "codebase_files_count": indexed_files_count,
            "rebuilt": rebuilt,
            "issues": issues,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as exc:
        logger.error(f"Error during Knowledge Base / Codebase sync: {exc}", exc_info=True)
        raise self.retry(exc=exc)


def lookup_kb_solution(
    query: Optional[str] = None,
    category: Optional[str] = None,
    tags: Optional[List[str]] = None,
    endpoint: Optional[str] = None,
    error_code: Optional[str] = None,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """
    Helper function for other agents to fast-query the knowledge base index.
    """
    service = KBService()
    return service.search(
        query=query,
        category=category,
        tags=tags,
        endpoint=endpoint,
        error_code=error_code,
        limit=limit,
    )


def lookup_code_symbol(
    symbol: Optional[str] = None,
    layer: Optional[str] = None,
    file_keyword: Optional[str] = None,
    endpoint_keyword: Optional[str] = None,
    table_name: Optional[str] = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """
    Helper function for other agents to fast-query code symbols, endpoints, and models without loading full files.
    """
    indexer = CodebaseIndexer()
    return indexer.query(
        symbol=symbol,
        layer=layer,
        file_keyword=file_keyword,
        endpoint_keyword=endpoint_keyword,
        table_name=table_name,
        limit=limit,
    )
