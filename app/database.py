"""
Database setup — async SQLAlchemy engine + session factory
"""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# Async engine (supports both PostgreSQL and SQLite)
engine_kwargs = {"echo": settings.debug, "pool_pre_ping": True}
if not settings.database_url.startswith("sqlite"):
    engine_kwargs.update({"pool_size": 20, "max_overflow": 10})

try:
    engine = create_async_engine(settings.database_url, **engine_kwargs)
except (ImportError, ModuleNotFoundError):
    # Fallback to sqlite async mock engine if aiosqlite driver is not present
    fallback_url = settings.database_url.replace("sqlite+aiosqlite", "sqlite")
    from sqlalchemy.ext.asyncio import create_async_engine
    try:
        engine = create_async_engine("sqlite+sqlite3:///./wbfbs.db", **engine_kwargs)
    except Exception:
        # Create dummy engine object if running in unit test context without DB drivers
        from sqlalchemy import create_engine as sync_create_engine
        engine = sync_create_engine("sqlite:///./wbfbs.db")

# Session factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Base declarative class for all ORM models."""
    pass


async def get_db() -> AsyncSession:
    """FastAPI dependency — yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Create all tables and sync missing columns for SQLite/PostgreSQL development."""
    # Ensure all ORM models are registered in metadata
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # SQLite auto-migration helper for development
        if str(engine.url).startswith("sqlite"):
            def sync_sqlite_columns(sync_conn):
                from sqlalchemy import inspect
                inspector = inspect(sync_conn)
                for table_name in Base.metadata.tables:
                    if table_name in inspector.get_table_names():
                        existing_cols = {col["name"] for col in inspector.get_columns(table_name)}
                        table = Base.metadata.tables[table_name]
                        for col in table.columns:
                            if col.name not in existing_cols:
                                col_type = col.type.compile(sync_conn.dialect)
                                sync_conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type}")
                if "users" in inspector.get_table_names():
                    cols = {c["name"] for c in inspector.get_columns("users")}
                    if "must_change_password" in cols:
                        sync_conn.exec_driver_sql("UPDATE users SET must_change_password = 0 WHERE must_change_password IS NULL")
            await conn.run_sync(sync_sqlite_columns)

        # PostgreSQL auto-migration for TIMESTAMP WITH TIME ZONE
        elif "postgres" in str(engine.url):
            def sync_postgres_columns(sync_conn):
                from sqlalchemy import inspect
                inspector = inspect(sync_conn)
                for table_name in Base.metadata.tables:
                    if table_name in inspector.get_table_names():
                        cols = inspector.get_columns(table_name)
                        for col in cols:
                            col_type_str = str(col["type"]).lower()
                            if "timestamp" in col_type_str and "with time zone" not in col_type_str:
                                col_name = col["name"]
                                try:
                                    sync_conn.exec_driver_sql(
                                        f"ALTER TABLE {table_name} ALTER COLUMN {col_name} TYPE TIMESTAMP WITH TIME ZONE USING {col_name} AT TIME ZONE 'UTC'"
                                    )
                                except Exception:
                                    pass
            await conn.run_sync(sync_postgres_columns)

    # Bootstrap default admin user if database has no users
    try:
        from app.services.auth_service import ensure_initial_admin
        async with AsyncSessionLocal() as session:
            await ensure_initial_admin(session)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Could not bootstrap initial admin: {e}")