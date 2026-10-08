"""Bridge (trigger) job dispatch.

GitLab runs a bridge job under the same ordering rules as any other job: it
waits for earlier stages or its ``needs:``, is skipped when they fail, and
only then creates the downstream pipeline. Runners never receive bridge
jobs, so the server dispatches them whenever a pipeline's state is
re-derived.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import unquote

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ci import Pipeline, PipelineJob
from app.models.project import Project


def _ready_bridges(pipeline: Pipeline) -> list[PipelineJob]:
    from app.api.runner import _job_stage_is_unblocked

    return [
        job
        for job in sorted(pipeline.jobs, key=lambda job: (job.stage_index, job.id))
        if job.trigger_project
        and job.status == "pending"
        and job.downstream_pipeline_id is None
        and _job_stage_is_unblocked(job)
    ]


async def _bridge_target(job: PipelineJob, db: AsyncSession) -> Project:
    target_name = unquote(str(job.trigger_project)).strip("/")
    target = (
        await db.execute(select(Project).where(Project.full_name == target_name))
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(
            status_code=400,
            detail=f"Bridge job {job.name} target project not found: {target_name}",
        )
    return target


async def dispatch_ready_bridges(pipeline: Pipeline, db: AsyncSession) -> bool:
    """Create downstream pipelines for bridges whose dependencies are met.

    Returns True when any bridge changed state. Without ``strategy: depend``
    a bridge succeeds once its downstream pipeline exists, as in GitLab.
    """
    from app.api.pipelines import CreatePipelineRequest, _create_pipeline
    from app.api.runner import _skip_jobs_after_failed_stage

    changed = False
    while True:
        ready = _ready_bridges(pipeline)
        if not ready:
            return changed
        for job in ready:
            now = datetime.now(timezone.utc)
            job.started_at = job.started_at or now
            try:
                target = await _bridge_target(job, db)
                downstream = await _create_pipeline(
                    target.id,
                    CreatePipelineRequest(ref=job.trigger_ref or pipeline.ref),
                    db,
                    source="parent_pipeline",
                )
            except HTTPException as exc:
                # GitLab reports a downstream creation failure (for example,
                # no .gitlab-ci.yml in the target) on the bridge job.
                job.status = "failed"
                job.failure_reason = str(exc.detail)
                job.finished_at = now
                _skip_jobs_after_failed_stage(pipeline)
            else:
                job.downstream_pipeline_id = downstream.id
                job.status = "success"
                job.finished_at = now
            changed = True
