"""Minimal GitLab Runner coordinator endpoints.

This is the validation slice for official gitlab-runner integration. It
supports registration, no-job polling, and persisted pipeline job execution.
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import DbSession
from app.config import settings
from app.models.ci import CiRunner, JobArtifact, JobTrace, Pipeline, PipelineJob
from app.services.ci_redaction import redact_trace_text
from app.services.delayed_jobs import promote_due_delayed_jobs

router = APIRouter(tags=["runner"])

EMULATOR_RUNNER_ID = 1
EMULATOR_RUNNER_TOKEN = "glrt-emulator-runner-token"
RUNNING_JOB_STALE_AFTER = timedelta(minutes=30)


class RunnerInfo(BaseModel):
    name: str | None = None
    version: str | None = None
    revision: str | None = None
    platform: str | None = None
    architecture: str | None = None
    executor: str | None = None
    shell: str | None = None
    features: dict = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)


class RegisterRunnerRequest(BaseModel):
    token: str | None = None
    description: str | None = None
    maintenance_note: str | None = None
    tag_list: str | None = None
    run_untagged: bool = True
    locked: bool = False
    access_level: str | None = None
    maximum_timeout: int | None = None
    paused: bool = False
    info: RunnerInfo | None = None


class VerifyRunnerRequest(BaseModel):
    token: str | None = None
    system_id: str | None = None


class JobRequest(BaseModel):
    token: str | None = None
    system_id: str | None = None
    last_update: str | None = None
    info: RunnerInfo | None = None
    session: dict | None = None


class JobUpdateRequest(BaseModel):
    token: str | None = None
    state: (
        Literal["pending", "running", "failed", "success", "skipped", "canceled"] | None
    ) = None
    failure_reason: str | None = None
    checksum: str | None = None
    output: dict | None = None
    exit_code: int | None = None
    info: RunnerInfo | None = None


def _runner_token_from_header(runner_token: str | None) -> str | None:
    return runner_token.strip() if runner_token else None


def _is_registration_token(token: str | None) -> bool:
    return bool(token and token == settings.RUNNER_REGISTRATION_TOKEN)


def _is_runner_token(token: str | None) -> bool:
    return bool(token and token == EMULATOR_RUNNER_TOKEN)


def _is_persisted_job_token(job: PipelineJob, token: str | None) -> bool:
    return bool(token and token == job.job_token)


def _tag_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [tag.strip() for tag in value.split(",") if tag.strip()]
    if isinstance(value, list):
        return [str(tag).strip() for tag in value if str(tag).strip()]
    return []


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _elapsed_seconds(since: datetime | None, now: datetime) -> int | None:
    aware = _aware_utc(since)
    if aware is None:
        return None
    return max(0, int((now - aware).total_seconds()))


def _iso_utc(value: datetime | None) -> str | None:
    aware = _aware_utc(value)
    if aware is None:
        return None
    return aware.isoformat().replace("+00:00", "Z")


def _runner_response(runner: CiRunner) -> dict:
    return {
        "id": runner.id,
        "token": runner.token,
        "token_expires_at": None,
    }


def _runner_json(runner: CiRunner, *, include_token: bool = False) -> dict:
    data = {
        "id": runner.id,
        "description": runner.description or "glemu-runner",
        "active": not runner.paused,
        "paused": bool(runner.paused),
        "is_shared": True,
        "runner_type": "instance_type",
        "name": runner.runner_name,
        "online": runner.last_contact_at is not None,
        "status": "online"
        if runner.last_contact_at is not None and not runner.paused
        else "offline",
        "tag_list": list(runner.tags or []),
        "run_untagged": bool(runner.run_untagged),
        "locked": bool(runner.locked),
        "version": runner.runner_version,
        "revision": runner.runner_revision,
        "platform": runner.runner_platform,
        "architecture": runner.runner_architecture,
        "executor": runner.runner_executor,
        "system_id": runner.system_id,
        "contacted_at": runner.last_contact_at,
        "last_contact_at": runner.last_contact_at,
        "last_poll_at": runner.last_poll_at,
        "last_verify_at": runner.last_verify_at,
        "last_job_id": runner.last_job_id,
        "created_at": runner.created_at,
        "updated_at": runner.updated_at,
    }
    if include_token:
        data["token"] = runner.token
    return data


def _runner_job_json(job: PipelineJob, runner: CiRunner) -> dict:
    duration = None
    queued_duration = None
    queued = _aware_utc(job.queued_at)
    started = _aware_utc(job.started_at)
    finished = _aware_utc(job.finished_at)
    if queued and started:
        queued_duration = max(0, int((started - queued).total_seconds()))
    if started and finished:
        duration = max(0, int((finished - started).total_seconds()))
    artifacts = [
        {
            "file_type": artifact.file_type,
            "file_format": artifact.file_format,
            "filename": artifact.filename,
            "size": artifact.size,
            "expire_at": _iso_utc(artifact.expire_at),
            "created_at": _iso_utc(artifact.created_at),
        }
        for artifact in job.artifacts
    ]
    return {
        "id": job.id,
        "name": job.name,
        "status": job.status,
        "stage": job.stage,
        "ref": job.pipeline.ref if job.pipeline else None,
        "environment": job.environment,
        "tag": False,
        "coverage": job.coverage,
        "allow_failure": bool(job.allow_failure),
        "created_at": _iso_utc(job.created_at),
        "scheduled_at": _iso_utc(job.scheduled_at),
        "started_at": _iso_utc(job.started_at),
        "finished_at": _iso_utc(job.finished_at),
        "duration": duration,
        "queued_duration": queued_duration,
        "user": None,
        "commit": {"id": job.pipeline.sha, "short_id": job.pipeline.sha[:8]}
        if job.pipeline
        else None,
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
        "project_id": job.project_id,
        "tag_list": job.tags or [],
        "artifacts": artifacts,
        "runner": {
            "id": runner.id,
            "description": runner.description,
            "runner_type": "instance_type",
        },
        "web_url": f"{settings.BASE_URL}/{job.project.full_name}/-/jobs/{job.id}"
        if job.project
        else None,
    }


async def _ensure_runner(db: DbSession, token: str = EMULATOR_RUNNER_TOKEN) -> CiRunner:
    result = await db.execute(select(CiRunner).where(CiRunner.token == token))
    runner = result.scalar_one_or_none()
    if runner is None:
        runner = CiRunner(
            token=token,
            description="glemu-runner",
            tags=[],
            run_untagged=True,
            paused=False,
            locked=False,
        )
        db.add(runner)
        await db.flush()
    return runner


async def _create_registered_runner(db: DbSession) -> CiRunner:
    existing = await db.execute(select(CiRunner.id).limit(1))
    if existing.first() is None:
        token = EMULATOR_RUNNER_TOKEN
    else:
        while True:
            token = f"glrt-{secrets.token_urlsafe(24)}"
            result = await db.execute(
                select(CiRunner.id).where(CiRunner.token == token)
            )
            if result.scalar_one_or_none() is None:
                break
    runner = CiRunner(
        token=token,
        description="glemu-runner",
        tags=[],
        run_untagged=True,
        paused=False,
        locked=False,
    )
    db.add(runner)
    await db.flush()
    return runner


async def _runner_for_token(db: DbSession, token: str | None) -> CiRunner | None:
    if not token:
        return None
    result = await db.execute(select(CiRunner).where(CiRunner.token == token))
    return result.scalar_one_or_none()


def _apply_runner_info(runner: CiRunner, info: RunnerInfo | None) -> None:
    if info is None:
        return
    runner.runner_name = info.name
    runner.runner_version = info.version
    runner.runner_revision = info.revision
    runner.runner_platform = info.platform
    runner.runner_architecture = info.architecture
    runner.runner_executor = info.executor


def _record_runner_contact(
    runner: CiRunner,
    *,
    event: str,
    info: RunnerInfo | None = None,
    job_id: int | None = None,
    system_id: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    runner.last_contact_at = now
    if event == "poll":
        runner.last_poll_at = now
    elif event == "verify":
        runner.last_verify_at = now
    if job_id is not None:
        runner.last_job_id = job_id
    if system_id is not None:
        runner.system_id = system_id
    _apply_runner_info(runner, info)


async def registered_runner_diagnostics(db: DbSession) -> dict:
    """Return persisted runner state for admin diagnostics."""
    result = await db.execute(
        select(CiRunner).order_by(
            CiRunner.last_contact_at.desc().nullslast(), CiRunner.id.asc()
        )
    )
    runner = result.scalars().first()
    if runner is None:
        return {
            "id": EMULATOR_RUNNER_ID,
            "token": EMULATOR_RUNNER_TOKEN,
            "description": "glemu-runner",
            "tags": [],
            "run_untagged": True,
            "paused": False,
            "last_contact_at": None,
            "last_poll_at": None,
            "last_verify_at": None,
            "last_job_id": None,
            "last_runner_name": None,
            "last_runner_version": None,
            "last_runner_executor": None,
        }
    return {
        "id": runner.id,
        "token": runner.token,
        "description": runner.description or "glemu-runner",
        "tags": list(runner.tags or []),
        "run_untagged": bool(runner.run_untagged),
        "paused": bool(runner.paused),
        "last_contact_at": runner.last_contact_at,
        "last_poll_at": runner.last_poll_at,
        "last_verify_at": runner.last_verify_at,
        "last_job_id": runner.last_job_id,
        "last_runner_name": runner.runner_name,
        "last_runner_version": runner.runner_version,
        "last_runner_executor": runner.runner_executor,
    }


def explain_job_scheduling(
    jobs: list[PipelineJob], runner: dict | CiRunner | None
) -> dict[int, dict]:
    """Explain why jobs are eligible, blocked, or waiting.

    This mirrors the runner coordinator's stage/needs/tag checks so admin and
    API diagnostics describe the same scheduling behavior.
    """
    jobs_by_name = {job.name: job for job in jobs}
    now = datetime.now(timezone.utc)
    if isinstance(runner, CiRunner):
        runner_tags = set(runner.tags or [])
        run_untagged = bool(runner.run_untagged)
        runner_last_contact_at = runner.last_contact_at
    else:
        runner_data = runner or {}
        runner_tags = set(runner_data.get("tags") or runner_data.get("tag_list") or [])
        run_untagged = bool(runner_data.get("run_untagged", True))
        runner_last_contact_at = runner_data.get("last_contact_at")

    diagnostics: dict[int, dict] = {}
    for job in jobs:
        reasons: list[str] = []
        blockers: list[dict] = []
        blocked = False
        eligible = job.status == "pending"

        if job.status == "scheduled":
            scheduled_at = _aware_utc(job.scheduled_at)
            blocked = True
            if scheduled_at is None:
                reason = "delayed job is missing scheduled_at; runner poll will promote it"
            elif scheduled_at <= now:
                reason = "delayed job is due; runner poll will promote it"
            else:
                reason = f"delayed until {_iso_utc(scheduled_at)}"
            reasons.append(reason)
            blockers.append(
                {
                    "type": "delayed",
                    "scheduled_at": _iso_utc(scheduled_at),
                    "reason": reason,
                }
            )
        elif job.status == "pending":
            job_runs_after_failure = _job_runs_after_failure(job)
            if _job_waits_for_failure(job) and not _job_has_required_failure_dependency(
                job
            ):
                blocked = True
                reason = "waiting for an earlier required failure"
                reasons.append(reason)
                blockers.append(
                    {"type": "when", "when": "on_failure", "reason": reason}
                )
            if job.needs is not None:
                for need in _need_items(job.needs):
                    peer = jobs_by_name.get(need["job"])
                    if peer is None:
                        if need["optional"]:
                            continue
                        blocked = True
                        reason = f"missing required need `{need['job']}`"
                        reasons.append(reason)
                        blockers.append(
                            {
                                "type": "missing_need",
                                "job": need["job"],
                                "reason": reason,
                            }
                        )
                    elif peer.status == "failed" and (
                        peer.allow_failure or job_runs_after_failure
                    ):
                        continue
                    elif peer.status not in {"success", "skipped", "manual"}:
                        blocked = True
                        reason = f"waiting for need `{peer.name}` ({peer.status})"
                        reasons.append(reason)
                        blockers.append(
                            {
                                "type": "need",
                                "job": peer.name,
                                "job_id": peer.id,
                                "status": peer.status,
                                "reason": reason,
                            }
                        )
            else:
                for peer in jobs:
                    if (
                        peer.status == "failed"
                        and (peer.allow_failure or job_runs_after_failure)
                        and peer.stage_index < job.stage_index
                    ):
                        continue
                    if peer.stage_index < job.stage_index and peer.status not in {
                        "success",
                        "skipped",
                        "manual",
                    }:
                        blocked = True
                        reason = f"waiting for earlier stage job `{peer.name}` ({peer.status})"
                        reasons.append(reason)
                        blockers.append(
                            {
                                "type": "stage",
                                "job": peer.name,
                                "job_id": peer.id,
                                "status": peer.status,
                                "reason": reason,
                            }
                        )

            job_tags = set(job.tags or [])
            if job_tags:
                missing = sorted(job_tags - runner_tags)
                if missing:
                    blocked = True
                    reason = f"runner missing tag(s): {', '.join(missing)}"
                    reasons.append(reason)
                    blockers.append(
                        {
                            "type": "runner_tags",
                            "missing_tags": missing,
                            "reason": reason,
                        }
                    )
            elif not run_untagged:
                blocked = True
                reason = "runner is not configured to run untagged jobs"
                reasons.append(reason)
                blockers.append({"type": "run_untagged", "reason": reason})

            resource_blocker = _resource_group_blocker(job)
            if resource_blocker is not None:
                blocked = True
                reason = (
                    f"resource group `{job.resource_group}` is held by running "
                    f"job `{resource_blocker.name}`"
                )
                reasons.append(reason)
                blockers.append(
                    {
                        "type": "resource_group",
                        "resource_group": job.resource_group,
                        "job": resource_blocker.name,
                        "job_id": resource_blocker.id,
                        "reason": reason,
                    }
                )

            if not blocked:
                reasons.append("eligible for the next runner poll")
        elif job.status == "running":
            running_seconds = _elapsed_seconds(job.started_at or job.updated_at, now)
            stale = running_seconds is not None and running_seconds >= int(
                RUNNING_JOB_STALE_AFTER.total_seconds()
            )
            if stale:
                blocked = True
                reason = (
                    "running longer than the emulator stale threshold; "
                    "operator requeue can reset the runner-facing attempt"
                )
                reasons.append(reason)
                blockers.append(
                    {
                        "type": "stale_running_job",
                        "reason": reason,
                        "running_seconds": running_seconds,
                        "stale_after_seconds": int(
                            RUNNING_JOB_STALE_AFTER.total_seconds()
                        ),
                    }
                )
            else:
                reasons.append(
                    "assigned to a runner; requeue if the runner died or the job is stale"
                )
        elif job.status == "manual":
            reasons.append("waiting for Play")
        elif job.status in {"failed", "canceled", "skipped", "success"}:
            reasons.append("terminal; Retry creates a new pending attempt")

        diagnostics[job.id] = {
            "job_id": job.id,
            "job_name": job.name,
            "status": job.status,
            "eligible": eligible and not blocked,
            "blocked": blocked,
            "reasons": reasons,
            "blockers": blockers,
            "stale": bool(job.status == "running" and blocked),
            "stale_after_seconds": int(RUNNING_JOB_STALE_AFTER.total_seconds())
            if job.status == "running"
            else None,
            "running_seconds": _elapsed_seconds(job.started_at or job.updated_at, now)
            if job.status == "running"
            else None,
            "runner_contact_age_seconds": _elapsed_seconds(runner_last_contact_at, now)
            if job.status == "running"
            else None,
            "recovery": {
                "operator_requeue": job.status in {"pending", "running", "scheduled"},
                "gitlab_compatible_flow": "cancel_then_retry"
                if job.status == "running"
                else None,
            },
        }
    return diagnostics


def _last_update_headers() -> dict[str, str]:
    return {
        "X-GitLab-Last-Update": datetime.now(timezone.utc).isoformat(),
    }


def _persisted_remote_job_headers(job: PipelineJob) -> dict[str, str]:
    trace_size = job.trace_size or 0
    return {
        "Job-Status": job.status,
        "X-GitLab-Trace-Update-Interval": "1",
        "Range": f"0-{trace_size - 1}" if trace_size else "0-0",
    }


def _variable_payload_item(key: str, value: object) -> dict:
    if isinstance(value, dict):
        masked = bool(value.get("masked", False))
        return {
            "key": key,
            "value": str(value.get("value", "")),
            "public": bool(value.get("public", not masked)),
            "file": bool(value.get("file", False)),
            "masked": masked,
            "raw": bool(value.get("raw", False)),
        }
    return {
        "key": key,
        "value": str(value),
        "public": True,
        "file": False,
        "masked": False,
        "raw": False,
    }


def _variables_from_dict(values: dict[str, object]) -> list[dict]:
    return [_variable_payload_item(key, value) for key, value in values.items()]


def _service_payload(services: list[dict] | None) -> list[dict]:
    payload: list[dict] = []
    for service in services or []:
        if not isinstance(service, dict) or not service.get("name"):
            continue
        item = {"name": str(service["name"])}
        if service.get("alias"):
            item["alias"] = str(service["alias"])
        if service.get("command"):
            item["command"] = [str(entry) for entry in service.get("command", [])]
        if service.get("entrypoint"):
            item["entrypoint"] = [
                str(entry) for entry in service.get("entrypoint", [])
            ]
        if service.get("pull_policy"):
            item["pull_policy"] = [
                str(entry) for entry in service.get("pull_policy", [])
            ]
        if service.get("variables"):
            item["variables"] = service.get("variables")
        payload.append(item)
    return payload


def _image_payload(job: PipelineJob) -> dict:
    payload = {"name": job.image}
    config = job.image_config or {}
    if config.get("entrypoint"):
        payload["entrypoint"] = [str(entry) for entry in config.get("entrypoint", [])]
    if config.get("pull_policy"):
        payload["pull_policy"] = [
            str(entry) for entry in config.get("pull_policy", [])
        ]
    return payload


def _redact_trace_text(text: str, job: PipelineJob) -> str:
    return redact_trace_text(text, job.variables or {})


def _coverage_pattern(raw_pattern: str) -> tuple[str, int]:
    pattern = raw_pattern.strip()
    flags = 0
    if len(pattern) >= 2 and pattern.startswith("/"):
        last_slash = pattern.rfind("/")
        if last_slash > 0:
            raw_flags = pattern[last_slash + 1 :]
            unsupported = set(raw_flags) - {"i", "m", "s", "x"}
            if unsupported:
                return pattern, flags
            pattern_body = pattern[1:last_slash]
            flags_by_name = {
                "i": re.IGNORECASE,
                "m": re.MULTILINE,
                "s": re.DOTALL,
                "x": re.VERBOSE,
            }
            for flag in raw_flags:
                flags |= flags_by_name[flag]
            pattern = pattern_body
    return pattern, flags


def _extract_coverage_from_trace(job: PipelineJob) -> str | None:
    if not job.coverage_regex or not job.trace or not job.trace.content:
        return None
    pattern, flags = _coverage_pattern(job.coverage_regex)
    try:
        match = re.search(pattern, job.trace.content, flags)
    except re.error:
        return None
    if match is None:
        return None
    candidate = match.group(1) if match.groups() else match.group(0)
    number = re.search(r"\d+(?:\.\d+)?", candidate)
    return number.group(0) if number else None


def _refresh_job_coverage(job: PipelineJob) -> None:
    coverage = _extract_coverage_from_trace(job)
    if coverage is not None:
        job.coverage = coverage


def _retry_when_matches(configured: list[str], failure_reason: str | None) -> bool:
    if not configured:
        return True
    reason = failure_reason or "script_failure"
    return "always" in configured or reason in configured


def _should_auto_retry(job: PipelineJob, failure_reason: str | None) -> bool:
    config = job.retry_config or {}
    try:
        max_attempts = int(config.get("max") or 0)
    except (TypeError, ValueError):
        return False
    if max_attempts <= 0:
        return False
    if (job.retry_attempt or 0) >= max_attempts:
        return False
    configured_when = config.get("when") or []
    if isinstance(configured_when, str):
        configured_when = [configured_when]
    return _retry_when_matches([str(item) for item in configured_when], failure_reason)


def _prepare_auto_retry(job: PipelineJob) -> None:
    job.retry_attempt = (job.retry_attempt or 0) + 1
    now = datetime.now(timezone.utc)
    job.status = "scheduled" if (job.when or "on_success") == "delayed" else "pending"
    job.job_token = f"gljt-persisted-{secrets.token_urlsafe(24)}"
    job.runner_name = None
    job.failure_reason = None
    job.exit_code = None
    job.trace_checksum = None
    job.trace_size = 0
    job.coverage = None
    job.queued_at = now
    job.scheduled_at = now.replace(tzinfo=None) if job.status == "scheduled" else None
    job.started_at = None
    job.finished_at = None
    if job.trace:
        job.trace.content = ""
        job.trace.size = 0


def _repo_url_with_job_token(repo_url: str, job_token: str) -> str:
    parts = urlsplit(repo_url)
    credentials = f"gitlab-ci-token:{quote(job_token, safe='')}"
    return urlunsplit(
        (
            parts.scheme,
            f"{credentials}@{parts.netloc}",
            parts.path,
            parts.query,
            parts.fragment,
        )
    )


def _artifact_payload(job: PipelineJob) -> list[dict]:
    config = job.artifacts_config or {}
    paths = config.get("paths") or job.artifacts_paths or []
    if not paths and not config.get("untracked"):
        return []
    return [
        {
            "name": str(config.get("name") or "artifacts"),
            "untracked": bool(config.get("untracked", False)),
            "paths": paths,
            "exclude": [str(path) for path in config.get("exclude", [])],
            "when": str(config.get("when") or "on_success"),
            "artifact_type": str(config.get("artifact_type") or "archive"),
            "artifact_format": str(config.get("artifact_format") or "zip"),
            "expire_in": str(config.get("expire_in") or ""),
        }
    ]


def _cache_payload(entries: list[dict] | None) -> list[dict]:
    if not entries:
        return []
    return [
        {
            "key": str(entry.get("key") or "default"),
            "untracked": bool(entry.get("untracked", False)),
            "unprotect": bool(entry.get("unprotect", False)),
            "policy": str(entry.get("policy") or "pull-push"),
            "paths": [str(path) for path in entry.get("paths", [])],
            "when": str(entry.get("when") or "on_success"),
            "fallback_keys": [str(key) for key in entry.get("fallback_keys", [])],
        }
        for entry in entries
    ]


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


def _dependencies_payload(job: PipelineJob) -> list[dict]:
    if job.dependencies is not None:
        dependency_names = [str(name) for name in job.dependencies]
    elif job.needs is not None:
        dependency_names = [
            need["job"] for need in _need_items(job.needs) if need.get("artifacts", True)
        ]
    else:
        dependency_names = [
            peer.name
            for peer in sorted(
                job.pipeline.jobs,
                key=lambda peer: (peer.stage_index, peer.id),
            )
            if peer.stage_index < job.stage_index
        ]
    if not dependency_names:
        return []
    peers_by_name = {peer.name: peer for peer in job.pipeline.jobs}
    dependencies: list[dict] = []
    for needed_name in dependency_names:
        peer = peers_by_name.get(needed_name)
        if peer is None or not peer.artifacts:
            continue
        artifact = peer.artifacts[0]
        if _artifact_is_expired(artifact):
            continue
        if not artifact.filename:
            continue
        dependencies.append(
            {
                "id": peer.id,
                "token": peer.job_token,
                "name": peer.name,
                "artifacts_file": {
                    "filename": artifact.filename,
                    "size": artifact.size or 0,
                },
            }
        )
    return dependencies


def _artifact_expire_at(expire_in: str | None) -> datetime | None:
    if not expire_in:
        return None
    value = expire_in.strip().lower()
    if not value or value == "never":
        return None
    match = re.fullmatch(r"(\d+)\s*([a-z]+)", value)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    seconds_by_unit = {
        "second": 1,
        "seconds": 1,
        "sec": 1,
        "secs": 1,
        "s": 1,
        "minute": 60,
        "minutes": 60,
        "min": 60,
        "mins": 60,
        "m": 60,
        "hour": 3600,
        "hours": 3600,
        "hr": 3600,
        "hrs": 3600,
        "h": 3600,
        "day": 86400,
        "days": 86400,
        "d": 86400,
        "week": 604800,
        "weeks": 604800,
        "w": 604800,
    }
    seconds = seconds_by_unit.get(unit)
    if seconds is None:
        return None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now + timedelta(seconds=amount * seconds)


def _artifact_is_expired(artifact: JobArtifact) -> bool:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return bool(artifact.expire_at and artifact.expire_at <= now)


def _build_persisted_job_payload(job: PipelineJob) -> dict:
    pipeline = job.pipeline
    project = job.project
    base_url = settings.BASE_URL.rstrip("/")
    repo_url = f"{base_url}/{project.full_name}.git"
    authenticated_repo_url = _repo_url_with_job_token(repo_url, job.job_token)
    protected = False
    timeout_seconds = job.timeout_seconds or 3600
    variables = {
        "CI": "true",
        "GITLAB_CI": "true",
        "CI_API_V4_URL": f"{base_url}/api/v4",
        "CI_SERVER_URL": base_url,
        "CI_REPOSITORY_URL": authenticated_repo_url,
        "CI_COMMIT_SHA": pipeline.sha,
        "CI_COMMIT_REF_NAME": pipeline.ref,
        "CI_DEFAULT_BRANCH": project.default_branch or "main",
        "CI_PROJECT_ID": str(project.id),
        "CI_PROJECT_NAME": project.name,
        "CI_PROJECT_PATH": project.full_name,
        "CI_JOB_ID": str(job.id),
        "CI_JOB_NAME": job.name,
        "CI_JOB_STAGE": job.stage,
        "CI_JOB_TOKEN": job.job_token,
        "GIT_STRATEGY": "fetch",
        **(job.variables or {}),
    }
    if job.environment:
        variables["CI_ENVIRONMENT_NAME"] = job.environment
    return {
        "id": job.id,
        "token": job.job_token,
        "allow_git_fetch": True,
        "job_info": {
            "name": job.name,
            "stage": job.stage,
            "pipeline_id": pipeline.id,
            "project_id": project.id,
            "project_name": project.name,
            "project_full_path": project.full_name,
            "namespace_id": project.owner_id,
            "root_namespace_id": project.owner_id,
            "organization_id": 1,
            "instance_id": "gitlab-emulator",
            "instance_uuid": "gitlab-emulator",
            "user_id": project.owner_id,
            "time_in_queue_seconds": 0,
            "project_jobs_running_on_instance_runners_count": "0",
            "queue_size": 0,
            "queue_depth": 0,
        },
        "git_info": {
            "repo_url": authenticated_repo_url,
            "repo_object_format": "sha1",
            "ref": pipeline.ref,
            "sha": pipeline.sha,
            "before_sha": pipeline.before_sha
            or "0000000000000000000000000000000000000000",
            "ref_type": "branch",
            "refspecs": [
                f"+refs/heads/{pipeline.ref}:refs/remotes/origin/{pipeline.ref}"
            ],
            "depth": 0,
            "protected": protected,
        },
        "runner_info": {"timeout": timeout_seconds},
        "inputs": [],
        "variables": _variables_from_dict(variables),
        "steps": [
            {
                "name": "script",
                "script": job.script or [],
                "timeout": timeout_seconds,
                "when": "on_success"
                if (job.when or "on_success") == "delayed"
                else job.when or "on_success",
                "allow_failure": bool(job.allow_failure),
            }
        ],
        "image": _image_payload(job),
        "services": _service_payload(job.services),
        "artifacts": _artifact_payload(job),
        "cache": _cache_payload(job.cache),
        "credentials": [],
        "dependencies": _dependencies_payload(job),
        "features": {
            "trace_sections": True,
            "token_mask_prefixes": ["gljt-"],
            "failure_reasons": [
                "script_failure",
                "runner_system_failure",
                "job_execution_timeout",
            ],
            "tracing": None,
        },
        "secrets": {},
        "hooks": [],
        "policy_options": {"execution_policy_job": False, "policy_name": ""},
        "suspend_options": {},
    }


async def _derive_pipeline_status(pipeline: Pipeline, db: DbSession) -> None:
    await db.refresh(pipeline, attribute_names=["jobs"])
    statuses = [job.status for job in pipeline.jobs]
    blocking_statuses = [
        job.status
        for job in pipeline.jobs
        if not (job.status == "failed" and job.allow_failure)
    ]
    now = datetime.now(timezone.utc)
    if not statuses:
        pipeline.status = "pending"
    elif not blocking_statuses:
        pipeline.status = "success"
        pipeline.finished_at = pipeline.finished_at or now
    elif all(job_status == "canceled" for job_status in blocking_statuses):
        pipeline.status = "canceled"
        pipeline.finished_at = pipeline.finished_at or now
    elif any(job_status == "canceled" for job_status in blocking_statuses) and not any(
        job_status in {"pending", "running", "scheduled"}
        for job_status in blocking_statuses
    ):
        pipeline.status = "canceled"
        pipeline.finished_at = pipeline.finished_at or now
    elif any(job_status == "running" for job_status in blocking_statuses):
        pipeline.status = "running"
        pipeline.started_at = pipeline.started_at or now
        pipeline.finished_at = None
    elif any(job_status in {"pending", "scheduled"} for job_status in blocking_statuses):
        pipeline.status = "pending"
        pipeline.finished_at = None
    elif any(job_status == "failed" for job_status in blocking_statuses):
        pipeline.status = "failed"
        pipeline.finished_at = pipeline.finished_at or now
    elif all(
        job_status in {"success", "skipped", "manual", "failed"}
        for job_status in blocking_statuses
    ):
        pipeline.status = "success"
        pipeline.finished_at = pipeline.finished_at or now
    else:
        pipeline.status = "pending"


TERMINAL_DEPENDENCY_STATUSES = {"success", "skipped", "manual", "failed", "canceled"}


def _job_waits_for_failure(job: PipelineJob) -> bool:
    return (job.when or "on_success") == "on_failure"


def _job_runs_after_failure(job: PipelineJob) -> bool:
    return (job.when or "on_success") in {"always", "on_failure"}


def _job_has_required_failure_dependency(job: PipelineJob) -> bool:
    if job.needs is not None:
        peers = {peer.name: peer for peer in job.pipeline.jobs}
        return any(
            (peer := peers.get(need["job"])) is not None
            and peer.status == "failed"
            and not peer.allow_failure
            for need in _need_items(job.needs)
        )
    return any(
        peer.stage_index < job.stage_index
        and peer.status == "failed"
        and not peer.allow_failure
        for peer in job.pipeline.jobs
    )


def _job_dependencies_are_terminal(job: PipelineJob) -> bool:
    if job.needs is not None:
        peers = {peer.name: peer for peer in job.pipeline.jobs}
        for need in _need_items(job.needs):
            peer = peers.get(need["job"])
            if peer is None:
                if need["optional"]:
                    continue
                return False
            if peer.status not in TERMINAL_DEPENDENCY_STATUSES:
                return False
        return True
    return all(
        peer.status in TERMINAL_DEPENDENCY_STATUSES
        for peer in job.pipeline.jobs
        if peer.stage_index < job.stage_index
    )


def _job_stage_is_unblocked(job: PipelineJob) -> bool:
    if _job_waits_for_failure(job) and not _job_has_required_failure_dependency(job):
        return False
    job_runs_after_failure = _job_runs_after_failure(job)
    if job.needs is not None:
        peers = {peer.name: peer for peer in job.pipeline.jobs}
        for need in _need_items(job.needs):
            peer = peers.get(need["job"])
            if peer is None:
                if need["optional"]:
                    continue
                return False
            status = peer.status
            if status == "failed" and (peer.allow_failure or job_runs_after_failure):
                continue
            if status not in {"success", "skipped", "manual"}:
                return False
        return True
    return all(
        peer.status in {"success", "skipped", "manual"}
        or (peer.status == "failed" and (peer.allow_failure or job_runs_after_failure))
        for peer in job.pipeline.jobs
        if peer.stage_index < job.stage_index
    )


def _runner_tags_from_request(body: JobRequest, runner: CiRunner) -> list[str]:
    if body.info:
        for key in ("tag_list", "tags"):
            tags = _tag_list(body.info.config.get(key))
            if tags:
                return tags
    return list(runner.tags or [])


def _runner_run_untagged_from_request(body: JobRequest, runner: CiRunner) -> bool:
    if body.info and "run_untagged" in body.info.config:
        return bool(body.info.config["run_untagged"])
    return bool(runner.run_untagged)


def _runner_can_run_job(job: PipelineJob, body: JobRequest, runner: CiRunner) -> bool:
    job_tags = set(job.tags or [])
    if not job_tags:
        return _runner_run_untagged_from_request(body, runner)
    runner_tags = set(_runner_tags_from_request(body, runner))
    return job_tags.issubset(runner_tags)


def _resource_group_blocker(job: PipelineJob) -> PipelineJob | None:
    resource_group = job.resource_group
    if not resource_group or not job.pipeline:
        return None
    for peer in job.pipeline.jobs:
        if (
            peer.id != job.id
            and peer.project_id == job.project_id
            and peer.resource_group == resource_group
            and peer.status == "running"
        ):
            return peer
    return None


def _skip_jobs_after_failed_stage(pipeline: Pipeline) -> None:
    failed_stage_indexes = [
        job.stage_index
        for job in pipeline.jobs
        if job.status == "failed" and not job.allow_failure
    ]
    failed_job_names = {
        job.name
        for job in pipeline.jobs
        if job.status == "failed" and not job.allow_failure
    }
    if not failed_stage_indexes:
        return
    first_failed_stage = min(failed_stage_indexes)
    now = datetime.now(timezone.utc)
    for job in pipeline.jobs:
        needed_names = {need["job"] for need in _need_items(job.needs)}
        needs_failed_job = bool(needed_names & failed_job_names)
        job_runs_after_failure = _job_runs_after_failure(job)
        if (
            job.status in {"pending", "scheduled"}
            and (job.stage_index > first_failed_stage or needs_failed_job)
            and not job_runs_after_failure
        ):
            job.status = "skipped"
            job.finished_at = job.finished_at or now


def _skip_on_failure_jobs_without_failure(pipeline: Pipeline) -> None:
    now = datetime.now(timezone.utc)
    for job in pipeline.jobs:
        if (
            job.status in {"pending", "scheduled"}
            and _job_waits_for_failure(job)
            and _job_dependencies_are_terminal(job)
            and not _job_has_required_failure_dependency(job)
        ):
            job.status = "skipped"
            job.finished_at = job.finished_at or now


@router.get("/runners")
async def list_runners(db: DbSession):
    """List persisted runners for emulator/operator inspection."""
    result = await db.execute(select(CiRunner).order_by(CiRunner.id.asc()))
    return [_runner_json(runner) for runner in result.scalars().all()]


@router.get("/runners/{runner_id}")
async def get_runner(runner_id: int, db: DbSession):
    """Return persisted runner details."""
    result = await db.execute(select(CiRunner).where(CiRunner.id == runner_id))
    runner = result.scalar_one_or_none()
    if runner is None:
        raise HTTPException(status_code=404, detail="Runner Not Found")
    return _runner_json(runner, include_token=True)


@router.get("/runners/{runner_id}/jobs")
async def list_runner_jobs(runner_id: int, db: DbSession):
    """Return recent jobs associated with a persisted runner."""
    result = await db.execute(select(CiRunner).where(CiRunner.id == runner_id))
    runner = result.scalar_one_or_none()
    if runner is None:
        raise HTTPException(status_code=404, detail="Runner Not Found")
    runner_names = {name for name in [runner.runner_name, runner.description] if name}
    if not runner_names:
        return []
    jobs_result = await db.execute(
        select(PipelineJob)
        .options(
            selectinload(PipelineJob.pipeline),
            selectinload(PipelineJob.project),
            selectinload(PipelineJob.artifacts),
        )
        .where(PipelineJob.runner_name.in_(runner_names))
        .order_by(PipelineJob.id.desc())
        .limit(50)
    )
    return [_runner_job_json(job, runner) for job in jobs_result.scalars().all()]


@router.post("/runners", status_code=status.HTTP_201_CREATED)
async def register_runner(
    body: RegisterRunnerRequest,
    db: DbSession,
    runner_token: str | None = Header(default=None, alias="RUNNER-TOKEN"),
):
    """Register a runner with the emulator registration token."""
    token = body.token or _runner_token_from_header(runner_token)
    if not token:
        raise HTTPException(status_code=403, detail="Forbidden")
    if _is_registration_token(token):
        runner = await _create_registered_runner(db)
    else:
        runner = await _runner_for_token(db, token)
        if runner is None and _is_runner_token(token):
            runner = await _ensure_runner(db, token)
        if runner is None:
            raise HTTPException(status_code=403, detail="Forbidden")
    runner.tags = _tag_list(body.tag_list)
    runner.run_untagged = body.run_untagged
    runner.description = body.description or "glemu-runner"
    runner.paused = body.paused
    runner.locked = body.locked
    _record_runner_contact(runner, event="register", info=body.info)
    await db.commit()
    await db.refresh(runner)
    return _runner_response(runner)


@router.post("/runners/verify")
async def verify_runner(
    body: VerifyRunnerRequest,
    db: DbSession,
    runner_token: str | None = Header(default=None, alias="RUNNER-TOKEN"),
):
    """Verify a persisted emulator runner token."""
    token = body.token or _runner_token_from_header(runner_token)
    if not token:
        raise HTTPException(status_code=403, detail="Forbidden")
    runner = await _runner_for_token(db, token)
    if runner is None and (_is_runner_token(token) or _is_registration_token(token)):
        runner = await _ensure_runner(db)
    if runner is None:
        raise HTTPException(status_code=403, detail="Forbidden")
    _record_runner_contact(runner, event="verify", system_id=body.system_id)
    await db.commit()
    await db.refresh(runner)
    return _runner_response(runner)


@router.delete("/runners", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_runner(
    body: VerifyRunnerRequest,
    db: DbSession,
    runner_token: str | None = Header(default=None, alias="RUNNER-TOKEN"),
):
    """Accept runner unregister requests for validation workflows."""
    token = body.token or _runner_token_from_header(runner_token)
    runner = await _runner_for_token(db, token)
    if runner is None:
        if not _is_runner_token(token):
            raise HTTPException(status_code=403, detail="Forbidden")
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    await db.delete(runner)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/jobs/request", status_code=status.HTTP_204_NO_CONTENT)
async def request_job(
    body: JobRequest,
    db: DbSession,
    runner_token: str | None = Header(default=None, alias="RUNNER-TOKEN"),
):
    """Let official gitlab-runner poll successfully when no jobs are queued."""
    token = body.token or _runner_token_from_header(runner_token)
    runner = await _runner_for_token(db, token)
    if runner is None and _is_runner_token(token):
        runner = await _ensure_runner(db, token)
    if runner is None:
        raise HTTPException(status_code=403, detail="Forbidden")
    _record_runner_contact(
        runner, event="poll", info=body.info, system_id=body.system_id
    )
    if runner.paused:
        await db.commit()
        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers=_last_update_headers(),
        )

    await promote_due_delayed_jobs(db)
    result = await db.execute(
        select(PipelineJob)
        .options(
            selectinload(PipelineJob.pipeline).selectinload(Pipeline.jobs),
            selectinload(PipelineJob.pipeline)
            .selectinload(Pipeline.jobs)
            .selectinload(PipelineJob.artifacts),
            selectinload(PipelineJob.project),
        )
        .where(PipelineJob.status == "pending")
        .order_by(PipelineJob.stage_index.asc(), PipelineJob.id.asc())
    )
    persisted_job = None
    for candidate in result.scalars().all():
        if _job_stage_is_unblocked(candidate) and _runner_can_run_job(
            candidate, body, runner
        ) and _resource_group_blocker(candidate) is None:
            persisted_job = candidate
            break
    if persisted_job is not None:
        persisted_job.status = "running"
        persisted_job.runner_name = (
            body.info.name if body.info and body.info.name else runner.description
        )
        persisted_job.started_at = datetime.now(timezone.utc)
        persisted_job.pipeline.status = "running"
        persisted_job.pipeline.started_at = (
            persisted_job.pipeline.started_at or persisted_job.started_at
        )
        _record_runner_contact(
            runner,
            event="poll",
            info=body.info,
            job_id=persisted_job.id,
            system_id=body.system_id,
        )
        await db.commit()
        import json

        return Response(
            content=json.dumps(_build_persisted_job_payload(persisted_job)),
            media_type="application/json",
            status_code=status.HTTP_201_CREATED,
            headers=_last_update_headers(),
        )

    await db.commit()
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers=_last_update_headers(),
    )


@router.patch("/jobs/{job_id}/trace", status_code=status.HTTP_202_ACCEPTED)
async def patch_job_trace(
    job_id: int,
    request: Request,
    db: DbSession,
    job_token: str | None = Header(default=None, alias="JOB-TOKEN"),
    content_range: str | None = Header(default=None, alias="Content-Range"),
):
    """Append trace bytes from an official runner."""
    result = await db.execute(
        select(PipelineJob)
        .options(
            selectinload(PipelineJob.pipeline).selectinload(Pipeline.jobs),
            selectinload(PipelineJob.trace),
        )
        .where(PipelineJob.id == job_id)
    )
    persisted_job = result.scalar_one_or_none()
    if persisted_job is not None:
        if not _is_persisted_job_token(persisted_job, job_token):
            raise HTTPException(status_code=403, detail="Forbidden")
        trace = persisted_job.trace
        if trace is None:
            trace = JobTrace(job_id=persisted_job.id, content="", size=0)
            db.add(trace)
            await db.flush()
        current = (trace.content or "").encode()
        start = len(current)
        if content_range:
            try:
                range_start = int(content_range.split("-", 1)[0])
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail="Invalid Content-Range"
                ) from exc
            if range_start != start:
                return Response(
                    status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                    headers=_persisted_remote_job_headers(persisted_job),
                )
        incoming = await request.body()
        trace.content = _redact_trace_text(
            (current + incoming).decode(errors="replace"),
            persisted_job,
        )
        trace.size = len(trace.content.encode())
        persisted_job.trace_size = trace.size
        _refresh_job_coverage(persisted_job)
        await db.commit()
        await db.refresh(persisted_job)
        return Response(
            status_code=status.HTTP_202_ACCEPTED,
            headers=_persisted_remote_job_headers(persisted_job),
        )

    raise HTTPException(status_code=404, detail="Job Not Found")


@router.put("/jobs/{job_id}")
async def update_job(
    job_id: int,
    body: JobUpdateRequest,
    db: DbSession,
    job_token: str | None = Header(default=None, alias="JOB-TOKEN"),
):
    """Accept final and intermediate job state updates from a runner."""
    result = await db.execute(
        select(PipelineJob)
        .options(
            selectinload(PipelineJob.pipeline).selectinload(Pipeline.jobs),
            selectinload(PipelineJob.trace),
        )
        .where(PipelineJob.id == job_id)
    )
    persisted_job = result.scalar_one_or_none()
    if persisted_job is not None:
        token = body.token or job_token
        if not _is_persisted_job_token(persisted_job, token):
            raise HTTPException(status_code=403, detail="Forbidden")
        if body.state:
            persisted_job.status = body.state
        _skip_on_failure_jobs_without_failure(persisted_job.pipeline)
        persisted_job.failure_reason = body.failure_reason
        persisted_job.exit_code = body.exit_code
        if body.output:
            persisted_job.trace_checksum = body.output.get("checksum")
            persisted_job.trace_size = (
                body.output.get("bytesize") or persisted_job.trace_size
            )
        auto_retry = (
            persisted_job.status == "failed"
            and _should_auto_retry(persisted_job, body.failure_reason)
        )
        if auto_retry:
            _prepare_auto_retry(persisted_job)
        elif persisted_job.status == "failed":
            _skip_jobs_after_failed_stage(persisted_job.pipeline)
        if persisted_job.status in {"success", "failed"}:
            _refresh_job_coverage(persisted_job)
            persisted_job.finished_at = datetime.now(timezone.utc)
        await _derive_pipeline_status(persisted_job.pipeline, db)
        await db.commit()
        await db.refresh(persisted_job)
        return Response(
            status_code=status.HTTP_200_OK,
            headers=_persisted_remote_job_headers(persisted_job),
        )

    raise HTTPException(status_code=404, detail="Job Not Found")


@router.post("/jobs/{job_id}/artifacts", status_code=status.HTTP_201_CREATED)
async def upload_job_artifacts(
    job_id: int,
    request: Request,
    db: DbSession,
    job_token: str | None = Header(default=None, alias="JOB-TOKEN"),
    artifact_format: str | None = None,
    artifact_type: str | None = None,
):
    """Accept artifact uploads from official runners."""
    result = await db.execute(select(PipelineJob).where(PipelineJob.id == job_id))
    persisted_job = result.scalar_one_or_none()
    if persisted_job is not None:
        if not _is_persisted_job_token(persisted_job, job_token):
            raise HTTPException(status_code=403, detail="Forbidden")
        body = await request.body()
        artifact_dir = os.path.join(
            settings.DATA_DIR, "artifacts", str(persisted_job.id)
        )
        os.makedirs(artifact_dir, exist_ok=True)
        filename = f"job-{persisted_job.id}-artifacts.{artifact_format or 'zip'}"
        storage_path = os.path.join(artifact_dir, filename)
        with open(storage_path, "wb") as artifact_file:
            artifact_file.write(body)
        db.add(
            JobArtifact(
                job_id=persisted_job.id,
                filename=filename,
                content_type=request.headers.get("Content-Type"),
                file_type=artifact_type or "archive",
                file_format=artifact_format or "zip",
                size=len(body),
                storage_path=storage_path,
                expire_at=_artifact_expire_at(
                    (persisted_job.artifacts_config or {}).get("expire_in")
                ),
            )
        )
        await db.commit()
        return {"message": "201 Created"}

    raise HTTPException(status_code=404, detail="Job Not Found")


@router.get("/jobs/{job_id}/artifacts")
async def download_job_artifacts(
    job_id: int,
    db: DbSession,
    job_token: str | None = Header(default=None, alias="JOB-TOKEN"),
):
    """Serve artifact downloads requested by official runner dependencies."""
    result = await db.execute(
        select(PipelineJob)
        .options(selectinload(PipelineJob.artifacts))
        .where(PipelineJob.id == job_id)
    )
    persisted_job = result.scalar_one_or_none()
    if persisted_job is None:
        raise HTTPException(status_code=404, detail="Job Not Found")
    if not _is_persisted_job_token(persisted_job, job_token):
        raise HTTPException(status_code=403, detail="Forbidden")
    artifact = persisted_job.artifacts[0] if persisted_job.artifacts else None
    if (
        artifact is None
        or not artifact.storage_path
        or not os.path.isfile(artifact.storage_path)
    ):
        raise HTTPException(status_code=404, detail="Artifacts Not Found")
    if _artifact_is_expired(artifact):
        raise HTTPException(status_code=404, detail="Artifacts Expired")
    return FileResponse(
        artifact.storage_path,
        media_type=artifact.content_type or "application/zip",
        filename=artifact.filename,
    )


def _cache_storage_path(project_id: int, cache_key: str) -> str:
    import hashlib

    digest = hashlib.sha256(cache_key.encode()).hexdigest()
    return os.path.join(settings.DATA_DIR, "caches", str(project_id), f"{digest}.zip")


def _cache_keys_with_fallbacks(cache_key: str, fallback_keys: str | None) -> list[str]:
    keys = [cache_key]
    if fallback_keys:
        keys.extend(key.strip() for key in fallback_keys.split(",") if key.strip())
    return keys


def _find_cache_archive(
    project_id: int, cache_key: str, fallback_keys: str | None
) -> tuple[str, str] | None:
    for candidate_key in _cache_keys_with_fallbacks(cache_key, fallback_keys):
        storage_path = _cache_storage_path(project_id, candidate_key)
        if os.path.exists(storage_path):
            return candidate_key, storage_path
    return None


@router.head("/projects/{project_id}/cache/{cache_key:path}")
async def head_project_cache(
    project_id: int,
    cache_key: str,
    fallback_keys: str | None = Query(default=None),
):
    """Return metadata for a stored cache archive."""
    found = _find_cache_archive(project_id, cache_key, fallback_keys)
    if found is None:
        raise HTTPException(status_code=404, detail="Cache Not Found")
    resolved_key, storage_path = found
    return Response(
        status_code=status.HTTP_200_OK,
        headers={
            "Content-Length": str(os.path.getsize(storage_path)),
            "Content-Type": "application/zip",
            "X-GitLab-Cache-Key": resolved_key,
        },
    )


@router.get("/projects/{project_id}/cache/{cache_key:path}")
async def download_project_cache(
    project_id: int,
    cache_key: str,
    fallback_keys: str | None = Query(default=None),
):
    """Download a stored cache archive."""
    found = _find_cache_archive(project_id, cache_key, fallback_keys)
    if found is None:
        raise HTTPException(status_code=404, detail="Cache Not Found")
    resolved_key, storage_path = found
    return FileResponse(
        storage_path,
        media_type="application/zip",
        filename="cache.zip",
        headers={"X-GitLab-Cache-Key": resolved_key},
    )


@router.put(
    "/projects/{project_id}/cache/{cache_key:path}",
    status_code=status.HTTP_201_CREATED,
)
async def upload_project_cache(project_id: int, cache_key: str, request: Request):
    """Store a cache archive for a project/cache key."""
    storage_path = _cache_storage_path(project_id, cache_key)
    os.makedirs(os.path.dirname(storage_path), exist_ok=True)
    with open(storage_path, "wb") as cache_file:
        cache_file.write(await request.body())
    return {"message": "201 Created", "key": cache_key}
