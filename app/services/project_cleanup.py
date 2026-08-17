"""Cleanup helpers for deleting project-owned persisted data."""

import os

from datetime import datetime

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.ci import (
    CiRunner,
    CiSecret,
    CiSecretAccessEvent,
    CiVariable,
    Pipeline,
    PipelineJob,
    PipelineSchedule,
    PipelineTrigger,
)


async def delete_project_ci_data(db: AsyncSession, project_id: int) -> None:
    """Delete all persisted CI data owned by a project.

    Variables and secrets use polymorphic scope columns rather than foreign
    keys, so they need explicit cleanup. Pipelines are loaded with their
    dependent jobs so SQLAlchemy's existing job/trace/artifact cascades apply.
    """
    await delete_project_pipelines(db, project_id)

    await db.execute(
        delete(CiVariable).where(
            CiVariable.scope_type == "project",
            CiVariable.scope_id == project_id,
        )
    )
    await db.execute(
        delete(CiSecret).where(
            CiSecret.scope_type == "project",
            CiSecret.scope_id == project_id,
        )
    )
    await db.execute(
        delete(PipelineTrigger).where(PipelineTrigger.project_id == project_id)
    )
    await db.execute(
        delete(PipelineSchedule).where(PipelineSchedule.project_id == project_id)
    )


async def delete_project_pipelines(
    db: AsyncSession,
    project_id: int,
    *,
    pipeline_ids: list[int] | None = None,
    created_before: datetime | None = None,
) -> int:
    """Delete selected pipelines and all of their persisted job data."""
    query = (
        select(Pipeline)
        .options(
            selectinload(Pipeline.jobs).selectinload(PipelineJob.trace),
            selectinload(Pipeline.jobs).selectinload(PipelineJob.artifacts),
        )
        .where(Pipeline.project_id == project_id)
    )
    if pipeline_ids is not None:
        query = query.where(Pipeline.id.in_(pipeline_ids))
    if created_before is not None:
        query = query.where(Pipeline.created_at < created_before)

    result = await db.execute(query)
    pipelines = list(result.scalars().all())
    pipeline_ids = [pipeline.id for pipeline in pipelines]
    job_ids = [job.id for pipeline in pipelines for job in pipeline.jobs]

    # Nullable references from unrelated schedules, secrets, and runners must
    # be cleared before the referenced project jobs/pipelines are removed.
    if pipeline_ids:
        await db.execute(
            update(PipelineSchedule)
            .where(PipelineSchedule.last_pipeline_id.in_(pipeline_ids))
            .values(last_pipeline_id=None)
        )
    if job_ids:
        await db.execute(
            update(CiSecret)
            .where(CiSecret.last_accessed_by_job_id.in_(job_ids))
            .values(last_accessed_by_job_id=None)
        )
        await db.execute(
            update(CiRunner)
            .where(CiRunner.last_job_id.in_(job_ids))
            .values(last_job_id=None)
        )

    # Access events reference all of these entities and may refer to a group
    # secret, so remove them before deleting either the jobs or project.
    event_filters = [CiSecretAccessEvent.project_id == project_id]
    if pipeline_ids:
        event_filters.append(CiSecretAccessEvent.pipeline_id.in_(pipeline_ids))
    if job_ids:
        event_filters.append(CiSecretAccessEvent.job_id.in_(job_ids))
    await db.execute(delete(CiSecretAccessEvent).where(or_(*event_filters)))

    for pipeline in pipelines:
        for job in pipeline.jobs:
            for artifact in job.artifacts:
                if artifact.storage_path:
                    try:
                        os.remove(artifact.storage_path)
                    except FileNotFoundError:
                        pass
        await db.delete(pipeline)

    return len(pipelines)
