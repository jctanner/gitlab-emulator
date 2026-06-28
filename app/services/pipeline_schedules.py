"""Pipeline schedule due-run calculation and worker execution."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models.ci import Pipeline, PipelineSchedule
from app.services.delayed_jobs import promote_due_delayed_jobs

logger = logging.getLogger("gitlab_emulator.pipeline_schedules")


@dataclass(frozen=True)
class ScheduleRunResult:
    checked: int = 0
    created: int = 0
    failed: int = 0


def utc_now_naive() -> datetime:
    """Return the UTC timestamp format used by SQLite-backed schedule rows."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def compute_next_run_at(
    cron: str,
    cron_timezone: str = "UTC",
    *,
    after: datetime | None = None,
) -> datetime:
    """Compute the next UTC run time for a five-field cron expression.

    The emulator intentionally supports the common GitLab pipeline schedule
    shape: minute, hour, day-of-month, month, day-of-week with lists, ranges,
    wildcards, and step values. Returned datetimes are naive UTC values because
    the existing SQLite model stores naive timestamps.
    """
    try:
        tz = ZoneInfo(cron_timezone or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown cron timezone: {cron_timezone}") from exc

    fields = cron.split()
    if len(fields) != 5:
        raise ValueError("Cron must contain five fields")

    minutes = _parse_cron_field(fields[0], 0, 59, "minute")
    hours = _parse_cron_field(fields[1], 0, 23, "hour")
    days = _parse_cron_field(fields[2], 1, 31, "day of month")
    months = _parse_cron_field(fields[3], 1, 12, "month")
    weekdays = _parse_cron_field(fields[4], 0, 7, "day of week")
    weekdays = {0 if value == 7 else value for value in weekdays}

    after_utc = _as_utc_aware(after or utc_now_naive())
    candidate = after_utc.astimezone(tz).replace(second=0, microsecond=0)
    candidate += timedelta(minutes=1)
    limit = candidate + timedelta(days=366)

    while candidate <= limit:
        cron_weekday = (candidate.weekday() + 1) % 7
        if (
            candidate.minute in minutes
            and candidate.hour in hours
            and candidate.day in days
            and candidate.month in months
            and cron_weekday in weekdays
        ):
            return candidate.astimezone(timezone.utc).replace(tzinfo=None)
        candidate += timedelta(minutes=1)

    raise ValueError("Cron expression did not match within one year")


def set_schedule_next_run(
    schedule: PipelineSchedule,
    *,
    after: datetime | None = None,
) -> None:
    """Update a schedule's next run based on active state and cron metadata."""
    schedule.next_run_at = (
        compute_next_run_at(schedule.cron, schedule.cron_timezone, after=after)
        if schedule.active
        else None
    )


async def play_pipeline_schedule(
    schedule: PipelineSchedule,
    project_id: int,
    db: AsyncSession,
    *,
    actor=None,
    advance_next_run: bool = True,
    now: datetime | None = None,
) -> Pipeline:
    """Create a source=schedule pipeline and update schedule metadata."""
    from app.api.pipelines import CreatePipelineRequest, PipelineVariable, _create_pipeline

    if advance_next_run:
        set_schedule_next_run(schedule, after=now or utc_now_naive())
        await db.commit()

    variables = [
        PipelineVariable(**variable)
        for variable in schedule.variables or []
        if isinstance(variable, dict)
    ]
    pipeline = await _create_pipeline(
        project_id,
        CreatePipelineRequest(ref=schedule.ref, variables=variables),
        db,
        source="schedule",
        actor=actor,
    )
    schedule.last_pipeline_id = pipeline.id
    if advance_next_run:
        set_schedule_next_run(schedule, after=now or utc_now_naive())
    await db.commit()
    await db.refresh(schedule)
    return pipeline


async def run_due_pipeline_schedules(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 25,
) -> ScheduleRunResult:
    """Create pipelines for active schedules whose next_run_at is due."""
    current = _normalize_naive_utc(now or utc_now_naive())
    result = await db.execute(
        select(PipelineSchedule)
        .options(selectinload(PipelineSchedule.project))
        .where(
            PipelineSchedule.active.is_(True),
            or_(
                PipelineSchedule.next_run_at.is_(None),
                PipelineSchedule.next_run_at <= current,
            ),
        )
        .order_by(PipelineSchedule.next_run_at.asc().nullsfirst(), PipelineSchedule.id.asc())
        .limit(limit)
    )
    schedules = result.scalars().all()
    stats = ScheduleRunResult(checked=len(schedules))
    created = 0
    failed = 0
    for schedule in schedules:
        try:
            set_schedule_next_run(schedule, after=current)
            await db.commit()
            await play_pipeline_schedule(
                schedule,
                schedule.project_id,
                db,
                advance_next_run=False,
                now=current,
            )
            created += 1
        except Exception:
            failed += 1
            await db.rollback()
            logger.exception("Failed to materialize pipeline schedule %s", schedule.id)
    return ScheduleRunResult(checked=stats.checked, created=created, failed=failed)


async def pipeline_schedule_worker(
    *,
    interval_seconds: float = 60.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Poll due active schedules until cancelled or a stop event is set."""
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        try:
            async with async_session() as db:
                delayed_stats = await promote_due_delayed_jobs(db, commit=True)
                stats = await run_due_pipeline_schedules(db)
                if delayed_stats.promoted or stats.created or stats.failed:
                    logger.info(
                        "Pipeline schedule worker delayed_promoted=%s "
                        "checked=%s created=%s failed=%s",
                        delayed_stats.promoted,
                        stats.checked,
                        stats.created,
                        stats.failed,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pipeline schedule worker iteration failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass


def http_cron_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _parse_cron_field(field: str, minimum: int, maximum: int, name: str) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"Invalid empty {name} cron field")
        step = 1
        base = part
        if "/" in part:
            base, step_text = part.split("/", 1)
            try:
                step = int(step_text)
            except ValueError as exc:
                raise ValueError(f"Invalid {name} cron step: {step_text}") from exc
            if step <= 0:
                raise ValueError(f"Invalid {name} cron step: {step_text}")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start = _parse_int(start_text, name)
            end = _parse_int(end_text, name)
        else:
            start = end = _parse_int(base, name)
        if start < minimum or end > maximum or start > end:
            raise ValueError(f"Invalid {name} cron range: {part}")
        values.update(range(start, end + 1, step))
    return values


def _parse_int(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid {name} cron value: {value}") from exc


def _as_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_naive_utc(value: datetime) -> datetime:
    return _as_utc_aware(value).replace(tzinfo=None)
