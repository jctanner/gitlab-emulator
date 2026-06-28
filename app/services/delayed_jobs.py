"""Delayed CI job promotion helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.ci import Pipeline, PipelineJob

logger = logging.getLogger("gitlab_emulator.delayed_jobs")


@dataclass(frozen=True)
class DelayedJobPromotionResult:
    checked: int = 0
    promoted: int = 0


async def promote_due_delayed_jobs(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 100,
    commit: bool = False,
) -> DelayedJobPromotionResult:
    """Promote due delayed jobs from scheduled to pending."""
    current = _normalize_naive_utc(now or datetime.now(timezone.utc))
    result = await db.execute(
        select(PipelineJob)
        .options(selectinload(PipelineJob.pipeline).selectinload(Pipeline.jobs))
        .where(
            PipelineJob.status == "scheduled",
            or_(
                PipelineJob.scheduled_at.is_(None),
                PipelineJob.scheduled_at <= current,
            ),
        )
        .order_by(PipelineJob.scheduled_at.asc().nullsfirst(), PipelineJob.id.asc())
        .limit(limit)
    )
    checked = 0
    promoted = 0
    for job in result.scalars().all():
        checked += 1
        job.status = "pending"
        job.queued_at = current
        job.scheduled_at = None
        if job.pipeline.status not in {"running", "pending"}:
            job.pipeline.status = "pending"
            job.pipeline.finished_at = None
        promoted += 1
    if commit and promoted:
        await db.commit()
        logger.info("Promoted %s delayed job(s)", promoted)
    else:
        await db.flush()
    return DelayedJobPromotionResult(checked=checked, promoted=promoted)


def _normalize_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
