"""Database compatibility migration tests."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import _ensure_sqlite_compat_columns


async def test_sqlite_compat_columns_quote_reserved_when_column(tmp_path):
    """Older SQLite DBs can add the pipeline_jobs.when compatibility column."""
    db_path = tmp_path / "compat.db"
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with test_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE repositories (id INTEGER PRIMARY KEY)"))
        await conn.execute(text("CREATE TABLE pipelines (id INTEGER PRIMARY KEY)"))
        await conn.execute(text("CREATE TABLE collaborators (id INTEGER PRIMARY KEY)"))
        await conn.execute(text("CREATE TABLE pipeline_jobs (id INTEGER PRIMARY KEY)"))
        await conn.execute(text("CREATE TABLE ci_runners (id INTEGER PRIMARY KEY)"))

        await _ensure_sqlite_compat_columns(conn)

        result = await conn.execute(text("PRAGMA table_info(pipeline_jobs)"))
        columns = {row[1] for row in result.fetchall()}
        assert "when" in columns
        assert "image_config" in columns
        assert "services" in columns
        assert "dependencies" in columns
        assert "allow_failure_exit_codes" in columns
        assert "retry_config" in columns
        assert "retry_attempt" in columns
        assert "timeout_seconds" in columns
        assert "interruptible" in columns
        assert "resource_group" in columns
        assert "coverage_regex" in columns
        assert "coverage" in columns
        assert "environment" in columns
        assert "environment_url" in columns
        assert "environment_action" in columns
        assert "hooks_config" in columns
        assert "trigger_project" in columns
        assert "trigger_ref" in columns
        assert "trigger_strategy" in columns
        assert "downstream_pipeline_id" in columns
        assert "erased_at" in columns

        result = await conn.execute(text("PRAGMA table_info(pipelines)"))
        pipeline_columns = {row[1] for row in result.fetchall()}
        assert "name" in pipeline_columns
        assert "before_sha" in pipeline_columns
        assert "variables" in pipeline_columns

        result = await conn.execute(text("PRAGMA table_info(ci_runners)"))
        runner_columns = {row[1] for row in result.fetchall()}
        assert "runner_features" in runner_columns
        assert "runner_config" in runner_columns

    await test_engine.dispose()
