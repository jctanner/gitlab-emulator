from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """Create all tables and set WAL mode."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_sqlite_compat_columns(conn)
        await conn.execute(text("PRAGMA journal_mode=WAL"))


async def _ensure_sqlite_compat_columns(conn) -> None:
    """Add JSON columns introduced after early VM databases were created."""
    if conn.engine.url.get_backend_name() != "sqlite":
        return

    async def ensure_column(table: str, column: str, ddl: str) -> None:
        result = await conn.execute(text(f"PRAGMA table_info({table})"))
        rows = result.fetchall()
        if not rows:
            return
        existing = {row[1] for row in rows}
        if column not in existing:
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))

    await ensure_column(
        "repositories",
        "ci_security_settings",
        "ci_security_settings JSON DEFAULT '{}'",
    )
    await ensure_column(
        "pipelines",
        "security_warnings",
        "security_warnings JSON DEFAULT '[]'",
    )
    await ensure_column(
        "pipelines",
        "name",
        "name VARCHAR",
    )
    await ensure_column(
        "pipelines",
        "before_sha",
        "before_sha VARCHAR",
    )
    await ensure_column(
        "pipeline_jobs",
        "secret_metadata",
        "secret_metadata JSON DEFAULT '[]'",
    )
    await ensure_column(
        "pipeline_jobs",
        "image_config",
        "image_config JSON DEFAULT '{}'",
    )
    await ensure_column(
        "pipeline_jobs",
        "services",
        "services JSON DEFAULT '[]'",
    )
    await ensure_column(
        "pipeline_jobs",
        "dependencies",
        "dependencies JSON DEFAULT NULL",
    )
    await ensure_column(
        "pipeline_jobs",
        "allow_failure",
        "allow_failure BOOLEAN DEFAULT 0",
    )
    await ensure_column(
        "pipeline_jobs",
        "allow_failure_exit_codes",
        "allow_failure_exit_codes JSON DEFAULT '[]'",
    )
    await ensure_column(
        "pipeline_jobs",
        "retry_config",
        "retry_config JSON DEFAULT '{}'",
    )
    await ensure_column(
        "pipeline_jobs",
        "retry_attempt",
        "retry_attempt INTEGER DEFAULT 0",
    )
    await ensure_column(
        "pipeline_jobs",
        "timeout_seconds",
        "timeout_seconds INTEGER",
    )
    await ensure_column(
        "pipeline_jobs",
        "interruptible",
        "interruptible BOOLEAN DEFAULT 0",
    )
    await ensure_column(
        "pipeline_jobs",
        "resource_group",
        "resource_group VARCHAR",
    )
    await ensure_column(
        "pipeline_jobs",
        "coverage_regex",
        "coverage_regex VARCHAR",
    )
    await ensure_column(
        "pipeline_jobs",
        "coverage",
        "coverage VARCHAR",
    )
    await ensure_column(
        "pipeline_jobs",
        "environment",
        "environment VARCHAR",
    )
    await ensure_column(
        "pipeline_jobs",
        "when",
        '"when" VARCHAR DEFAULT \'on_success\'',
    )
    await ensure_column(
        "pipeline_jobs",
        "scheduled_at",
        "scheduled_at DATETIME",
    )
    await ensure_column(
        "pipeline_schedules",
        "next_run_at",
        "next_run_at DATETIME",
    )
    await ensure_column(
        "collaborators",
        "access_level",
        "access_level INTEGER",
    )
