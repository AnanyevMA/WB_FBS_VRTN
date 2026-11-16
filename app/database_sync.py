"""
Synchronous SQLAlchemy engine shared by the Celery agents and the Telegram bot.

Every agent module used to create its own engine with its own connection pool, so one worker
kept an idle PostgreSQL connection per module, and each connection is a separate server process.
One engine with a small pool keeps at most `db_sync_pool_size` idle connections per process.
"""
from sqlalchemy import create_engine

from app.config import settings


def build_sync_engine_kwargs(database_url: str) -> dict:
    kwargs = {"pool_pre_ping": True}
    if not database_url.startswith("sqlite"):
        kwargs.update({
            "pool_size": settings.db_sync_pool_size,
            "max_overflow": settings.db_sync_max_overflow,
            "pool_recycle": settings.db_pool_recycle,
            "pool_timeout": settings.db_pool_timeout,
        })
    return kwargs


sync_engine = create_engine(settings.database_url_sync, **build_sync_engine_kwargs(settings.database_url_sync))
