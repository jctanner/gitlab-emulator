"""Minimal GitLab pipeline and job APIs."""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import json
import mimetypes
import os
import secrets
import ssl
import zipfile
from datetime import datetime, timedelta, timezone
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
from app.services.ci_yaml import (
    ParsedCiJob,
    parse_gitlab_ci,
    parse_gitlab_ci_workflow_name,
)
from app.services.permissions import (
    DEVELOPER,
    MAINTAINER,
    REPORTER,
    pipeline_variables_allowed_for_access_level,
    project_access_level,
    require_project_access,
)
from app.services.pipeline_schedules import (
    http_cron_error,
    play_pipeline_schedule as materialize_pipeline_schedule,
    set_schedule_next_run,
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
    script: list[str] = Field(
        default_factory=lambda: ["echo hello from persisted pipeline"]
    )
    variables: dict[str, str] = Field(default_factory=dict)
    needs: list[str | dict] | None = None
    dependencies: list[str] | None = None
    tags: list[str] = Field(default_factory=list)
    cache: list[dict] = Field(default_factory=list)
    artifacts_paths: list[str] = Field(default_factory=list)
    artifacts: dict = Field(default_factory=dict)
    allow_failure: bool = False


class CreatePipelineRequest(BaseModel):
    ref: str = "main"
    sha: str = "0000000000000000000000000000000000000000"
    variables: list[PipelineVariable] = Field(default_factory=list)
    job: PipelineJobDefinition | None = None


class CiLintRequest(BaseModel):
    content: str
    ref: str = "main"
    variables: list[PipelineVariable] = Field(default_factory=list)
    include_jobs: bool = True
    include_merged_yaml: bool = False


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
    ref: str | None = None,
    ref_kind: str = "branch",
    default_branch: str | None = None,
) -> dict[str, dict]:
    entries = {
        **_pipeline_variable_entries(variables),
        "CI_PIPELINE_SOURCE": _variable_entry(source),
    }
    if default_branch:
        entries["CI_DEFAULT_BRANCH"] = _variable_entry(default_branch)
    if ref:
        entries["CI_COMMIT_REF_NAME"] = _variable_entry(ref)
        if ref_kind == "tag":
            entries["CI_COMMIT_TAG"] = _variable_entry(ref)
        else:
            entries["CI_COMMIT_BRANCH"] = _variable_entry(ref)
    return entries


def _simple_variable_values(entries: dict[str, dict]) -> dict[str, str]:
    return {key: str(entry.get("value", "")) for key, entry in entries.items()}


def _jwt_segment(value: dict) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    ).decode()
    return encoded.rstrip("=")


def _emulator_id_token(
    *,
    project: Project,
    pipeline: Pipeline,
    job: PipelineJob,
    audiences: list[str],
) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    ref_type = "tag" if pipeline.ref.startswith("refs/tags/") else "branch"
    header = {"alg": "none", "typ": "JWT"}
    payload = {
        "aud": audiences[0] if len(audiences) == 1 else audiences,
        "iss": settings.BASE_URL.rstrip("/"),
        "sub": f"project_path:{project.full_name}:ref_type:{ref_type}:ref:{pipeline.ref}",
        "project_id": str(project.id),
        "project_path": project.full_name,
        "pipeline_id": str(pipeline.id),
        "job_id": str(job.id),
        "job_name": job.name,
        "ref": pipeline.ref,
        "ref_type": ref_type,
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
    }
    return f"{_jwt_segment(header)}.{_jwt_segment(payload)}."


def _id_token_variable_entries(
    *,
    project: Project,
    pipeline: Pipeline,
    job: PipelineJob,
    id_tokens: dict[str, dict],
) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    for key, config in id_tokens.items():
        audiences = [str(value) for value in config.get("aud", [])]
        entries[str(key)] = _variable_entry(
            _emulator_id_token(
                project=project,
                pipeline=pipeline,
                job=job,
                audiences=audiences,
            ),
            masked=True,
            public=False,
        )
    return entries


def _expand_ci_rule_value(value: str, variables: dict[str, str]) -> str:
    expanded = value
    for key, variable_value in variables.items():
        expanded = expanded.replace(f"${{{key}}}", variable_value)
        expanded = expanded.replace(f"${key}", variable_value)
    return expanded


async def _get_project(project_id: int, db: DbSession) -> Project:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project Not Found")
    return project


async def _get_project_ref(
    project_ref: str,
    db: DbSession,
    current_user=None,
    *,
    enforce_read_access: bool = False,
) -> Project:
    decoded_ref = unquote(str(project_ref)).strip("/")
    if decoded_ref.isdigit():
        result = await db.execute(select(Project).where(Project.id == int(decoded_ref)))
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
    if (
        enforce_read_access
        and project.private
        and (
            current_user is None
            or await project_access_level(project, current_user, db) < REPORTER
        )
    ):
        raise HTTPException(status_code=404, detail="Project Not Found")
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
    if not sha:
        sha = await _git_output(
            project.disk_path,
            "rev-parse",
            f"refs/tags/{ref}^{{commit}}",
        )
    return sha.strip() if sha else "0000000000000000000000000000000000000000"


async def _ref_kind(project: Project, ref: str) -> str:
    if not project.disk_path:
        return "branch"
    branch = await _git_output(
        project.disk_path,
        "show-ref",
        "--verify",
        f"refs/heads/{ref}",
    )
    if branch:
        return "branch"
    tag = await _git_output(
        project.disk_path,
        "show-ref",
        "--verify",
        f"refs/tags/{ref}",
    )
    return "tag" if tag else "branch"


async def _repo_paths_at_ref(project: Project, ref: str) -> set[str]:
    if not project.disk_path:
        return set()
    output = await _git_output(project.disk_path, "ls-tree", "-r", "--name-only", ref)
    if not output:
        return set()
    return {line.strip() for line in output.splitlines() if line.strip()}


async def _cache_key_file_maps(
    project: Project,
    ref: str,
) -> tuple[dict[str, str], dict[str, str]]:
    if not project.disk_path:
        return {}, {}
    tree = await _git_output(project.disk_path, "ls-tree", "-r", ref)
    if not tree:
        return {}, {}
    blob_ids: dict[str, str] = {}
    for line in tree.splitlines():
        metadata, separator, path = line.partition("\t")
        if not separator:
            continue
        parts = metadata.split()
        if len(parts) >= 3 and parts[1] == "blob":
            blob_ids[path] = parts[2]

    commit_ids: dict[str, str] = {}
    for path in blob_ids:
        commit = await _git_output(
            project.disk_path,
            "log",
            "-n",
            "1",
            "--format=%H",
            ref,
            "--",
            path,
        )
        if commit:
            commit_ids[path] = commit.strip()
    return blob_ids, commit_ids


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


async def _changed_paths_from_ref(project: Project, compare_ref: str, sha: str) -> set[str]:
    if (
        not project.disk_path
        or not compare_ref
        or sha == "0000000000000000000000000000000000000000"
    ):
        return set()
    output = await _git_output(
        project.disk_path,
        "diff",
        "--name-only",
        f"{compare_ref}...{sha}",
    )
    if not output:
        return set()
    return {line.strip() for line in output.splitlines() if line.strip()}


def _collect_rules_changes_compare_refs(
    value: Any,
    variables: dict[str, str],
) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        rules = value.get("rules")
        if isinstance(rules, list):
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                changes = rule.get("changes")
                if not isinstance(changes, dict) or not changes.get("compare_to"):
                    continue
                refs.add(_expand_ci_rule_value(str(changes["compare_to"]), variables))
        for nested in value.values():
            refs.update(_collect_rules_changes_compare_refs(nested, variables))
    elif isinstance(value, list):
        for item in value:
            refs.update(_collect_rules_changes_compare_refs(item, variables))
    return refs


async def _rules_changes_path_sets(
    content: str,
    project: Project,
    sha: str,
    variables: dict[str, str],
) -> dict[str, set[str]]:
    parsed = yaml.safe_load(content) or {}
    if not isinstance(parsed, dict):
        return {}
    path_sets: dict[str, set[str]] = {}
    for compare_ref in _collect_rules_changes_compare_refs(parsed, variables):
        path_sets[compare_ref] = await _changed_paths_from_ref(
            project,
            compare_ref,
            sha,
        )
    return path_sets


def _collect_rules_exists_refs(
    value: Any,
    variables: dict[str, str],
    *,
    current_project: str,
    current_ref: str,
) -> set[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()
    if isinstance(value, dict):
        rules = value.get("rules")
        if isinstance(rules, list):
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                exists = rule.get("exists")
                if not isinstance(exists, dict):
                    continue
                if "project" not in exists and "ref" not in exists:
                    continue
                project_ref = _expand_ci_rule_value(
                    str(exists.get("project") or current_project),
                    variables,
                )
                ref = _expand_ci_rule_value(
                    str(exists.get("ref") or current_ref),
                    variables,
                )
                refs.add((project_ref, ref))
        for nested in value.values():
            refs.update(
                _collect_rules_exists_refs(
                    nested,
                    variables,
                    current_project=current_project,
                    current_ref=current_ref,
                )
            )
    elif isinstance(value, list):
        for item in value:
            refs.update(
                _collect_rules_exists_refs(
                    item,
                    variables,
                    current_project=current_project,
                    current_ref=current_ref,
                )
            )
    return refs


async def _rules_exists_path_sets(
    content: str,
    project: Project,
    ref: str,
    variables: dict[str, str],
    db: DbSession,
) -> dict[tuple[str, str], set[str]]:
    parsed = yaml.safe_load(content) or {}
    if not isinstance(parsed, dict):
        return {}
    path_sets: dict[tuple[str, str], set[str]] = {}
    refs = _collect_rules_exists_refs(
        parsed,
        variables,
        current_project=project.full_name,
        current_ref=ref,
    )
    for project_ref, target_ref in refs:
        target_project = (
            project
            if project_ref == project.full_name
            else await _get_project_ref(project_ref, db)
        )
        paths = await _repo_paths_at_ref(target_project, target_ref)
        path_sets[(project_ref, target_ref)] = paths
        if target_project.id == project.id:
            path_sets[("", target_ref)] = paths
    return path_sets


async def _read_gitlab_ci(project: Project, ref: str) -> str:
    return await _read_repo_file(
        project, ref, ".gitlab-ci.yml", ".gitlab-ci.yml not found"
    )


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


def _include_values(value: Any) -> list[str]:
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
            for file_path in _include_values(item["local"]):
                include_item = {"kind": "local", "file": file_path}
                if "rules" in item:
                    include_item["rules"] = item["rules"]
                items.append(include_item)
        elif isinstance(item, dict) and item.get("project") and item.get("file"):
            for file_path in _include_values(item["file"]):
                include_item = {
                    "kind": "project",
                    "project": str(item["project"]),
                    "file": file_path,
                    "ref": str(item.get("ref") or "main"),
                }
                if "rules" in item:
                    include_item["rules"] = item["rules"]
                items.append(include_item)
        elif isinstance(item, dict) and item.get("remote"):
            for remote_url in _include_values(item["remote"]):
                include_item = {"kind": "remote", "remote": remote_url}
                if "rules" in item:
                    include_item["rules"] = item["rules"]
                items.append(include_item)
        elif isinstance(item, dict) and item.get("template"):
            for template_name in _include_values(item["template"]):
                include_item = {"kind": "template", "template": template_name}
                if "rules" in item:
                    include_item["rules"] = item["rules"]
                items.append(include_item)
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


def _split_top_level_ci_expression(expression: str, operator: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(expression):
        char = expression[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and expression.startswith(operator, index):
            parts.append(expression[start:index].strip())
            index += len(operator)
            start = index
            continue
        index += 1
    parts.append(expression[start:].strip())
    return parts


def _strip_ci_expression_parentheses(expression: str) -> str:
    expression = expression.strip()
    while expression.startswith("(") and expression.endswith(")"):
        depth = 0
        wraps_entire_expression = True
        for index, char in enumerate(expression):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(expression) - 1:
                    wraps_entire_expression = False
                    break
        if not wraps_entire_expression:
            break
        expression = expression[1:-1].strip()
    return expression


def _ci_expression_atom_matches(expression: str, variables: dict[str, str]) -> bool:
    expression = expression.strip()
    if expression.startswith("!"):
        return not _ci_expression_matches(expression[1:].strip(), variables)
    if "==" in expression:
        left, right = expression.split("==", 1)
        return _ci_expression_value(left, variables) == _ci_expression_value(
            right, variables
        )
    if "!=" in expression:
        left, right = expression.split("!=", 1)
        return _ci_expression_value(left, variables) != _ci_expression_value(
            right, variables
        )
    return bool(_ci_expression_value(expression, variables))


def _ci_expression_value(value: str, variables: dict[str, str]) -> str:
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    if value.startswith("$"):
        return variables.get(value[1:].strip("{}"), "")
    if value == "null":
        return ""
    return value


def _ci_expression_matches(expression: str, variables: dict[str, str]) -> bool:
    expression = _strip_ci_expression_parentheses(expression)
    if not expression:
        return False
    or_terms = _split_top_level_ci_expression(expression, "||")
    if len(or_terms) > 1:
        return any(_ci_expression_matches(term, variables) for term in or_terms)
    and_terms = _split_top_level_ci_expression(expression, "&&")
    if len(and_terms) > 1:
        return all(_ci_expression_matches(term, variables) for term in and_terms)
    return _ci_expression_atom_matches(expression, variables)


def _ci_path_matches(pattern: str, paths: set[str]) -> bool:
    normalized = pattern.strip().lstrip("/")
    if not normalized:
        return False
    if normalized.endswith("/"):
        return any(path.startswith(normalized) for path in paths)
    return any(
        path == normalized
        or path.startswith(f"{normalized}/")
        or fnmatch.fnmatch(path, normalized)
        for path in paths
    )


def _include_rule_path_patterns(value: Any, variables: dict[str, str]) -> list[str]:
    if isinstance(value, dict):
        return [
            _expand_ci_rule_value(str(path), variables)
            for path in _include_values(value.get("paths"))
        ]
    return [_expand_ci_rule_value(str(path), variables) for path in _include_values(value)]


def _include_rule_paths_match(
    value: Any,
    paths: set[str],
    variables: dict[str, str],
) -> bool:
    patterns = _include_rule_path_patterns(value, variables)
    return bool(patterns) and any(_ci_path_matches(pattern, paths) for pattern in patterns)


def _include_rules_match(
    include_item: dict,
    variables: dict[str, str],
    existing_paths: set[str],
    changed_paths: set[str],
) -> bool:
    rules = include_item.get("rules")
    if rules is None:
        return True
    if not isinstance(rules, list):
        raise HTTPException(status_code=400, detail="CI include rules must be a list")
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if "if" in rule and not _ci_expression_matches(str(rule["if"]), variables):
            continue
        if "exists" in rule and not _include_rule_paths_match(
            rule.get("exists"),
            existing_paths,
            variables,
        ):
            continue
        if "changes" in rule and not _include_rule_paths_match(
            rule.get("changes"),
            changed_paths,
            variables,
        ):
            continue
        return str(rule.get("when") or "always") != "never"
    return False


def _fetch_remote_include_sync(url: str) -> str:
    context = ssl._create_unverified_context() if url.startswith("https://") else None
    with urlopen(url, timeout=10, context=context) as response:
        status = getattr(response, "status", 200)
        if status >= 400:
            raise HTTPException(
                status_code=400, detail=f"CI remote include failed: {url}"
            )
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
        raise HTTPException(
            status_code=400, detail=f"CI template include not found: {name}"
        )
    return content


async def _read_ci_config_with_includes(
    project: Project,
    ref: str,
    path: str,
    db: DbSession,
    variables: dict[str, str] | None = None,
    existing_paths: set[str] | None = None,
    changed_paths: set[str] | None = None,
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
            variables or {},
            existing_paths or set(),
            changed_paths or set(),
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
    variables: dict[str, str],
    existing_paths: set[str],
    changed_paths: set[str],
    depth: int,
    seen: set[tuple[str, str, str, str]],
) -> dict:
    if depth > 10:
        raise HTTPException(status_code=400, detail="CI include depth limit exceeded")
    root = _ci_mapping(content, path)
    merged: dict = {}
    for include_item in _include_items(root.get("include")):
        if not _include_rules_match(
            include_item,
            variables,
            existing_paths,
            changed_paths,
        ):
            continue
        include_config = await _read_include_config(
            include_item,
            project,
            ref,
            db,
            variables,
            existing_paths,
            changed_paths,
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
    variables: dict[str, str],
    existing_paths: set[str],
    changed_paths: set[str],
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
            variables,
            existing_paths,
            changed_paths,
            depth,
            seen,
        )
    if include_item["kind"] == "remote":
        url = include_item["remote"]
        key = ("remote", url, "", "")
        if key in seen:
            raise HTTPException(
                status_code=400, detail=f"Circular CI include detected: {url}"
            )
        seen.add(key)
        try:
            return await _read_ci_config_content_with_includes(
                await _fetch_remote_include(url),
                url,
                project,
                ref,
                db,
                variables,
                existing_paths,
                changed_paths,
                depth,
                seen,
            )
        finally:
            seen.remove(key)
    if include_item["kind"] == "template":
        name = include_item["template"]
        key = ("template", name, "", "")
        if key in seen:
            raise HTTPException(
                status_code=400, detail=f"Circular CI include detected: {name}"
            )
        seen.add(key)
        try:
            return await _read_ci_config_content_with_includes(
                _read_template_include(name),
                f"template:{name}",
                project,
                ref,
                db,
                variables,
                existing_paths,
                changed_paths,
                depth,
                seen,
            )
        finally:
            seen.remove(key)
    raise HTTPException(status_code=400, detail="Invalid CI include")


async def _read_gitlab_ci_with_includes(
    project: Project,
    ref: str,
    db: DbSession,
    variables: dict[str, str] | None = None,
    existing_paths: set[str] | None = None,
    changed_paths: set[str] | None = None,
) -> str:
    merged = await _read_ci_config_with_includes(
        project,
        ref,
        ".gitlab-ci.yml",
        db,
        variables or {},
        existing_paths or set(),
        changed_paths or set(),
    )
    return yaml.safe_dump(merged, sort_keys=False)


def _pipeline_json(pipeline: Pipeline) -> dict:
    return {
        "id": pipeline.id,
        "iid": pipeline.iid,
        "project_id": pipeline.project_id,
        "sha": pipeline.sha,
        "before_sha": pipeline.before_sha
        or "0000000000000000000000000000000000000000",
        "ref": pipeline.ref,
        "name": pipeline.name,
        "status": pipeline.status,
        "source": pipeline.source,
        "security_warnings": pipeline.security_warnings or [],
        "created_at": _fmt_dt(pipeline.created_at),
        "updated_at": _fmt_dt(pipeline.updated_at),
        "web_url": f"{settings.BASE_URL}/{pipeline.project.full_name}/-/pipelines/{pipeline.id}"
        if pipeline.project
        else None,
    }


def _pipeline_variable_json(variable: dict) -> dict:
    return {
        "key": str(variable.get("key", "")),
        "value": str(variable.get("value", "")),
        "variable_type": str(variable.get("variable_type") or "env_var"),
        "file": bool(variable.get("file", False)),
        "masked": bool(variable.get("masked", False)),
        "raw": bool(variable.get("raw", False)),
        "public": variable.get("public"),
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
            item = {
                "job": str(need["job"]),
                "optional": bool(need.get("optional", False)),
                "artifacts": bool(need.get("artifacts", True)),
            }
            if need.get("project"):
                item["project"] = str(need["project"])
            if need.get("pipeline"):
                item["pipeline"] = str(need["pipeline"])
            if need.get("ref"):
                item["ref"] = str(need["ref"])
            items.append(item)
    return items


def _need_names(needs: list | None) -> list[str]:
    return [item["job"] for item in _need_items(needs)]


def _is_external_need(need: dict) -> bool:
    return bool(need.get("project") or need.get("pipeline"))


def _need_key(need: dict) -> tuple:
    if need.get("project"):
        return ("project", need.get("project"), need.get("ref"), need["job"])
    if need.get("pipeline"):
        return ("pipeline", need.get("pipeline"), need["job"])
    return ("job", need["job"])


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _elapsed_seconds(start: datetime | None, end: datetime | None) -> int | None:
    started = _aware_utc(start)
    finished = _aware_utc(end)
    if started is None or finished is None:
        return None
    return max(0, int((finished - started).total_seconds()))


def _failed_job_is_allowed(job: PipelineJob) -> bool:
    if job.status != "failed":
        return False
    if job.allow_failure:
        return True
    if job.exit_code is None:
        return False
    allowed_codes = [int(code) for code in (job.allow_failure_exit_codes or [])]
    return int(job.exit_code) in allowed_codes


def _reset_job_for_retry(job: PipelineJob, now: datetime) -> None:
    job.status = "scheduled" if (job.when or "on_success") == "delayed" else "pending"
    job.job_token = f"gljt-persisted-{secrets.token_urlsafe(24)}"
    job.runner_name = None
    job.failure_reason = None
    job.exit_code = None
    job.trace_checksum = None
    job.trace_size = 0
    job.coverage = None
    job.queued_at = now
    if job.status == "scheduled":
        job.scheduled_at = now.replace(tzinfo=None)
    else:
        job.scheduled_at = None
    job.started_at = None
    job.finished_at = None
    if job.trace:
        job.trace.content = ""
        job.trace.size = 0


async def _erase_job_trace_and_artifacts(job: PipelineJob, db: DbSession) -> None:
    job.trace_checksum = None
    job.trace_size = 0
    job.erased_at = datetime.now(timezone.utc)
    if job.trace:
        job.trace.content = ""
        job.trace.size = 0

    for artifact in list(job.artifacts):
        if artifact.storage_path:
            try:
                os.remove(artifact.storage_path)
            except FileNotFoundError:
                pass
        await db.delete(artifact)
    job.artifacts.clear()


def _requeue_stale_or_pending_job(job: PipelineJob, now: datetime) -> dict:
    """Reset a pending/running/scheduled job for another runner poll.

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
    if job.status == "scheduled":
        job.status = "pending"
        job.scheduled_at = None
    return previous


def _validate_job_needs(parsed_jobs: list[ParsedCiJob]) -> None:
    jobs_by_name = {job.name: job for job in parsed_jobs}
    for job in parsed_jobs:
        needs = _need_items(job.needs)
        seen: set[tuple] = set()
        duplicate_names: list[str] = []
        for item in needs:
            key = _need_key(item)
            if key in seen:
                duplicate_names.append(item["job"])
            seen.add(key)
        if duplicate_names:
            names = ", ".join(sorted(set(duplicate_names)))
            raise HTTPException(
                status_code=400,
                detail=f"Job {job.name} has duplicate needs: {names}",
            )
        if ("job", job.name) in seen:
            raise HTTPException(
                status_code=400,
                detail=f"Job {job.name} cannot need itself",
            )
        missing = [
            item["job"]
            for item in needs
            if not _is_external_need(item)
            and item["job"] not in jobs_by_name
            and not item["optional"]
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
            if not _is_external_need(item)
            and item["job"] in jobs_by_name
            and jobs_by_name[item["job"]].stage_index > job.stage_index
        ]
        if future_stage:
            names = ", ".join(future_stage)
            raise HTTPException(
                status_code=400,
                detail=f"Job {job.name} needs future-stage job(s): {names}",
            )


def _validate_job_dependencies(parsed_jobs: list[ParsedCiJob]) -> None:
    jobs_by_name = {job.name: job for job in parsed_jobs}
    for job in parsed_jobs:
        if job.dependencies is None:
            continue
        missing = [
            dependency
            for dependency in job.dependencies
            if dependency not in jobs_by_name
        ]
        if missing:
            names = ", ".join(missing)
            raise HTTPException(
                status_code=400,
                detail=f"Job {job.name} dependencies missing job(s): {names}",
            )
        invalid_stage = [
            dependency
            for dependency in job.dependencies
            if jobs_by_name[dependency].stage_index >= job.stage_index
        ]
        if invalid_stage:
            names = ", ".join(invalid_stage)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Job {job.name} dependencies must be from earlier stages: "
                    f"{names}"
                ),
            )


def _ci_lint_response(
    body: CiLintRequest,
    *,
    default_branch: str = "main",
) -> dict:
    variables = _pipeline_context_variable_entries(
        body.variables,
        source="api",
        ref=body.ref,
        default_branch=default_branch,
    )
    try:
        parsed_jobs = parse_gitlab_ci(
            body.content,
            ref=body.ref,
            variables=_simple_variable_values(variables),
        )
        _validate_job_needs(parsed_jobs)
        _validate_job_dependencies(parsed_jobs)
    except HTTPException as exc:
        error = str(exc.detail)
        return {
            "status": "invalid",
            "valid": False,
            "errors": [error],
            "warnings": [],
        }
    except (ValueError, yaml.YAMLError) as exc:
        return {
            "status": "invalid",
            "valid": False,
            "errors": [str(exc)],
            "warnings": [],
        }

    payload = {
        "status": "valid",
        "valid": True,
        "errors": [],
        "warnings": [],
    }
    if body.include_jobs:
        payload["jobs"] = [
            {
                "name": job.name,
                "stage": job.stage,
                "stage_index": job.stage_index,
                "when": job.when,
                "allow_failure": job.allow_failure,
            }
            for job in parsed_jobs
        ]
    if body.include_merged_yaml:
        payload["merged_yaml"] = body.content
    return payload


@router.post("/ci/lint")
async def lint_ci_yaml(body: CiLintRequest):
    return _ci_lint_response(body)


@router.post("/projects/{project_ref:path}/ci/lint")
async def lint_project_ci_yaml(
    project_ref: str,
    body: CiLintRequest,
    db: DbSession,
    current_user: CurrentUser,
):
    project = await _get_project_ref(
        project_ref, db, current_user, enforce_read_access=True
    )
    return _ci_lint_response(
        body,
        default_branch=getattr(project, "default_branch", None) or "main",
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
        "failure_reason": job.failure_reason,
        "stage": job.stage,
        "stage_index": job.stage_index,
        "name": job.name,
        "image": job.image,
        "image_config": job.image_config or {},
        "needs": _need_names(job.needs),
        "dependencies": job.dependencies,
        "tag_list": job.tags or [],
        "services": job.services or [],
        "cache": job.cache or [],
        "when": job.when or "on_success",
        "retry": job.retry_config or {},
        "retry_attempt": job.retry_attempt or 0,
        "timeout": job.timeout_seconds,
        "interruptible": bool(job.interruptible),
        "resource_group": job.resource_group,
        "ref": job.pipeline.ref if job.pipeline else None,
        "tag": bool((job.variables or {}).get("CI_COMMIT_TAG")),
        "coverage": job.coverage,
        "coverage_regex": job.coverage_regex,
        "environment": job.environment,
        "environment_url": job.environment_url,
        "environment_action": job.environment_action,
        "allow_failure": bool(job.allow_failure),
        "allow_failure_exit_codes": job.allow_failure_exit_codes or [],
        "trigger": {
            "project": job.trigger_project,
            "ref": job.trigger_ref,
            "strategy": job.trigger_strategy,
        }
        if job.trigger_project
        else None,
        "downstream_pipeline": (
            {"id": job.downstream_pipeline_id}
            if job.downstream_pipeline_id is not None
            else None
        ),
        "created_at": _fmt_dt(job.created_at),
        "scheduled_at": _fmt_dt(job.scheduled_at),
        "started_at": _fmt_dt(job.started_at),
        "finished_at": _fmt_dt(job.finished_at),
        "erased_at": _fmt_dt(job.erased_at),
        "duration": _elapsed_seconds(job.started_at, job.finished_at),
        "queued_duration": _elapsed_seconds(job.queued_at, job.started_at),
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
    blocking_statuses = [
        job.status
        for job in pipeline.jobs
        if not _failed_job_is_allowed(job)
    ]
    now = datetime.now(timezone.utc)

    if not statuses:
        pipeline.status = "pending"
    elif not blocking_statuses:
        pipeline.status = "success"
        pipeline.finished_at = pipeline.finished_at or now
    elif all(status == "canceled" for status in blocking_statuses):
        pipeline.status = "canceled"
        pipeline.finished_at = pipeline.finished_at or now
    elif any(status == "canceled" for status in blocking_statuses) and not any(
        status in {"pending", "running", "scheduled"} for status in blocking_statuses
    ):
        pipeline.status = "canceled"
        pipeline.finished_at = pipeline.finished_at or now
    elif any(status == "running" for status in blocking_statuses):
        pipeline.status = "running"
        pipeline.started_at = pipeline.started_at or now
        pipeline.finished_at = None
    elif any(status in {"pending", "scheduled"} for status in blocking_statuses):
        pipeline.status = "pending"
        pipeline.finished_at = None
    elif any(status == "failed" for status in blocking_statuses):
        pipeline.status = "failed"
        pipeline.finished_at = pipeline.finished_at or now
    elif all(
        status in {"success", "skipped", "manual", "failed"}
        for status in blocking_statuses
    ):
        pipeline.status = "success"
        pipeline.finished_at = pipeline.finished_at or now
    else:
        pipeline.status = "pending"


async def _resolve_bridge_target(
    project: Project,
    parsed_job: ParsedCiJob,
    ref: str,
    db: DbSession,
) -> Project | None:
    if not parsed_job.trigger:
        return None
    target_ref = str(parsed_job.trigger["ref"])
    target_name = str(parsed_job.trigger["project"]).strip("/")
    result = await db.execute(
        select(Project).where(Project.full_name == unquote(target_name).strip("/"))
    )
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(
            status_code=400,
            detail=f"Bridge job {parsed_job.name} target project not found: {target_name}",
        )
    if target.id == project.id and target_ref == ref:
        raise HTTPException(
            status_code=400,
            detail=f"Bridge job {parsed_job.name} cannot trigger the same project/ref",
        )
    return target


async def _cancel_interruptible_jobs_for_new_pipeline(
    project_id: int, ref: str, new_pipeline_id: int, db: DbSession
) -> None:
    """Cancel older interruptible jobs superseded by a same-ref pipeline."""
    result = await db.execute(
        select(Pipeline)
        .options(selectinload(Pipeline.jobs))
        .where(
            Pipeline.project_id == project_id,
            Pipeline.ref == ref,
            Pipeline.id != new_pipeline_id,
        )
        .order_by(Pipeline.id.desc())
    )
    now = datetime.now(timezone.utc)
    for pipeline in result.scalars().all():
        changed = False
        for job in pipeline.jobs:
            if job.interruptible and job.status in {
                "pending",
                "running",
                "scheduled",
                "manual",
            }:
                job.status = "canceled"
                job.failure_reason = "canceled"
                job.finished_at = now
                changed = True
        if changed:
            await _derive_pipeline_status(pipeline, db)


async def _create_pipeline(
    project_id: int,
    body: CreatePipelineRequest,
    db: DbSession,
    *,
    source: str = "api",
    actor=None,
    before_sha: str | None = None,
) -> Pipeline:
    """Create a persisted pipeline from a direct job or `.gitlab-ci.yml`."""
    project = await _get_project(project_id, db)
    parsed_jobs: list[ParsedCiJob]
    ci_content = ""
    pipeline_name = None
    sha = body.sha
    if sha == "0000000000000000000000000000000000000000":
        sha = await _resolve_ref(project, body.ref)
    ref_kind = await _ref_kind(project, body.ref)

    if body.variables and source not in {"trigger", "schedule", "merge_request_event"}:
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
                image_config={},
                script=body.job.script,
                variables=body.job.variables,
                variable_metadata={
                    key: _variable_entry(value)
                    for key, value in body.job.variables.items()
                },
                needs=body.job.needs,
                dependencies=body.job.dependencies,
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
                allow_failure=body.job.allow_failure,
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
                ref=body.ref,
                ref_kind=ref_kind,
                default_branch=project.default_branch,
            )
            ci_content = await _read_repo_file(
                project,
                body.ref,
                ".gitlab-ci.yml",
                ".gitlab-ci.yml not found",
            )
            rule_variables = _simple_variable_values(
                {
                    **rule_project_variable_entries,
                    **pipeline_variable_entries,
                }
            )
            existing_paths = await _repo_paths_at_ref(project, body.ref)
            cache_key_files, cache_key_files_commits = await _cache_key_file_maps(
                project,
                sha,
            )
            changed_paths = await _changed_paths_at_sha(project, sha)
            merged_ci_content = await _read_gitlab_ci_with_includes(
                project,
                body.ref,
                db,
                variables=rule_variables,
                existing_paths=existing_paths,
                changed_paths=changed_paths,
            )
            existing_path_sets = await _rules_exists_path_sets(
                merged_ci_content,
                project,
                body.ref,
                rule_variables,
                db,
            )
            changed_path_sets = await _rules_changes_path_sets(
                merged_ci_content,
                project,
                sha,
                rule_variables,
            )
            parsed_jobs = parse_gitlab_ci(
                merged_ci_content,
                ref=body.ref,
                ref_kind=ref_kind,
                variables=rule_variables,
                existing_paths=existing_paths,
                changed_paths=changed_paths,
                existing_path_sets=existing_path_sets,
                changed_path_sets=changed_path_sets,
                cache_key_files=cache_key_files,
                cache_key_files_commits=cache_key_files_commits,
            )
            pipeline_name = parse_gitlab_ci_workflow_name(
                merged_ci_content,
                ref=body.ref,
                ref_kind=ref_kind,
                variables=rule_variables,
                existing_paths=existing_paths,
                changed_paths=changed_paths,
                existing_path_sets=existing_path_sets,
                changed_path_sets=changed_path_sets,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    _validate_job_needs(parsed_jobs)
    _validate_job_dependencies(parsed_jobs)
    bridge_targets: dict[str, Project] = {}
    for parsed_job in parsed_jobs:
        target = await _resolve_bridge_target(project, parsed_job, body.ref, db)
        if target is not None:
            bridge_targets[parsed_job.name] = target

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
        before_sha=before_sha,
        name=pipeline_name,
        status="pending",
        source=source,
        variables=[variable.model_dump() for variable in body.variables],
        security_warnings=security_warnings,
    )
    db.add(pipeline)
    await db.flush()

    pipeline_variable_entries = _pipeline_context_variable_entries(
        body.variables,
        source=source,
        ref=body.ref,
        ref_kind=ref_kind,
        default_branch=project.default_branch,
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
        job_scheduled_at = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(seconds=parsed_job.start_in_seconds)
            if parsed_job.when == "delayed" and parsed_job.start_in_seconds
            else None
        )
        job_status = (
            "manual"
            if parsed_job.when == "manual"
            else "scheduled"
            if parsed_job.when == "delayed"
            else "pending"
            if parsed_job.trigger
            else "pending"
        )
        job = PipelineJob(
            pipeline_id=pipeline.id,
            project_id=project.id,
            name=parsed_job.name,
            stage=parsed_job.stage,
            stage_index=parsed_job.stage_index,
            image=parsed_job.image,
            image_config=parsed_job.image_config,
            script=parsed_job.script,
            variables=variables,
            needs=_need_items(parsed_job.needs)
            if parsed_job.needs is not None
            else None,
            dependencies=parsed_job.dependencies,
            tags=parsed_job.tags,
            services=parsed_job.services,
            cache=parsed_job.cache,
            artifacts_paths=parsed_job.artifacts_paths,
            artifacts_config=parsed_job.artifacts,
            when=parsed_job.when,
            scheduled_at=job_scheduled_at,
            allow_failure=parsed_job.allow_failure,
            allow_failure_exit_codes=parsed_job.allow_failure_exit_codes,
            retry_config=parsed_job.retry,
            timeout_seconds=parsed_job.timeout_seconds,
            interruptible=parsed_job.interruptible,
            resource_group=parsed_job.resource_group,
            coverage_regex=parsed_job.coverage,
            environment=parsed_job.environment,
            environment_url=parsed_job.environment_url,
            environment_action=parsed_job.environment_action,
            hooks_config=parsed_job.hooks,
            trigger_project=parsed_job.trigger["project"] if parsed_job.trigger else None,
            trigger_ref=parsed_job.trigger["ref"] if parsed_job.trigger else None,
            trigger_strategy=parsed_job.trigger["strategy"]
            if parsed_job.trigger
            else None,
            secret_metadata=[
                ci_secret_metadata_entry(resolved_secret)
                for resolved_secret in resolved_secrets
            ],
            job_token=f"gljt-persisted-{secrets.token_urlsafe(24)}",
            status=job_status,
        )
        db.add(job)
        await db.flush()
        if parsed_job.id_tokens:
            job.variables = {
                **(job.variables or {}),
                **_id_token_variable_entries(
                    project=project,
                    pipeline=pipeline,
                    job=job,
                    id_tokens=parsed_job.id_tokens,
                ),
            }
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
    await _cancel_interruptible_jobs_for_new_pipeline(
        project.id, body.ref, pipeline.id, db
    )
    await db.commit()
    await db.refresh(pipeline)
    if bridge_targets:
        await db.refresh(pipeline, attribute_names=["jobs"])
        now = datetime.now(timezone.utc)
        for job in pipeline.jobs:
            if not job.trigger_project or job.downstream_pipeline_id is not None:
                continue
            target = bridge_targets.get(job.name)
            if target is None:
                continue
            downstream = await _create_pipeline(
                target.id,
                CreatePipelineRequest(ref=job.trigger_ref or body.ref),
                db,
                source="parent_pipeline",
            )
            job.downstream_pipeline_id = downstream.id
            job.status = "success"
            job.started_at = job.started_at or now
            job.finished_at = now
        await _derive_pipeline_status(pipeline, db)
        await db.commit()
        await db.refresh(pipeline)
    return pipeline


@router.get("/projects/{project_id}/triggers")
async def list_pipeline_triggers(
    project_id: int,
    db: DbSession,
    current_user: CurrentUser,
):
    project = await _get_project(project_id, db)
    await require_project_access(project, current_user, db, MAINTAINER)
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
    current_user: CurrentUser,
):
    project = await _get_project(project_id, db)
    await require_project_access(project, current_user, db, MAINTAINER)
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
async def delete_pipeline_trigger(
    project_id: int,
    trigger_id: int,
    db: DbSession,
    current_user: CurrentUser,
):
    project = await _get_project(project_id, db)
    await require_project_access(project, current_user, db, MAINTAINER)
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
async def list_pipeline_schedules(
    project_id: int,
    db: DbSession,
    current_user: CurrentUser,
):
    project = await _get_project(project_id, db)
    await require_project_access(project, current_user, db, DEVELOPER)
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
    current_user: CurrentUser,
):
    project = await _get_project(project_id, db)
    await require_project_access(project, current_user, db, DEVELOPER)
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
    try:
        set_schedule_next_run(schedule)
    except ValueError as exc:
        raise http_cron_error(exc) from exc
    db.add(schedule)
    await db.commit()
    await db.refresh(schedule)
    return _schedule_json(schedule)


@router.get("/projects/{project_id}/pipeline_schedules/{schedule_id}")
async def get_pipeline_schedule(
    project_id: int,
    schedule_id: int,
    db: DbSession,
    current_user: CurrentUser,
):
    project = await _get_project(project_id, db)
    await require_project_access(project, current_user, db, DEVELOPER)
    schedule = await _get_pipeline_schedule(project_id, schedule_id, db)
    return _schedule_json(schedule)


@router.put("/projects/{project_id}/pipeline_schedules/{schedule_id}")
async def update_pipeline_schedule(
    project_id: int,
    schedule_id: int,
    body: UpdatePipelineScheduleRequest,
    db: DbSession,
    current_user: CurrentUser,
):
    project = await _get_project(project_id, db)
    await require_project_access(project, current_user, db, DEVELOPER)
    schedule = await _get_pipeline_schedule(project_id, schedule_id, db)
    updates = body.model_dump(exclude_unset=True)
    if "variables" in updates and updates["variables"] is not None:
        updates["variables"] = [
            variable.model_dump() for variable in body.variables or []
        ]
    for key, value in updates.items():
        if value is not None:
            setattr(schedule, key, value)
    try:
        set_schedule_next_run(schedule)
    except ValueError as exc:
        raise http_cron_error(exc) from exc
    await db.commit()
    await db.refresh(schedule)
    return _schedule_json(schedule)


@router.delete(
    "/projects/{project_id}/pipeline_schedules/{schedule_id}", status_code=204
)
async def delete_pipeline_schedule(
    project_id: int,
    schedule_id: int,
    db: DbSession,
    current_user: CurrentUser,
):
    project = await _get_project(project_id, db)
    await require_project_access(project, current_user, db, DEVELOPER)
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


@router.post(
    "/projects/{project_id}/pipeline_schedules/{schedule_id}/play", status_code=201
)
async def play_pipeline_schedule(
    project_id: int,
    schedule_id: int,
    db: DbSession,
    current_user: CurrentUser,
):
    project = await _get_project(project_id, db)
    await require_project_access(project, current_user, db, DEVELOPER)
    schedule = await _get_pipeline_schedule(project_id, schedule_id, db)
    pipeline = await materialize_pipeline_schedule(
        schedule,
        project_id,
        db,
        actor=current_user,
    )
    return _pipeline_json(pipeline)


@router.post("/projects/{project_ref:path}/pipeline", status_code=201)
async def create_pipeline(
    project_ref: str,
    body: CreatePipelineRequest,
    db: DbSession,
    current_user: CurrentUser,
):
    """Create a minimal pipeline from a direct job or `.gitlab-ci.yml`."""
    if current_user is None:
        raise HTTPException(status_code=401, detail="Requires authentication")
    project = await _get_project_ref(project_ref, db)
    await require_project_access(project, current_user, db, DEVELOPER)
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
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    project = await _get_project_ref(
        project_ref, db, current_user, enforce_read_access=True
    )
    query = (
        select(Pipeline)
        .where(Pipeline.project_id == project.id)
        .order_by(Pipeline.id.desc())
    )
    total = (
        await db.execute(select(func.count()).select_from(query.subquery()))
    ).scalar() or 0
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
    current_user: CurrentUser,
    ref: str | None = None,
):
    project = await _get_project_ref(
        project_ref, db, current_user, enforce_read_access=True
    )
    query = select(Pipeline).where(Pipeline.project_id == project.id)
    if ref:
        query = query.where(Pipeline.ref == ref)
    result = await db.execute(query.order_by(Pipeline.id.desc()).limit(1))
    pipeline = result.scalar_one_or_none()
    if pipeline is None:
        raise HTTPException(status_code=404, detail="Pipeline Not Found")
    return _pipeline_json(pipeline)


@router.get("/projects/{project_ref:path}/pipelines/{pipeline_id}")
async def get_pipeline(
    project_ref: str, pipeline_id: int, db: DbSession, current_user: CurrentUser
):
    pipeline = await _get_pipeline_for_project_ref(
        project_ref, pipeline_id, db, current_user, enforce_read_access=True
    )
    return _pipeline_json(pipeline)


@router.get("/projects/{project_ref:path}/pipelines/{pipeline_id}/variables")
async def get_pipeline_variables(
    project_ref: str, pipeline_id: int, db: DbSession, current_user: CurrentUser
):
    pipeline = await _get_pipeline_for_project_ref(
        project_ref, pipeline_id, db, current_user, enforce_read_access=True
    )
    return [_pipeline_variable_json(variable) for variable in pipeline.variables or []]


async def _get_pipeline_for_project_ref(
    project_ref: str,
    pipeline_id: int,
    db: DbSession,
    current_user=None,
    *,
    enforce_read_access: bool = False,
) -> Pipeline:
    project = await _get_project_ref(
        project_ref, db, current_user, enforce_read_access=enforce_read_access
    )
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
async def cancel_pipeline(
    project_ref: str,
    pipeline_id: int,
    db: DbSession,
    current_user: CurrentUser,
):
    pipeline = await _get_pipeline_for_project_ref(project_ref, pipeline_id, db)
    await require_project_access(pipeline.project, current_user, db, DEVELOPER)
    now = datetime.now(timezone.utc)
    for job in pipeline.jobs:
        if job.status in {"pending", "running", "manual", "scheduled"}:
            job.status = "canceled"
            job.finished_at = job.finished_at or now
    pipeline.status = "canceled"
    pipeline.finished_at = pipeline.finished_at or now
    await db.commit()
    await db.refresh(pipeline)
    return _pipeline_json(pipeline)


@router.post("/projects/{project_ref:path}/pipelines/{pipeline_id}/retry")
async def retry_pipeline(
    project_ref: str,
    pipeline_id: int,
    db: DbSession,
    current_user: CurrentUser,
):
    pipeline = await _get_pipeline_for_project_ref(project_ref, pipeline_id, db)
    await require_project_access(pipeline.project, current_user, db, DEVELOPER)
    now = datetime.now(timezone.utc)
    retryable = {"failed", "canceled", "skipped"}
    for job in pipeline.jobs:
        if job.status in retryable:
            _reset_job_for_retry(job, now)
    await _derive_pipeline_status(pipeline, db)
    pipeline.finished_at = (
        None if pipeline.status in {"pending", "running"} else pipeline.finished_at
    )
    await db.commit()
    await db.refresh(pipeline)
    return _pipeline_json(pipeline)


@router.get("/projects/{project_ref:path}/pipelines/{pipeline_id}/diagnostics")
async def get_pipeline_diagnostics(
    project_ref: str, pipeline_id: int, db: DbSession, current_user: CurrentUser
):
    pipeline = await _get_pipeline_for_project_ref(
        project_ref, pipeline_id, db, current_user, enforce_read_access=True
    )
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
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    project = await _get_project_ref(
        project_ref, db, current_user, enforce_read_access=True
    )
    query = (
        select(PipelineJob)
        .join(Pipeline)
        .where(Pipeline.project_id == project.id, Pipeline.id == pipeline_id)
        .order_by(PipelineJob.id.asc())
    )
    total = (
        await db.execute(select(func.count()).select_from(query.subquery()))
    ).scalar() or 0
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
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    project = await _get_project_ref(
        project_ref, db, current_user, enforce_read_access=True
    )
    query = (
        select(PipelineJob)
        .where(PipelineJob.project_id == project.id)
        .order_by(PipelineJob.id.desc())
    )
    total = (
        await db.execute(select(func.count()).select_from(query.subquery()))
    ).scalar() or 0
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
    current_user: CurrentUser,
    job: str,
):
    project = await _get_project_ref(
        project_ref, db, current_user, enforce_read_access=True
    )
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
    if artifact.expire_at and artifact.expire_at <= datetime.now(timezone.utc).replace(
        tzinfo=None
    ):
        raise HTTPException(status_code=404, detail="Artifacts Expired")
    if not os.path.isfile(artifact.storage_path):
        raise HTTPException(status_code=404, detail="Artifacts Not Found")
    return FileResponse(
        artifact.storage_path,
        media_type=artifact.content_type or "application/zip",
        filename=artifact.filename,
    )


@router.get("/projects/{project_ref:path}/jobs/artifacts/{ref_name:path}/raw/{artifact_path:path}")
async def download_project_job_artifact_file_by_ref(
    project_ref: str,
    ref_name: str,
    artifact_path: str,
    db: DbSession,
    current_user: CurrentUser,
    job: str,
):
    project = await _get_project_ref(
        project_ref, db, current_user, enforce_read_access=True
    )
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
    if artifact.expire_at and artifact.expire_at <= datetime.now(timezone.utc).replace(
        tzinfo=None
    ):
        raise HTTPException(status_code=404, detail="Artifacts Expired")
    if not os.path.isfile(artifact.storage_path):
        raise HTTPException(status_code=404, detail="Artifacts Not Found")
    if artifact.file_format != "zip" and not artifact.filename.endswith(".zip"):
        raise HTTPException(status_code=404, detail="Artifact File Not Found")
    try:
        with zipfile.ZipFile(artifact.storage_path) as archive:
            try:
                content = archive.read(artifact_path)
            except KeyError:
                raise HTTPException(
                    status_code=404, detail="Artifact File Not Found"
                ) from None
    except zipfile.BadZipFile:
        raise HTTPException(status_code=404, detail="Artifact File Not Found") from None

    media_type = mimetypes.guess_type(artifact_path)[0] or "application/octet-stream"
    return Response(content=content, media_type=media_type)


@router.get("/projects/{project_ref:path}/jobs/{job_id}")
async def get_project_job(
    project_ref: str, job_id: int, db: DbSession, current_user: CurrentUser
):
    job = await _get_job_for_project_ref(
        project_ref, job_id, db, current_user, enforce_read_access=True
    )
    return _job_json(job)


async def _get_job_for_project_ref(
    project_ref: str,
    job_id: int,
    db: DbSession,
    current_user=None,
    *,
    enforce_read_access: bool = False,
) -> PipelineJob:
    project = await _get_project_ref(
        project_ref, db, current_user, enforce_read_access=enforce_read_access
    )
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
async def cancel_project_job(
    project_ref: str,
    job_id: int,
    db: DbSession,
    current_user: CurrentUser,
):
    job = await _get_job_for_project_ref(project_ref, job_id, db)
    await require_project_access(job.project, current_user, db, DEVELOPER)
    now = datetime.now(timezone.utc)
    if job.status in {"pending", "running", "manual", "scheduled"}:
        job.status = "canceled"
        job.finished_at = job.finished_at or now
    await _derive_pipeline_status(job.pipeline, db)
    await db.commit()
    await db.refresh(job)
    return _job_json(job)


@router.post("/projects/{project_ref:path}/jobs/{job_id}/retry")
async def retry_project_job(
    project_ref: str,
    job_id: int,
    db: DbSession,
    current_user: CurrentUser,
):
    job = await _get_job_for_project_ref(project_ref, job_id, db)
    await require_project_access(job.project, current_user, db, DEVELOPER)
    if job.status in {"failed", "canceled", "skipped", "success"}:
        _reset_job_for_retry(job, datetime.now(timezone.utc))
    await _derive_pipeline_status(job.pipeline, db)
    job.pipeline.finished_at = (
        None
        if job.pipeline.status in {"pending", "running"}
        else job.pipeline.finished_at
    )
    await db.commit()
    await db.refresh(job)
    return _job_json(job)


@router.post("/projects/{project_ref:path}/jobs/{job_id}/play")
async def play_project_job(
    project_ref: str,
    job_id: int,
    db: DbSession,
    current_user: CurrentUser,
):
    job = await _get_job_for_project_ref(project_ref, job_id, db)
    await require_project_access(job.project, current_user, db, DEVELOPER)
    if job.status != "manual":
        raise HTTPException(status_code=400, detail="Job is not playable")
    now = datetime.now(timezone.utc)
    job.status = "pending"
    job.queued_at = now
    job.failure_reason = None
    job.exit_code = None
    await _derive_pipeline_status(job.pipeline, db)
    job.pipeline.finished_at = (
        None
        if job.pipeline.status in {"pending", "running"}
        else job.pipeline.finished_at
    )
    await db.commit()
    await db.refresh(job)
    return _job_json(job)


@router.post("/projects/{project_ref:path}/jobs/{job_id}/erase")
async def erase_project_job(
    project_ref: str,
    job_id: int,
    db: DbSession,
    current_user: CurrentUser,
):
    job = await _get_job_for_project_ref(project_ref, job_id, db)
    await require_project_access(job.project, current_user, db, DEVELOPER)
    if job.status in {"pending", "running", "manual", "scheduled"}:
        raise HTTPException(status_code=400, detail="Job is not erasable")
    await _erase_job_trace_and_artifacts(job, db)
    await db.commit()
    await db.refresh(job)
    return _job_json(job)


@router.get("/projects/{project_ref:path}/jobs/{job_id}/trace")
async def get_project_job_trace(
    project_ref: str, job_id: int, db: DbSession, current_user: CurrentUser
):
    project = await _get_project_ref(
        project_ref, db, current_user, enforce_read_access=True
    )
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


@router.post("/projects/{project_ref:path}/jobs/{job_id}/artifacts/keep")
async def keep_project_job_artifacts(
    project_ref: str,
    job_id: int,
    db: DbSession,
    current_user: CurrentUser,
):
    job = await _get_job_for_project_ref(project_ref, job_id, db)
    await require_project_access(job.project, current_user, db, DEVELOPER)
    if not job.artifacts:
        raise HTTPException(status_code=404, detail="Artifacts Not Found")
    for artifact in job.artifacts:
        artifact.expire_at = None
    await db.commit()
    await db.refresh(job)
    return _job_json(job)


@router.get("/projects/{project_ref:path}/jobs/{job_id}/artifacts")
async def download_project_job_artifacts(
    project_ref: str, job_id: int, db: DbSession, current_user: CurrentUser
):
    project = await _get_project_ref(
        project_ref, db, current_user, enforce_read_access=True
    )
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
    if artifact.expire_at and artifact.expire_at <= datetime.now(timezone.utc).replace(
        tzinfo=None
    ):
        raise HTTPException(status_code=404, detail="Artifacts Expired")
    if not os.path.isfile(artifact.storage_path):
        raise HTTPException(status_code=404, detail="Artifacts Not Found")
    return FileResponse(
        artifact.storage_path,
        media_type=artifact.content_type or "application/zip",
        filename=artifact.filename,
    )


@router.get("/projects/{project_ref:path}/jobs/{job_id}/artifacts/{artifact_path:path}")
async def download_project_job_artifact_file(
    project_ref: str,
    job_id: int,
    artifact_path: str,
    db: DbSession,
    current_user: CurrentUser,
):
    project = await _get_project_ref(
        project_ref, db, current_user, enforce_read_access=True
    )
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
    if artifact.expire_at and artifact.expire_at <= datetime.now(timezone.utc).replace(
        tzinfo=None
    ):
        raise HTTPException(status_code=404, detail="Artifacts Expired")
    if not os.path.isfile(artifact.storage_path):
        raise HTTPException(status_code=404, detail="Artifacts Not Found")
    if artifact.file_format != "zip" and not artifact.filename.endswith(".zip"):
        raise HTTPException(status_code=404, detail="Artifact File Not Found")
    try:
        with zipfile.ZipFile(artifact.storage_path) as archive:
            try:
                content = archive.read(artifact_path)
            except KeyError:
                raise HTTPException(
                    status_code=404, detail="Artifact File Not Found"
                ) from None
    except zipfile.BadZipFile:
        raise HTTPException(status_code=404, detail="Artifact File Not Found") from None

    media_type = mimetypes.guess_type(artifact_path)[0] or "application/octet-stream"
    return Response(content=content, media_type=media_type)
