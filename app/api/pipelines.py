"""Minimal GitLab pipeline and job APIs."""

from __future__ import annotations

import asyncio
import os
import secrets
import ssl
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import urlopen

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
import yaml

from app.api.deps import CurrentUser, DbSession
from app.api.pagination import paginated_json
from app.api.runner import explain_job_scheduling
from app.config import settings
from app.models.ci import (
    CiRunner,
    CiSecretAccessEvent,
    JobTrace,
    Pipeline,
    PipelineJob,
    PipelineSchedule,
    PipelineTrigger,
)
from app.models.project import Project
from app.schemas.user import _fmt_dt
from app.services.ci_security import (
    pipeline_variable_policy,
    pipeline_security_warnings,
    strict_security_blocks,
)
from app.services.ci_secrets import ci_secret_metadata_entry, project_secret_entries
from app.services.ci_variables import project_variable_entries
from app.services.ci_yaml import ParsedCiJob, parse_gitlab_ci
from app.services.permissions import (
    pipeline_variables_allowed_for_access_level,
    project_access_level,
)

router = APIRouter(tags=["pipelines"])


CI_TEMPLATES: dict[str, str] = {
    "Bash.gitlab-ci.yml": """
.bash-template:
  image: alpine:3.20
  before_script:
    - echo bash template before
""",
    "Jobs/Build.gitlab-ci.yml": """
.build-template:
  stage: build
  script:
    - echo build template
""",
}


class PipelineVariable(BaseModel):
    key: str
    value: str
    variable_type: str = "env_var"
    file: bool = False
    masked: bool = False
    raw: bool = False
    public: bool | None = None


class PipelineJobDefinition(BaseModel):
    name: str = "smoke"
    stage: str = "test"
    stage_index: int = 0
    image: str = "alpine:3.20"
    script: list[str] = Field(default_factory=lambda: ["echo hello from persisted pipeline"])
    variables: dict[str, str] = Field(default_factory=dict)
    needs: list[str | dict] | None = None
    tags: list[str] = Field(default_factory=list)
    cache: list[dict] = Field(default_factory=list)
    artifacts_paths: list[str] = Field(default_factory=list)
    artifacts: dict = Field(default_factory=dict)


class CreatePipelineRequest(BaseModel):
    ref: str = "main"
    sha: str = "0000000000000000000000000000000000000000"
    variables: list[PipelineVariable] = Field(default_factory=list)
    job: PipelineJobDefinition | None = None


class CreatePipelineTriggerRequest(BaseModel):
    description: str = ""


class CreatePipelineScheduleRequest(BaseModel):
    description: str = ""
    ref: str = "main"
    cron: str = "0 0 * * *"
    cron_timezone: str = "UTC"
    active: bool = True
    variables: list[PipelineVariable] = Field(default_factory=list)


class UpdatePipelineScheduleRequest(BaseModel):
    description: str | None = None
    ref: str | None = None
    cron: str | None = None
    cron_timezone: str | None = None
    active: bool | None = None
    variables: list[PipelineVariable] | None = None


def _variable_entry(
    value: str,
    *,
    file: bool = False,
    masked: bool = False,
    raw: bool = False,
    public: bool | None = None,
) -> dict:
    return {
        "value": str(value),
        "file": file,
        "masked": masked,
        "raw": raw,
        "public": (not masked) if public is None else public,
    }


def _pipeline_variable_entries(variables: list[PipelineVariable]) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    for item in variables:
        entries[item.key] = _variable_entry(
            item.value,
            file=item.file or item.variable_type == "file",
            masked=item.masked,
            raw=item.raw,
            public=item.public,
        )
    return entries


def _pipeline_context_variable_entries(
    variables: list[PipelineVariable],
    *,
    source: str,
) -> dict[str, dict]:
    return {
        **_pipeline_variable_entries(variables),
        "CI_PIPELINE_SOURCE": _variable_entry(source),
    }


def _simple_variable_values(entries: dict[str, dict]) -> dict[str, str]:
    return {key: str(entry.get("value", "")) for key, entry in entries.items()}


async def _get_project(project_id: int, db: DbSession) -> Project:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project Not Found")
    return project


async def _get_project_ref(project_ref: str, db: DbSession) -> Project:
    decoded_ref = unquote(str(project_ref)).strip("/")
    if decoded_ref.isdigit():
        result = await db.execute(
            select(Project).where(Project.id == int(decoded_ref))
        )
    else:
        result = await db.execute(
            select(Project).where(Project.full_name == decoded_ref)
        )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(
            status_code=400,
            detail=f"CI include project not found: {project_ref}",
        )
    return project


async def _git_output(disk_path: str, *args: str) -> str | None:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "--git-dir",
        disk_path,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await proc.communicate()
    if proc.returncode != 0:
        return None
    return stdout.decode()


async def _resolve_ref(project: Project, ref: str) -> str:
    if not project.disk_path:
        return "0000000000000000000000000000000000000000"
    sha = await _git_output(project.disk_path, "rev-parse", f"refs/heads/{ref}")
    return sha.strip() if sha else "0000000000000000000000000000000000000000"


async def _repo_paths_at_ref(project: Project, ref: str) -> set[str]:
    if not project.disk_path:
        return set()
    output = await _git_output(project.disk_path, "ls-tree", "-r", "--name-only", ref)
    if not output:
        return set()
    return {line.strip() for line in output.splitlines() if line.strip()}


async def _changed_paths_at_sha(project: Project, sha: str) -> set[str]:
    if not project.disk_path or sha == "0000000000000000000000000000000000000000":
        return set()
    output = await _git_output(
        project.disk_path,
        "diff-tree",
        "--root",
        "--no-commit-id",
        "--name-only",
        "-r",
        sha,
    )
    if not output:
        return set()
    return {line.strip() for line in output.splitlines() if line.strip()}


async def _read_gitlab_ci(project: Project, ref: str) -> str:
    return await _read_repo_file(project, ref, ".gitlab-ci.yml", ".gitlab-ci.yml not found")


async def _read_repo_file(
    project: Project,
    ref: str,
    path: str,
    not_found_detail: str,
) -> str:
    if not project.disk_path:
        raise HTTPException(status_code=400, detail=not_found_detail)
    content = await _git_output(project.disk_path, "show", f"{ref}:{path}")
    if content is None:
        raise HTTPException(status_code=400, detail=not_found_detail)
    return content


def _ci_mapping(content: str, path: str) -> dict:
    try:
        parsed = yaml.safe_load(content) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"{path} is invalid YAML") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail=f"{path} must contain a mapping")
    return parsed


def _include_files(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _include_items(value: Any) -> list[dict]:
    if value is None:
        return []
    raw_items = value if isinstance(value, list) else [value]
    items: list[dict] = []
    for item in raw_items:
        if isinstance(item, str):
            items.append({"kind": "local", "file": item})
        elif isinstance(item, dict) and item.get("local"):
            for file_path in _include_files(item["local"]):
                items.append({"kind": "local", "file": file_path})
        elif isinstance(item, dict) and item.get("project") and item.get("file"):
            for file_path in _include_files(item["file"]):
                items.append(
                    {
                        "kind": "project",
                        "project": str(item["project"]),
                        "file": file_path,
                        "ref": str(item.get("ref") or "main"),
                    }
                )
        elif isinstance(item, dict) and item.get("remote"):
            items.append({"kind": "remote", "remote": str(item["remote"])})
        elif isinstance(item, dict) and item.get("template"):
            items.append({"kind": "template", "template": str(item["template"])})
        elif isinstance(item, dict):
            raise HTTPException(
                status_code=400,
                detail="Only local, project, remote, and template CI includes are supported",
            )
        else:
            raise HTTPException(status_code=400, detail="Invalid CI include")
    for include_item in items:
        if include_item.get("file"):
            include_item["file"] = str(include_item["file"]).lstrip("/")
    return items


def _merge_ci_config(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if key == "include":
            continue
        merged[key] = value
    return merged


def _allowed_remote_include_hosts() -> set[str]:
    return {
        host.strip().lower()
        for host in settings.CI_REMOTE_INCLUDE_ALLOWED_HOSTS.split(",")
        if host.strip()
    }


def _remote_include_allowed(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    return parsed.hostname.lower() in _allowed_remote_include_hosts()


def _fetch_remote_include_sync(url: str) -> str:
    context = ssl._create_unverified_context() if url.startswith("https://") else None
    with urlopen(url, timeout=10, context=context) as response:
        status = getattr(response, "status", 200)
        if status >= 400:
            raise HTTPException(status_code=400, detail=f"CI remote include failed: {url}")
        return response.read().decode("utf-8")


async def _fetch_remote_include(url: str) -> str:
    if not _remote_include_allowed(url):
        raise HTTPException(
            status_code=400,
            detail=f"CI remote include host is not allowed: {urlparse(url).hostname or url}",
        )
    try:
        return await asyncio.to_thread(_fetch_remote_include_sync, url)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"CI remote include failed: {url}",
        ) from exc


def _read_template_include(name: str) -> str:
    content = CI_TEMPLATES.get(name)
    if content is None:
        raise HTTPException(status_code=400, detail=f"CI template include not found: {name}")
    return content


async def _read_ci_config_with_includes(
    project: Project,
    ref: str,
    path: str,
    db: DbSession,
    depth: int = 0,
    seen: set[tuple[str, str, str, str]] | None = None,
) -> dict:
    if depth > 10:
        raise HTTPException(status_code=400, detail="CI include depth limit exceeded")
    seen = seen or set()
    normalized_path = path.lstrip("/")
    key = ("repo", str(project.id), ref, normalized_path)
    if key in seen:
        raise HTTPException(
            status_code=400,
            detail=f"Circular CI include detected: {normalized_path}",
        )
    seen.add(key)

    content = await _read_repo_file(
        project,
        ref,
        normalized_path,
        ".gitlab-ci.yml not found"
        if normalized_path == ".gitlab-ci.yml"
        else f"CI include not found: {normalized_path}",
    )
    try:
        return await _read_ci_config_content_with_includes(
            content,
            normalized_path,
            project,
            ref,
            db,
            depth,
            seen,
        )
    finally:
        seen.remove(key)


async def _read_ci_config_content_with_includes(
    content: str,
    path: str,
    project: Project,
    ref: str,
    db: DbSession,
    depth: int,
    seen: set[tuple[str, str, str, str]],
) -> dict:
    if depth > 10:
        raise HTTPException(status_code=400, detail="CI include depth limit exceeded")
    root = _ci_mapping(content, path)
    merged: dict = {}
    for include_item in _include_items(root.get("include")):
        include_config = await _read_include_config(
            include_item,
            project,
            ref,
            db,
            depth + 1,
            seen,
        )
        merged = _merge_ci_config(merged, include_config)
    return _merge_ci_config(merged, root)


async def _read_include_config(
    include_item: dict,
    project: Project,
    ref: str,
    db: DbSession,
    depth: int,
    seen: set[tuple[str, str, str, str]],
) -> dict:
    include_project = project
    include_ref = ref
    if include_item["kind"] == "project":
        include_project = await _get_project_ref(include_item["project"], db)
        include_ref = include_item["ref"]
    if include_item["kind"] in {"local", "project"}:
        return await _read_ci_config_with_includes(
            include_project,
            include_ref,
            include_item["file"],
            db,
            depth,
            seen,
        )
    if include_item["kind"] == "remote":
        url = include_item["remote"]
        key = ("remote", url, "", "")
        if key in seen:
            raise HTTPException(status_code=400, detail=f"Circular CI include detected: {url}")
        seen.add(key)
        try:
            return await _read_ci_config_content_with_includes(
                await _fetch_remote_include(url),
                url,
                project,
                ref,
                db,
                depth,
                seen,
            )
        finally:
            seen.remove(key)
    if include_item["kind"] == "template":
        name = include_item["template"]
        key = ("template", name, "", "")
        if key in seen:
            raise HTTPException(status_code=400, detail=f"Circular CI include detected: {name}")
        seen.add(key)
        try:
            return await _read_ci_config_content_with_includes(
                _read_template_include(name),
                f"template:{name}",
                project,
                ref,
                db,
                depth,
                seen,
            )
        finally:
            seen.remove(key)
    raise HTTPException(status_code=400, detail="Invalid CI include")


async def _read_gitlab_ci_with_includes(project: Project, ref: str, db: DbSession) -> str:
    merged = await _read_ci_config_with_includes(
        project,
        ref,
        ".gitlab-ci.yml",
        db,
    )
    return yaml.safe_dump(merged, sort_keys=False)


def _pipeline_json(pipeline: Pipeline) -> dict:
    return {
        "id": pipeline.id,
        "iid": pipeline.iid,
        "project_id": pipeline.project_id,
        "sha": pipeline.sha,
        "ref": pipeline.ref,
        "status": pipeline.status,
        "source": pipeline.source,
        "security_warnings": pipeline.security_warnings or [],
        "created_at": _fmt_dt(pipeline.created_at),
        "updated_at": _fmt_dt(pipeline.updated_at),
        "web_url": f"{settings.BASE_URL}/{pipeline.project.full_name}/-/pipelines/{pipeline.id}"
        if pipeline.project
        else None,
    }


def _trigger_json(trigger: PipelineTrigger) -> dict:
    owner = trigger.owner
    return {
        "id": trigger.id,
        "description": trigger.description,
        "token": trigger.token,
        "owner": {
            "id": owner.id,
            "username": owner.login,
            "name": owner.name or owner.login,
        }
        if owner
        else None,
        "last_used": _fmt_dt(trigger.last_used_at),
        "created_at": _fmt_dt(trigger.created_at),
        "updated_at": _fmt_dt(trigger.updated_at),
    }


def _schedule_json(schedule: PipelineSchedule) -> dict:
    return {
        "id": schedule.id,
        "description": schedule.description,
        "ref": schedule.ref,
        "cron": schedule.cron,
        "cron_timezone": schedule.cron_timezone,
        "next_run_at": _fmt_dt(schedule.next_run_at),
        "active": schedule.active,
        "created_at": _fmt_dt(schedule.created_at),
        "updated_at": _fmt_dt(schedule.updated_at),
        "last_pipeline": _pipeline_json(schedule.last_pipeline)
        if schedule.last_pipeline
        else None,
        "owner": {
            "id": schedule.owner.id,
            "username": schedule.owner.login,
            "name": schedule.owner.name or schedule.owner.login,
        }
        if schedule.owner
        else None,
        "variables": schedule.variables or [],
    }


def _need_items(needs: list | None) -> list[dict]:
    if needs is None:
        return []
    items: list[dict] = []
    for need in needs:
        if isinstance(need, str):
            items.append({"job": need, "optional": False, "artifacts": True})
        elif isinstance(need, dict) and need.get("job"):
            items.append(
                {
                    "job": str(need["job"]),
                    "optional": bool(need.get("optional", False)),
                    "artifacts": bool(need.get("artifacts", True)),
                }
            )
    return items


def _need_names(needs: list | None) -> list[str]:
    return [item["job"] for item in _need_items(needs)]


def _reset_job_for_retry(job: PipelineJob, now: datetime) -> None:
    job.status = "pending"
    job.job_token = f"gljt-persisted-{secrets.token_urlsafe(24)}"
    job.runner_name = None
    job.failure_reason = None
    job.exit_code = None
    job.trace_checksum = None
    job.trace_size = 0
    job.queued_at = now
    job.started_at = None
    job.finished_at = None
    if job.trace:
        job.trace.content = ""
        job.trace.size = 0


def _requeue_stale_or_pending_job(job: PipelineJob, now: datetime) -> dict:
    """Reset a pending/running job for another runner poll.

    This is intentionally operator-only behavior for the emulator. GitLab-shaped
    clients should use cancel/retry; the CI Lab requeue path exists for
    recovering jobs abandoned by a runner while keeping the same job record.
    """
    previous = {
        "status": job.status,
        "runner_name": job.runner_name,
        "trace_size": job.trace_size or 0,
        "started_at": job.started_at,
    }
    _reset_job_for_retry(job, now)
    return previous


def _validate_job_needs(parsed_jobs: list[ParsedCiJob]) -> None:
    jobs_by_name = {job.name: job for job in parsed_jobs}
    for job in parsed_jobs:
        needs = _need_items(job.needs)
        seen: set[str] = set()
        duplicate_names: list[str] = []
        for item in needs:
            if item["job"] in seen:
                duplicate_names.append(item["job"])
            seen.add(item["job"])
        if duplicate_names:
            names = ", ".join(sorted(set(duplicate_names)))
            raise HTTPException(
                status_code=400,
                detail=f"Job {job.name} has duplicate needs: {names}",
            )
        if job.name in seen:
            raise HTTPException(
                status_code=400,
                detail=f"Job {job.name} cannot need itself",
            )
        missing = [
            item["job"]
            for item in needs
            if item["job"] not in jobs_by_name and not item["optional"]
        ]
        if missing:
            names = ", ".join(missing)
            raise HTTPException(
                status_code=400,
                detail=f"Job {job.name} needs missing job(s): {names}",
            )
        future_stage = [
            item["job"]
            for item in needs
            if item["job"] in jobs_by_name
            and jobs_by_name[item["job"]].stage_index > job.stage_index
        ]
        if future_stage:
            names = ", ".join(future_stage)
            raise HTTPException(
                status_code=400,
                detail=f"Job {job.name} needs future-stage job(s): {names}",
            )


def _job_json(job: PipelineJob) -> dict:
    artifacts = [
        {
            "file_type": artifact.file_type,
            "file_format": artifact.file_format,
            "filename": artifact.filename,
            "size": artifact.size,
            "expire_at": _fmt_dt(artifact.expire_at),
            "created_at": _fmt_dt(artifact.created_at),
        }
        for artifact in job.artifacts
    ]
    return {
        "id": job.id,
        "status": job.status,
        "stage": job.stage,
        "stage_index": job.stage_index,
        "name": job.name,
        "needs": _need_names(job.needs),
        "tag_list": job.tags or [],
        "cache": job.cache or [],
        "ref": job.pipeline.ref if job.pipeline else None,
        "tag": False,
        "coverage": None,
        "allow_failure": False,
        "created_at": _fmt_dt(job.created_at),
        "started_at": _fmt_dt(job.started_at),
        "finished_at": _fmt_dt(job.finished_at),
        "erased_at": None,
        "duration": None,
        "queued_duration": None,
        "user": None,
        "commit": {"id": job.pipeline.sha, "short_id": job.pipeline.sha[:8]} if job.pipeline else None,
        "pipeline": {
            "id": job.pipeline.id,
            "iid": job.pipeline.iid,
            "project_id": job.pipeline.project_id,
            "sha": job.pipeline.sha,
            "ref": job.pipeline.ref,
            "status": job.pipeline.status,
        }
        if job.pipeline
        else None,
        "web_url": f"{settings.BASE_URL}/{job.project.full_name}/-/jobs/{job.id}"
        if job.project
        else None,
        "artifacts": artifacts,
        "secret_metadata": job.secret_metadata or [],
        "runner": {"description": job.runner_name} if job.runner_name else None,
    }


async def _derive_pipeline_status(pipeline: Pipeline, db: DbSession) -> None:
    await db.refresh(pipeline, attribute_names=["jobs"])
    statuses = [job.status for job in pipeline.jobs]
    now = datetime.now(timezone.utc)

    if not statuses:
        pipeline.status = "pending"
    elif all(status == "canceled" for status in statuses):
        pipeline.status = "canceled"
        pipeline.finished_at = pipeline.finished_at or now
    elif any(status == "canceled" for status in statuses) and not any(
        status in {"pending", "running"} for status in statuses
    ):
        pipeline.status = "canceled"
        pipeline.finished_at = pipeline.finished_at or now
    elif any(status == "failed" for status in statuses):
        pipeline.status = "failed"
        pipeline.finished_at = pipeline.finished_at or now
    elif all(status in {"success", "skipped", "manual"} for status in statuses):
        pipeline.status = "success"
        pipeline.finished_at = pipeline.finished_at or now
    elif any(status == "running" for status in statuses):
        pipeline.status = "running"
        pipeline.started_at = pipeline.started_at or now
    else:
        pipeline.status = "pending"


async def _create_pipeline(
    project_id: int,
    body: CreatePipelineRequest,
    db: DbSession,
    *,
    source: str = "api",
    actor=None,
) -> Pipeline:
    """Create a persisted pipeline from a direct job or `.gitlab-ci.yml`."""
    project = await _get_project(project_id, db)
    parsed_jobs: list[ParsedCiJob]
    ci_content = ""
    sha = body.sha
    if sha == "0000000000000000000000000000000000000000":
        sha = await _resolve_ref(project, body.ref)

    if body.variables and source not in {"trigger", "schedule"}:
        access_level = await project_access_level(project, actor, db)
        if not pipeline_variables_allowed_for_access_level(
            policy=pipeline_variable_policy(project.ci_security_settings),
            access_level=access_level,
        ):
            raise HTTPException(
                status_code=400,
                detail="Pipeline variables are not allowed by project CI security settings",
            )

    if body.job is not None:
        parsed_jobs = [
            ParsedCiJob(
                name=body.job.name,
                stage=body.job.stage,
                stage_index=body.job.stage_index,
                image=body.job.image,
                script=body.job.script,
                variables=body.job.variables,
                variable_metadata={
                    key: _variable_entry(value)
                    for key, value in body.job.variables.items()
                },
                needs=body.job.needs,
                tags=body.job.tags,
                cache=body.job.cache,
                artifacts_paths=body.job.artifacts_paths,
                artifacts=body.job.artifacts
                or (
                    {
                        "name": "artifacts",
                        "untracked": False,
                        "paths": body.job.artifacts_paths,
                        "exclude": [],
                        "when": "on_success",
                        "expire_in": "",
                        "artifact_type": "archive",
                        "artifact_format": "zip",
                    }
                    if body.job.artifacts_paths
                    else {}
                ),
            )
        ]
    else:
        try:
            rule_project_variable_entries = await project_variable_entries(
                project,
                db,
                ref=body.ref,
            )
            pipeline_variable_entries = _pipeline_context_variable_entries(
                body.variables,
                source=source,
            )
            ci_content = await _read_repo_file(
                project,
                body.ref,
                ".gitlab-ci.yml",
                ".gitlab-ci.yml not found",
            )
            merged_ci_content = await _read_gitlab_ci_with_includes(project, body.ref, db)
            parsed_jobs = parse_gitlab_ci(
                merged_ci_content,
                ref=body.ref,
                variables=_simple_variable_values(
                    {
                        **rule_project_variable_entries,
                        **pipeline_variable_entries,
                    }
                ),
                existing_paths=await _repo_paths_at_ref(project, body.ref),
                changed_paths=await _changed_paths_at_sha(project, sha),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    _validate_job_needs(parsed_jobs)

    max_iid = (
        await db.execute(
            select(func.max(Pipeline.iid)).where(Pipeline.project_id == project.id)
        )
    ).scalar()
    security_warnings = pipeline_security_warnings(
        ci_content=ci_content,
        parsed_jobs=parsed_jobs,
        pipeline_variables=body.variables,
        settings=project.ci_security_settings,
    )
    blocking_warnings = strict_security_blocks(
        security_warnings,
        project.ci_security_settings,
    )
    if blocking_warnings:
        messages = "; ".join(warning["message"] for warning in blocking_warnings)
        raise HTTPException(
            status_code=400,
            detail=f"Pipeline blocked by CI strict security mode: {messages}",
        )

    pipeline = Pipeline(
        project_id=project.id,
        iid=(max_iid or 0) + 1,
        ref=body.ref,
        sha=sha,
        status="pending",
        source=source,
        security_warnings=security_warnings,
    )
    db.add(pipeline)
    await db.flush()

    pipeline_variable_entries = _pipeline_context_variable_entries(
        body.variables,
        source=source,
    )
    for parsed_job in parsed_jobs:
        project_variable_metadata = await project_variable_entries(
            project,
            db,
            ref=body.ref,
            environment=parsed_job.environment,
        )
        variables = {
            **project_variable_metadata,
            **pipeline_variable_entries,
            **(
                parsed_job.variable_metadata
                if parsed_job.variable_metadata
                else {
                    key: _variable_entry(value)
                    for key, value in parsed_job.variables.items()
                }
            ),
        }
        try:
            secret_entries, resolved_secrets = await project_secret_entries(
                project,
                db,
                ref=body.ref,
                environment=parsed_job.environment,
                secrets=parsed_job.secrets,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        variables.update(secret_entries)
        job = PipelineJob(
            pipeline_id=pipeline.id,
            project_id=project.id,
            name=parsed_job.name,
            stage=parsed_job.stage,
            stage_index=parsed_job.stage_index,
            image=parsed_job.image,
            script=parsed_job.script,
            variables=variables,
            needs=_need_items(parsed_job.needs) if parsed_job.needs is not None else None,
            tags=parsed_job.tags,
            cache=parsed_job.cache,
            artifacts_paths=parsed_job.artifacts_paths,
            artifacts_config=parsed_job.artifacts,
            secret_metadata=[
                ci_secret_metadata_entry(resolved_secret)
                for resolved_secret in resolved_secrets
            ],
            job_token=f"gljt-persisted-{secrets.token_urlsafe(24)}",
            status="manual" if parsed_job.when == "manual" else "pending",
        )
        db.add(job)
        await db.flush()
        now = datetime.now(timezone.utc)
        for resolved_secret in resolved_secrets:
            resolved_secret.secret.last_accessed_at = now
            resolved_secret.secret.last_accessed_by_job_id = job.id
            db.add(
                CiSecretAccessEvent(
                    secret_id=resolved_secret.secret.id,
                    project_id=project.id,
                    pipeline_id=pipeline.id,
                    job_id=job.id,
                    ref=body.ref,
                    environment=parsed_job.environment,
                    accessed_at=now,
                )
            )
        db.add(JobTrace(job_id=job.id, content="", size=0))
    await db.commit()
    await db.refresh(pipeline)
    return pipeline


@router.get("/projects/{project_id}/triggers")
async def list_pipeline_triggers(project_id: int, db: DbSession):
    await _get_project(project_id, db)
    result = await db.execute(
        select(PipelineTrigger)
        .where(PipelineTrigger.project_id == project_id)
        .order_by(PipelineTrigger.id.asc())
    )
    return [_trigger_json(trigger) for trigger in result.scalars().all()]


@router.post("/projects/{project_id}/triggers", status_code=201)
async def create_pipeline_trigger(
    project_id: int,
    body: CreatePipelineTriggerRequest,
    db: DbSession,
):
    project = await _get_project(project_id, db)
    trigger = PipelineTrigger(
        project_id=project.id,
        description=body.description,
        token=f"glptt-{secrets.token_urlsafe(24)}",
        owner_id=project.owner_id,
    )
    db.add(trigger)
    await db.commit()
    await db.refresh(trigger)
    return _trigger_json(trigger)


@router.delete("/projects/{project_id}/triggers/{trigger_id}", status_code=204)
async def delete_pipeline_trigger(project_id: int, trigger_id: int, db: DbSession):
    await _get_project(project_id, db)
    result = await db.execute(
        select(PipelineTrigger).where(
            PipelineTrigger.project_id == project_id,
            PipelineTrigger.id == trigger_id,
        )
    )
    trigger = result.scalar_one_or_none()
    if trigger is None:
        raise HTTPException(status_code=404, detail="Pipeline Trigger Not Found")
    await db.delete(trigger)
    await db.commit()
    return Response(status_code=204)


def _variables_from_trigger_payload(data: dict[str, Any]) -> list[PipelineVariable]:
    variables: list[PipelineVariable] = []
    for key, value in data.items():
        if key.startswith("variables[") and key.endswith("]"):
            variable_key = key[len("variables[") : -1]
            if variable_key:
                variables.append(PipelineVariable(key=variable_key, value=str(value)))
    return variables


@router.post("/projects/{project_id}/trigger/pipeline", status_code=201)
async def trigger_pipeline(project_id: int, request: Request, db: DbSession):
    await _get_project(project_id, db)
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = await request.json()
    else:
        form = await request.form()
        payload = dict(form.multi_items())
    token = str(payload.get("token") or "")
    ref = str(payload.get("ref") or "")
    if not token or not ref:
        raise HTTPException(status_code=400, detail="token and ref are required")
    result = await db.execute(
        select(PipelineTrigger).where(
            PipelineTrigger.project_id == project_id,
            PipelineTrigger.token == token,
        )
    )
    trigger = result.scalar_one_or_none()
    if trigger is None:
        raise HTTPException(status_code=404, detail="Pipeline Trigger Not Found")

    trigger.last_used_at = datetime.now(timezone.utc)
    variables = _variables_from_trigger_payload(payload)
    pipeline = await _create_pipeline(
        project_id,
        CreatePipelineRequest(ref=ref, variables=variables),
        db,
        source="trigger",
    )
    return _pipeline_json(pipeline)


@router.get("/projects/{project_id}/pipeline_schedules")
async def list_pipeline_schedules(project_id: int, db: DbSession):
    await _get_project(project_id, db)
    result = await db.execute(
        select(PipelineSchedule)
        .where(PipelineSchedule.project_id == project_id)
        .order_by(PipelineSchedule.id.asc())
    )
    return [_schedule_json(schedule) for schedule in result.scalars().all()]


@router.post("/projects/{project_id}/pipeline_schedules", status_code=201)
async def create_pipeline_schedule(
    project_id: int,
    body: CreatePipelineScheduleRequest,
    db: DbSession,
):
    project = await _get_project(project_id, db)
    schedule = PipelineSchedule(
        project_id=project.id,
        description=body.description,
        ref=body.ref,
        cron=body.cron,
        cron_timezone=body.cron_timezone,
        active=body.active,
        variables=[variable.model_dump() for variable in body.variables],
        owner_id=project.owner_id,
    )
    db.add(schedule)
    await db.commit()
    await db.refresh(schedule)
    return _schedule_json(schedule)


@router.get("/projects/{project_id}/pipeline_schedules/{schedule_id}")
async def get_pipeline_schedule(project_id: int, schedule_id: int, db: DbSession):
    schedule = await _get_pipeline_schedule(project_id, schedule_id, db)
    return _schedule_json(schedule)


@router.put("/projects/{project_id}/pipeline_schedules/{schedule_id}")
async def update_pipeline_schedule(
    project_id: int,
    schedule_id: int,
    body: UpdatePipelineScheduleRequest,
    db: DbSession,
):
    schedule = await _get_pipeline_schedule(project_id, schedule_id, db)
    updates = body.model_dump(exclude_unset=True)
    if "variables" in updates and updates["variables"] is not None:
        updates["variables"] = [variable.model_dump() for variable in body.variables or []]
    for key, value in updates.items():
        if value is not None:
            setattr(schedule, key, value)
    await db.commit()
    await db.refresh(schedule)
    return _schedule_json(schedule)


@router.delete("/projects/{project_id}/pipeline_schedules/{schedule_id}", status_code=204)
async def delete_pipeline_schedule(project_id: int, schedule_id: int, db: DbSession):
    schedule = await _get_pipeline_schedule(project_id, schedule_id, db)
    await db.delete(schedule)
    await db.commit()
    return Response(status_code=204)


async def _get_pipeline_schedule(
    project_id: int,
    schedule_id: int,
    db: DbSession,
) -> PipelineSchedule:
    await _get_project(project_id, db)
    result = await db.execute(
        select(PipelineSchedule).where(
            PipelineSchedule.project_id == project_id,
            PipelineSchedule.id == schedule_id,
        )
    )
    schedule = result.scalar_one_or_none()
    if schedule is None:
        raise HTTPException(status_code=404, detail="Pipeline Schedule Not Found")
    return schedule


@router.post("/projects/{project_id}/pipeline_schedules/{schedule_id}/play", status_code=201)
async def play_pipeline_schedule(project_id: int, schedule_id: int, db: DbSession):
    schedule = await _get_pipeline_schedule(project_id, schedule_id, db)
    variables = [PipelineVariable(**variable) for variable in schedule.variables or []]
    pipeline = await _create_pipeline(
        project_id,
        CreatePipelineRequest(ref=schedule.ref, variables=variables),
        db,
        source="schedule",
    )
    schedule.last_pipeline_id = pipeline.id
    await db.commit()
    await db.refresh(schedule)
    return _pipeline_json(pipeline)


@router.post("/projects/{project_ref:path}/pipeline", status_code=201)
async def create_pipeline(
    project_ref: str,
    body: CreatePipelineRequest,
    db: DbSession,
    current_user: CurrentUser,
):
    """Create a minimal pipeline from a direct job or `.gitlab-ci.yml`."""
    project = await _get_project_ref(project_ref, db)
    pipeline = await _create_pipeline(
        project.id,
        body,
        db,
        source="api",
        actor=current_user,
    )
    return _pipeline_json(pipeline)


@router.get("/projects/{project_ref:path}/pipelines")
async def list_pipelines(
    project_ref: str,
    request: Request,
    db: DbSession,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    project = await _get_project_ref(project_ref, db)
    query = (
        select(Pipeline)
        .where(Pipeline.project_id == project.id)
        .order_by(Pipeline.id.desc())
    )
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    result = await db.execute(query.offset((page - 1) * per_page).limit(per_page))
    return paginated_json(
        [_pipeline_json(pipeline) for pipeline in result.scalars().all()],
        request,
        page,
        per_page,
        total,
    )


@router.get("/projects/{project_ref:path}/pipelines/latest")
async def get_latest_pipeline(
    project_ref: str,
    db: DbSession,
    ref: str | None = None,
):
    project = await _get_project_ref(project_ref, db)
    query = select(Pipeline).where(Pipeline.project_id == project.id)
    if ref:
        query = query.where(Pipeline.ref == ref)
    result = await db.execute(query.order_by(Pipeline.id.desc()).limit(1))
    pipeline = result.scalar_one_or_none()
    if pipeline is None:
        raise HTTPException(status_code=404, detail="Pipeline Not Found")
    return _pipeline_json(pipeline)


@router.get("/projects/{project_ref:path}/pipelines/{pipeline_id}")
async def get_pipeline(project_ref: str, pipeline_id: int, db: DbSession):
    pipeline = await _get_pipeline_for_project_ref(project_ref, pipeline_id, db)
    return _pipeline_json(pipeline)


async def _get_pipeline_for_project_ref(
    project_ref: str,
    pipeline_id: int,
    db: DbSession,
) -> Pipeline:
    project = await _get_project_ref(project_ref, db)
    result = await db.execute(
        select(Pipeline)
        .options(selectinload(Pipeline.jobs).selectinload(PipelineJob.trace))
        .where(
            Pipeline.project_id == project.id,
            Pipeline.id == pipeline_id,
        )
    )
    pipeline = result.scalar_one_or_none()
    if pipeline is None:
        raise HTTPException(status_code=404, detail="Pipeline Not Found")
    return pipeline


@router.post("/projects/{project_ref:path}/pipelines/{pipeline_id}/cancel")
async def cancel_pipeline(project_ref: str, pipeline_id: int, db: DbSession):
    pipeline = await _get_pipeline_for_project_ref(project_ref, pipeline_id, db)
    now = datetime.now(timezone.utc)
    for job in pipeline.jobs:
        if job.status in {"pending", "running", "manual"}:
            job.status = "canceled"
            job.finished_at = job.finished_at or now
    pipeline.status = "canceled"
    pipeline.finished_at = pipeline.finished_at or now
    await db.commit()
    await db.refresh(pipeline)
    return _pipeline_json(pipeline)


@router.post("/projects/{project_ref:path}/pipelines/{pipeline_id}/retry")
async def retry_pipeline(project_ref: str, pipeline_id: int, db: DbSession):
    pipeline = await _get_pipeline_for_project_ref(project_ref, pipeline_id, db)
    now = datetime.now(timezone.utc)
    retryable = {"failed", "canceled", "skipped"}
    for job in pipeline.jobs:
        if job.status in retryable:
            _reset_job_for_retry(job, now)
    await _derive_pipeline_status(pipeline, db)
    pipeline.finished_at = None if pipeline.status in {"pending", "running"} else pipeline.finished_at
    await db.commit()
    await db.refresh(pipeline)
    return _pipeline_json(pipeline)


@router.get("/projects/{project_ref:path}/pipelines/{pipeline_id}/diagnostics")
async def get_pipeline_diagnostics(project_ref: str, pipeline_id: int, db: DbSession):
    pipeline = await _get_pipeline_for_project_ref(project_ref, pipeline_id, db)
    runner_result = await db.execute(
        select(CiRunner).order_by(
            CiRunner.last_contact_at.desc().nullslast(),
            CiRunner.id.asc(),
        )
    )
    runner = runner_result.scalars().first()
    explanations = explain_job_scheduling(list(pipeline.jobs), runner)
    return {
        "pipeline": _pipeline_json(pipeline),
        "security_warnings": pipeline.security_warnings or [],
        "runner": None
        if runner is None
        else {
            "id": runner.id,
            "description": runner.description,
            "tag_list": runner.tags or [],
            "run_untagged": bool(runner.run_untagged),
            "paused": bool(runner.paused),
            "last_contact_at": runner.last_contact_at,
            "last_poll_at": runner.last_poll_at,
            "last_job_id": runner.last_job_id,
        },
        "jobs": [explanations[job.id] for job in pipeline.jobs],
    }


@router.get("/projects/{project_ref:path}/pipelines/{pipeline_id}/jobs")
async def list_pipeline_jobs(
    project_ref: str,
    pipeline_id: int,
    request: Request,
    db: DbSession,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    project = await _get_project_ref(project_ref, db)
    query = (
        select(PipelineJob)
        .join(Pipeline)
        .where(Pipeline.project_id == project.id, Pipeline.id == pipeline_id)
        .order_by(PipelineJob.id.asc())
    )
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    result = await db.execute(query.offset((page - 1) * per_page).limit(per_page))
    return paginated_json(
        [_job_json(job) for job in result.scalars().all()],
        request,
        page,
        per_page,
        total,
    )


@router.get("/projects/{project_ref:path}/jobs")
async def list_project_jobs(
    project_ref: str,
    request: Request,
    db: DbSession,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    project = await _get_project_ref(project_ref, db)
    query = (
        select(PipelineJob)
        .where(PipelineJob.project_id == project.id)
        .order_by(PipelineJob.id.desc())
    )
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    result = await db.execute(query.offset((page - 1) * per_page).limit(per_page))
    return paginated_json(
        [_job_json(job) for job in result.scalars().all()],
        request,
        page,
        per_page,
        total,
    )


@router.get("/projects/{project_ref:path}/jobs/artifacts/{ref_name:path}/download")
async def download_project_job_artifacts_by_ref(
    project_ref: str,
    ref_name: str,
    db: DbSession,
    job: str,
):
    project = await _get_project_ref(project_ref, db)
    result = await db.execute(
        select(PipelineJob)
        .join(Pipeline)
        .options(selectinload(PipelineJob.artifacts))
        .where(
            Pipeline.project_id == project.id,
            Pipeline.ref == unquote(ref_name),
            Pipeline.status == "success",
            PipelineJob.name == job,
            PipelineJob.status == "success",
        )
        .order_by(Pipeline.id.desc(), PipelineJob.id.asc())
        .limit(1)
    )
    pipeline_job = result.scalar_one_or_none()
    if pipeline_job is None:
        raise HTTPException(status_code=404, detail="Job Artifacts Not Found")
    artifact = pipeline_job.artifacts[0] if pipeline_job.artifacts else None
    if artifact is None or not artifact.storage_path:
        raise HTTPException(status_code=404, detail="Artifacts Not Found")
    if artifact.expire_at and artifact.expire_at <= datetime.now(timezone.utc).replace(tzinfo=None):
        raise HTTPException(status_code=404, detail="Artifacts Expired")
    if not os.path.isfile(artifact.storage_path):
        raise HTTPException(status_code=404, detail="Artifacts Not Found")
    return FileResponse(
        artifact.storage_path,
        media_type=artifact.content_type or "application/zip",
        filename=artifact.filename,
    )


@router.get("/projects/{project_ref:path}/jobs/{job_id}")
async def get_project_job(project_ref: str, job_id: int, db: DbSession):
    job = await _get_job_for_project_ref(project_ref, job_id, db)
    return _job_json(job)


async def _get_job_for_project_ref(
    project_ref: str,
    job_id: int,
    db: DbSession,
) -> PipelineJob:
    project = await _get_project_ref(project_ref, db)
    result = await db.execute(
        select(PipelineJob)
        .options(
            selectinload(PipelineJob.pipeline).selectinload(Pipeline.jobs),
            selectinload(PipelineJob.trace),
            selectinload(PipelineJob.artifacts),
        )
        .where(
            PipelineJob.project_id == project.id,
            PipelineJob.id == job_id,
        )
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job Not Found")
    return job


@router.post("/projects/{project_ref:path}/jobs/{job_id}/cancel")
async def cancel_project_job(project_ref: str, job_id: int, db: DbSession):
    job = await _get_job_for_project_ref(project_ref, job_id, db)
    now = datetime.now(timezone.utc)
    if job.status in {"pending", "running", "manual"}:
        job.status = "canceled"
        job.finished_at = job.finished_at or now
    await _derive_pipeline_status(job.pipeline, db)
    await db.commit()
    await db.refresh(job)
    return _job_json(job)


@router.post("/projects/{project_ref:path}/jobs/{job_id}/retry")
async def retry_project_job(project_ref: str, job_id: int, db: DbSession):
    job = await _get_job_for_project_ref(project_ref, job_id, db)
    if job.status in {"failed", "canceled", "skipped", "success"}:
        _reset_job_for_retry(job, datetime.now(timezone.utc))
    await _derive_pipeline_status(job.pipeline, db)
    job.pipeline.finished_at = None if job.pipeline.status in {"pending", "running"} else job.pipeline.finished_at
    await db.commit()
    await db.refresh(job)
    return _job_json(job)


@router.post("/projects/{project_ref:path}/jobs/{job_id}/play")
async def play_project_job(project_ref: str, job_id: int, db: DbSession):
    job = await _get_job_for_project_ref(project_ref, job_id, db)
    if job.status != "manual":
        raise HTTPException(status_code=400, detail="Job is not playable")
    now = datetime.now(timezone.utc)
    job.status = "pending"
    job.queued_at = now
    job.failure_reason = None
    job.exit_code = None
    await _derive_pipeline_status(job.pipeline, db)
    job.pipeline.finished_at = None if job.pipeline.status in {"pending", "running"} else job.pipeline.finished_at
    await db.commit()
    await db.refresh(job)
    return _job_json(job)


@router.get("/projects/{project_ref:path}/jobs/{job_id}/trace")
async def get_project_job_trace(project_ref: str, job_id: int, db: DbSession):
    project = await _get_project_ref(project_ref, db)
    result = await db.execute(
        select(PipelineJob).where(
            PipelineJob.project_id == project.id,
            PipelineJob.id == job_id,
        )
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job Not Found")
    return Response(
        content=job.trace.content if job.trace else "",
        media_type="text/plain",
    )


@router.get("/projects/{project_ref:path}/jobs/{job_id}/artifacts")
async def download_project_job_artifacts(project_ref: str, job_id: int, db: DbSession):
    project = await _get_project_ref(project_ref, db)
    result = await db.execute(
        select(PipelineJob).where(
            PipelineJob.project_id == project.id,
            PipelineJob.id == job_id,
        )
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job Not Found")
    artifact = job.artifacts[0] if job.artifacts else None
    if artifact is None or not artifact.storage_path:
        raise HTTPException(status_code=404, detail="Artifacts Not Found")
    if artifact.expire_at and artifact.expire_at <= datetime.now(timezone.utc).replace(tzinfo=None):
        raise HTTPException(status_code=404, detail="Artifacts Expired")
    if not os.path.isfile(artifact.storage_path):
        raise HTTPException(status_code=404, detail="Artifacts Not Found")
    return FileResponse(
        artifact.storage_path,
        media_type=artifact.content_type or "application/zip",
        filename=artifact.filename,
    )
