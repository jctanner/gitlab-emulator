"""GitLab project endpoints.

These endpoints expose the GitLab-facing Project alias over the inherited
repository storage. The broader namespace/group model can replace this adapter
once the scaffold is no longer GitHub-shaped internally.
"""

import asyncio
import os
import re
import shutil
from datetime import datetime, timezone
from urllib.parse import unquote

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.api.deps import AuthUser, CurrentUser, DbSession
from app.api.pagination import paginated_json
from app.config import settings
from app.git.bare_repo import create_initial_commit, get_branches as get_disk_branches
from app.models.branch import Branch, BranchProtection
from app.models.ci import CiSecret, CiVariable
from app.models.group import Group
from app.models.organization import OrgMembership
from app.models.project import Project
from app.models.user import User
from app.schemas.user import _fmt_dt

router = APIRouter(tags=["projects"])
VARIABLE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
VARIABLE_TYPES = {"env_var", "file"}


class ProjectVariableCreate(BaseModel):
    key: str
    value: str
    variable_type: str = "env_var"
    protected: bool = False
    masked: bool = False
    hidden: bool = False
    raw: bool = False
    environment_scope: str = "*"
    description: str | None = None


class ProjectVariableUpdate(BaseModel):
    value: str | None = None
    variable_type: str | None = None
    protected: bool | None = None
    masked: bool | None = None
    hidden: bool | None = None
    raw: bool | None = None
    environment_scope: str | None = None
    description: str | None = None


class ProjectSecretCreate(BaseModel):
    name: str
    value: str
    description: str | None = None
    environment_scope: str = "*"
    branch_scope: str = "*"
    protected: bool = False
    rotation_reminder_days: int | None = None
    status: str = "healthy"


class ProjectSecretUpdate(BaseModel):
    value: str | None = None
    description: str | None = None
    environment_scope: str | None = None
    branch_scope: str | None = None
    protected: bool | None = None
    rotation_reminder_days: int | None = None
    status: str | None = None


def _decode_gitlab_ref(value: str) -> str:
    """Decode once-or-twice encoded GitLab path refs without over-processing."""
    decoded = str(value or "").strip("/")
    for _ in range(2):
        next_value = unquote(decoded).strip("/")
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


async def _init_bare_repo(disk_path: str, default_branch: str = "main") -> None:
    os.makedirs(disk_path, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "git", "init", "--bare", "--initial-branch", default_branch, disk_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()


async def _git_lines(repo_path: str, *args: str) -> list[str]:
    env = {**os.environ, "GIT_DIR": repo_path}
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    return [line for line in stdout.decode().splitlines() if line.strip()]


async def _git_text(repo_path: str, *args: str) -> str:
    env = {**os.environ, "GIT_DIR": repo_path}
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode())
    return stdout.decode()


async def _get_project_or_404(
    project_ref: str,
    db: DbSession,
    current_user: User | None,
) -> Project:
    decoded_ref = _decode_gitlab_ref(project_ref)
    if decoded_ref.isdigit():
        condition = Project.id == int(decoded_ref)
    else:
        condition = Project.full_name == decoded_ref

    result = await db.execute(select(Project).where(condition))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="404 Project Not Found")
    if project.private and (
        current_user is None
        or (current_user.id != project.owner_id and not current_user.site_admin)
    ):
        raise HTTPException(status_code=404, detail="404 Project Not Found")
    return project


def _require_project_owner(project: Project, user: User) -> None:
    if not user.site_admin and project.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")


def _validate_variable_key(key: str) -> str:
    normalized = str(key or "").strip()
    if not VARIABLE_KEY_RE.match(normalized):
        raise HTTPException(status_code=400, detail="Invalid variable key")
    return normalized


def _validate_variable_type(variable_type: str) -> str:
    normalized = str(variable_type or "env_var")
    if normalized not in VARIABLE_TYPES:
        raise HTTPException(status_code=400, detail="Invalid variable_type")
    return normalized


def _validate_secret_name(name: str) -> str:
    normalized = str(name or "").strip()
    if not VARIABLE_KEY_RE.match(normalized):
        raise HTTPException(status_code=400, detail="Invalid secret name")
    return normalized


def _variable_visibility(masked: bool, hidden: bool) -> str:
    if hidden:
        return "masked_and_hidden"
    if masked:
        return "masked"
    return "visible"


def _variable_json(variable: CiVariable) -> dict:
    hidden = variable.visibility == "masked_and_hidden"
    masked = variable.visibility in {"masked", "masked_and_hidden"}
    return {
        "key": variable.key,
        "variable_type": variable.variable_type,
        "value": None if hidden else variable.value,
        "protected": variable.protected,
        "masked": masked,
        "hidden": hidden,
        "raw": variable.raw,
        "environment_scope": variable.environment_scope,
        "description": variable.description,
    }


def _secret_json(secret: CiSecret) -> dict:
    return {
        "name": secret.name,
        "value": None,
        "description": secret.description,
        "environment_scope": secret.environment_scope,
        "branch_scope": secret.branch_scope,
        "protected": secret.protected,
        "rotation_reminder_days": secret.rotation_reminder_days,
        "status": secret.status,
        "last_accessed_at": _fmt_dt(secret.last_accessed_at),
        "last_accessed_by_job_id": secret.last_accessed_by_job_id,
        "created_at": _fmt_dt(secret.created_at),
        "updated_at": _fmt_dt(secret.updated_at),
    }


async def _get_project_secret_or_404(
    project: Project,
    db: DbSession,
    name: str,
    environment_scope: str | None = None,
    branch_scope: str | None = None,
) -> CiSecret:
    query = select(CiSecret).where(
        CiSecret.scope_type == "project",
        CiSecret.scope_id == project.id,
        CiSecret.name == _validate_secret_name(name),
    )
    query = query.where(
        CiSecret.environment_scope == (environment_scope if environment_scope is not None else "*"),
        CiSecret.branch_scope == (branch_scope if branch_scope is not None else "*"),
    )
    secret = (await db.execute(query)).scalar_one_or_none()
    if secret is None:
        raise HTTPException(status_code=404, detail="404 Secret Not Found")
    return secret


async def _get_project_variable_or_404(
    project: Project,
    db: DbSession,
    key: str,
    environment_scope: str | None = None,
) -> CiVariable:
    query = select(CiVariable).where(
        CiVariable.scope_type == "project",
        CiVariable.scope_id == project.id,
        CiVariable.key == _validate_variable_key(key),
    )
    if environment_scope is not None:
        query = query.where(CiVariable.environment_scope == environment_scope)
    else:
        query = query.where(CiVariable.environment_scope == "*")
    variable = (await db.execute(query)).scalar_one_or_none()
    if variable is None:
        raise HTTPException(status_code=404, detail="404 Variable Not Found")
    return variable


def _visibility_from_body(body: dict) -> tuple[str, bool]:
    visibility = body.get("visibility")
    if visibility in {"private", "internal", "public"}:
        return visibility, visibility == "private"
    private = bool(body.get("private", False))
    return ("private" if private else "public"), private


async def _project_json(project: Project, base_url: str, db: DbSession) -> dict:
    owner = project.owner
    namespace_path = project.full_name.rsplit("/", 1)[0]
    namespace_leaf = namespace_path.rsplit("/", 1)[-1]
    namespace_id = owner.id if owner else project.owner_id
    namespace_name = owner.name if owner and owner.name else namespace_path
    namespace_kind = "user"
    namespace_parent_id = None

    if project.owner_type == "Organization":
        result = await db.execute(
            select(Group).where(Group.login == namespace_path)
        )
        organization = result.scalar_one_or_none()
        if organization is not None:
            namespace_id = organization.id
            namespace_name = organization.name or organization.login
            if "/" in organization.login:
                parent_path = organization.login.rsplit("/", 1)[0]
                parent_result = await db.execute(
                    select(Group).where(Group.login == parent_path)
                )
                parent = parent_result.scalar_one_or_none()
                namespace_parent_id = parent.id if parent else None
        namespace_kind = "group"

    api = f"{base_url}/api/v4"
    web_url = f"{base_url}/{project.full_name}"
    git_url = f"{base_url}/{project.full_name}.git"
    ssh_url = f"git@{base_url.split('://', 1)[-1]}:{project.full_name}.git"

    return {
        "id": project.id,
        "description": project.description,
        "name": project.name,
        "name_with_namespace": project.full_name,
        "path": project.name,
        "path_with_namespace": project.full_name,
        "created_at": _fmt_dt(project.created_at),
        "updated_at": _fmt_dt(project.updated_at),
        "default_branch": project.default_branch,
        "tag_list": project.topics or [],
        "topics": project.topics or [],
        "ssh_url_to_repo": ssh_url,
        "http_url_to_repo": git_url,
        "web_url": web_url,
        "readme_url": f"{web_url}/-/blob/{project.default_branch}/README.md",
        "forks_count": project.forks_count,
        "avatar_url": None,
        "star_count": project.stargazers_count,
        "open_issues_count": project.open_issues_count,
        "last_activity_at": _fmt_dt(project.pushed_at or project.updated_at),
        "visibility": project.visibility,
        "namespace": {
            "id": namespace_id,
            "name": namespace_name,
            "path": namespace_leaf,
            "kind": namespace_kind,
            "full_path": namespace_path,
            "parent_id": namespace_parent_id,
            "avatar_url": None,
            "web_url": f"{base_url}/groups/{namespace_path}"
            if namespace_kind == "group"
            else f"{base_url}/{namespace_path}",
        },
        "archived": project.archived,
        "empty_repo": project.pushed_at is None,
        "issues_enabled": project.has_issues,
        "merge_requests_enabled": True,
        "wiki_enabled": project.has_wiki,
        "jobs_enabled": True,
        "snippets_enabled": False,
        "container_registry_enabled": False,
        "service_desk_enabled": False,
        "can_create_merge_request_in": True,
        "request_access_enabled": False,
        "lfs_enabled": True,
        "packages_enabled": False,
        "shared_runners_enabled": True,
        "public_jobs": True,
        "only_allow_merge_if_pipeline_succeeds": False,
        "only_allow_merge_if_all_discussions_are_resolved": False,
        "remove_source_branch_after_merge": None,
        "printing_merge_request_link_enabled": True,
        "resolve_outdated_diff_discussions": False,
        "build_timeout": 3600,
        "auto_cancel_pending_pipelines": "enabled",
        "build_git_strategy": "fetch",
        "ci_default_git_depth": 20,
        "ci_forward_deployment_enabled": True,
        "ci_separated_caches": True,
        "autoclose_referenced_issues": True,
        "suggestion_commit_message": None,
        "import_status": "none",
        "import_error": None,
        "shared_with_groups": [],
        "statistics": {
            "commit_count": 0,
            "storage_size": project.size,
            "repository_size": project.size,
            "wiki_size": 0,
            "lfs_objects_size": 0,
            "job_artifacts_size": 0,
            "packages_size": 0,
            "snippets_size": 0,
        },
        "permissions": {
            "project_access": None,
            "group_access": (
                {
                    "access_level": 50,
                    "notification_level": 3,
                }
                if project.owner_type == "Organization"
                else None
            ),
        },
        "issues_access_level": "enabled" if project.has_issues else "disabled",
        "repository_access_level": "enabled",
        "merge_requests_access_level": "enabled",
        "forking_access_level": "enabled" if project.allow_forking else "disabled",
        "wiki_access_level": "enabled" if project.has_wiki else "disabled",
        "builds_access_level": "enabled",
        "snippets_access_level": "disabled",
        "owner": {
            "id": owner.id,
            "username": owner.login,
            "name": owner.name or owner.login,
            "state": "active",
            "avatar_url": owner.avatar_url,
            "web_url": f"{base_url}/{owner.login}",
        } if owner else None,
        "_links": {
            "self": f"{api}/projects/{project.id}",
            "issues": f"{api}/projects/{project.id}/issues",
            "merge_requests": f"{api}/projects/{project.id}/merge_requests",
            "repo_branches": f"{api}/projects/{project.id}/repository/branches",
            "labels": f"{api}/projects/{project.id}/labels",
            "events": f"{api}/projects/{project.id}/events",
        },
    }


async def _resolve_project_namespace(
    body: dict,
    user: User,
    db: DbSession,
) -> tuple[str, str]:
    namespace_id = body.get("namespace_id")
    namespace_path = _decode_gitlab_ref(body.get("namespace_path") or "")
    if namespace_id is None and not namespace_path:
        return user.login, "User"

    if namespace_path and namespace_path == user.login:
        return user.login, "User"

    query = select(Group)
    if namespace_id is not None:
        try:
            namespace_id_int = int(namespace_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid namespace_id") from exc
        query = query.where(Group.id == namespace_id_int)
    else:
        query = query.where(Group.login == namespace_path)

    result = await db.execute(query)
    organization = result.scalar_one_or_none()
    if organization is None:
        if namespace_id is not None:
            try:
                namespace_id_int = int(namespace_id)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Invalid namespace_id") from exc
            if namespace_id_int == user.id:
                return user.login, "User"
        raise HTTPException(status_code=404, detail="Namespace Not Found")

    membership = (
        await db.execute(
            select(OrgMembership).where(
                OrgMembership.org_id == organization.id,
                OrgMembership.user_id == user.id,
                OrgMembership.state == "active",
            )
        )
    ).scalar_one_or_none()
    if membership is None and not user.site_admin:
        raise HTTPException(status_code=403, detail="Forbidden")

    return organization.login, "Organization"


def _branch_json(
    project: Project,
    branch: dict,
    base_url: str,
    protected: bool = False,
) -> dict:
    sha = branch["sha"]
    name = branch["name"]
    return {
        "name": name,
        "merged": False,
        "protected": protected,
        "default": name == project.default_branch,
        "developers_can_push": False,
        "developers_can_merge": False,
        "can_push": True,
        "web_url": f"{base_url}/{project.full_name}/-/tree/{name}",
        "commit": {
            "id": sha,
            "short_id": sha[:8],
            "created_at": None,
            "parent_ids": [],
            "title": "",
            "message": "",
            "author_name": "",
            "author_email": "",
            "authored_date": None,
            "committer_name": "",
            "committer_email": "",
            "committed_date": None,
            "trailers": {},
            "extended_trailers": {},
            "web_url": f"{base_url}/{project.full_name}/-/commit/{sha}",
        },
    }


async def _get_branch_record(
    project: Project,
    branch_name: str,
    db: DbSession,
) -> Branch | None:
    result = await db.execute(
        select(Branch)
        .options(selectinload(Branch.protection), selectinload(Branch.repository))
        .where(
            Branch.repo_id == project.id,
            Branch.name == branch_name,
        )
    )
    return result.scalar_one_or_none()


async def _get_branch_json(
    project: Project,
    branch_name: str,
    base_url: str,
    db: DbSession | None = None,
) -> dict:
    if not project.disk_path or not os.path.isdir(project.disk_path):
        raise HTTPException(status_code=404, detail="404 Project Not Found")
    decoded_name = unquote(branch_name)
    try:
        sha = (
            await _git_text(
                project.disk_path,
                "rev-parse",
                f"refs/heads/{decoded_name}^{{commit}}",
            )
        ).strip()
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="404 Branch Not Found") from exc
    protected = False
    if db is not None:
        record = await _get_branch_record(project, decoded_name, db)
        protected = bool(record and record.protected)
    return _branch_json(project, {"name": decoded_name, "sha": sha}, base_url, protected)


def _access_level_entry(access_level: int, entry_id: int | None = None) -> dict:
    descriptions = {
        0: "No one",
        30: "Developers + Maintainers",
        40: "Maintainers",
        60: "Administrators",
    }
    return {
        "id": entry_id,
        "access_level": access_level,
        "access_level_description": descriptions.get(
            access_level,
            f"Access level {access_level}",
        ),
        "deploy_key_id": None,
        "user_id": None,
        "group_id": None,
    }


def _access_levels(value, default: int) -> list[dict]:
    if value is None:
        return [_access_level_entry(default)]
    if isinstance(value, list):
        levels = []
        for index, item in enumerate(value, start=1):
            if isinstance(item, dict):
                access_level = int(item.get("access_level", default))
            else:
                access_level = int(item)
            levels.append(_access_level_entry(access_level, index))
        return levels or [_access_level_entry(default)]
    return [_access_level_entry(int(value))]


def _protected_branch_json(
    branch: Branch,
    base_url: str,
    project: Project | None = None,
) -> dict:
    restrictions = branch.protection.restrictions if branch.protection else {}
    restrictions = restrictions or {}
    push_levels = restrictions.get("push_access_levels") or [_access_level_entry(40)]
    merge_levels = restrictions.get("merge_access_levels") or [_access_level_entry(40)]
    unprotect_levels = restrictions.get("unprotect_access_levels") or [_access_level_entry(40)]
    project_path = project.full_name if project is not None else branch.repository.full_name
    return {
        "id": branch.protection.id if branch.protection else branch.id,
        "name": branch.name,
        "push_access_levels": push_levels,
        "merge_access_levels": merge_levels,
        "unprotect_access_levels": unprotect_levels,
        "allow_force_push": bool(restrictions.get("allow_force_push", False)),
        "code_owner_approval_required": bool(
            restrictions.get("code_owner_approval_required", False)
        ),
        "inherited": False,
        "created_at": None,
        "updated_at": None,
        "web_url": f"{base_url}/{project_path}/-/branches/{branch.name}",
    }


def _project_order_column(order_by: str):
    allowed = {
        "id": Project.id,
        "name": Project.name,
        "path": Project.name,
        "created_at": Project.created_at,
        "updated_at": Project.updated_at,
        "last_activity_at": Project.updated_at,
    }
    return allowed.get(order_by, Project.id)


async def _resolve_commit_ref(project: Project, ref: str) -> str:
    if not project.disk_path or not os.path.isdir(project.disk_path):
        raise HTTPException(status_code=404, detail="404 Project Not Found")
    try:
        return (
            await _git_text(project.disk_path, "rev-parse", f"{unquote(ref)}^{{commit}}")
        ).strip()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail="Invalid reference name") from exc


async def _validate_branch_name(project: Project, branch_name: str) -> None:
    if not project.disk_path or not os.path.isdir(project.disk_path):
        raise HTTPException(status_code=404, detail="404 Project Not Found")
    try:
        await _git_text(project.disk_path, "check-ref-format", "--branch", branch_name)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail="Invalid branch name") from exc


async def _tag_jsons(project: Project, base_url: str) -> list[dict]:
    if not project.disk_path or not os.path.isdir(project.disk_path):
        return []
    lines = await _git_lines(
        project.disk_path,
        "for-each-ref",
        "--format=%(refname:short) %(objectname)",
        "refs/tags/",
    )
    tags = []
    for line in lines:
        name, _, sha = line.partition(" ")
        if not name or not sha:
            continue
        tags.append(_tag_json(project, name, sha, base_url))
    return tags


def _tag_json(project: Project, name: str, sha: str, base_url: str) -> dict:
    return {
        "name": name,
        "message": None,
        "target": sha,
        "commit": {
            "id": sha,
            "short_id": sha[:8],
            "created_at": None,
            "parent_ids": [],
            "title": "",
            "message": "",
            "author_name": "",
            "author_email": "",
            "authored_date": None,
            "committer_name": "",
            "committer_email": "",
            "committed_date": None,
            "trailers": {},
            "extended_trailers": {},
            "web_url": f"{base_url}/{project.full_name}/-/commit/{sha}",
        },
        "release": None,
        "protected": False,
        "created_at": None,
    }


async def _get_tag_json(project: Project, tag_name: str, base_url: str) -> dict:
    if not project.disk_path or not os.path.isdir(project.disk_path):
        raise HTTPException(status_code=404, detail="404 Project Not Found")
    decoded_name = unquote(tag_name)
    try:
        sha = (
            await _git_text(
                project.disk_path,
                "rev-parse",
                f"refs/tags/{decoded_name}^{{commit}}",
            )
        ).strip()
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="404 Tag Not Found") from exc
    return _tag_json(project, decoded_name, sha, base_url)


async def _validate_tag_name(project: Project, tag_name: str) -> None:
    if not project.disk_path or not os.path.isdir(project.disk_path):
        raise HTTPException(status_code=404, detail="404 Project Not Found")
    try:
        await _git_text(project.disk_path, "check-ref-format", f"refs/tags/{tag_name}")
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail="Invalid tag name") from exc


@router.post("/projects", status_code=201)
async def create_project(body: dict, user: AuthUser, db: DbSession):
    """Create a GitLab-shaped project backed by a bare git repository."""
    name = body.get("name")
    path = str(body.get("path") or name).strip("/")
    if not name and not path:
        raise HTTPException(status_code=422, detail="name or path is required")
    if not name:
        name = path

    namespace_path, owner_type = await _resolve_project_namespace(body, user, db)
    full_name = f"{namespace_path}/{path}"
    existing = await db.execute(
        select(Project).where(Project.full_name == full_name)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=400, detail="Project already exists")

    visibility, private = _visibility_from_body(body)
    default_branch = str(body.get("default_branch") or "main")
    disk_path = os.path.join(settings.DATA_DIR, "repos", namespace_path, f"{path}.git")
    project = Project(
        owner_id=user.id,
        owner_type=owner_type,
        name=path,
        full_name=full_name,
        description=body.get("description"),
        private=private,
        default_branch=default_branch,
        disk_path=disk_path,
        visibility=visibility,
        has_issues=body.get("issues_enabled", True),
        has_wiki=body.get("wiki_enabled", True),
        has_projects=True,
        has_downloads=True,
    )

    db.add(project)
    await db.commit()
    await db.refresh(project)

    await _init_bare_repo(disk_path, project.default_branch)
    if body.get("initialize_with_readme"):
        commit_sha = await create_initial_commit(
            disk_path,
            project.default_branch,
            name,
            user.name or user.login,
            user.email or f"{user.login}@gitlab-emulator.local",
        )
        if commit_sha:
            project.pushed_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(project)

    return await _project_json(project, settings.BASE_URL, db)


@router.get("/projects/{project_ref:path}/repository/branches")
async def list_project_branches(
    project_ref: str,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List repository branches for a GitLab project."""
    project = await _get_project_or_404(project_ref, db, current_user)
    branches = await get_disk_branches(project.disk_path) if project.disk_path else []
    start = (page - 1) * per_page
    end = start + per_page
    items = []
    for branch in branches[start:end]:
        record = await _get_branch_record(project, branch["name"], db)
        items.append(
            _branch_json(
                project,
                branch,
                settings.BASE_URL,
                protected=bool(record and record.protected),
            )
        )
    return paginated_json(items, request, page, per_page, len(branches))


@router.post("/projects/{project_ref:path}/repository/branches", status_code=201)
async def create_project_branch(
    project_ref: str,
    user: AuthUser,
    db: DbSession,
    body: dict | None = Body(None),
    branch: str | None = Query(None),
    ref: str | None = Query(None),
):
    """Create a repository branch for a GitLab project."""
    project = await _get_project_or_404(project_ref, db, user)
    payload = body or {}
    branch_name = unquote(str(payload.get("branch") or branch or "")).strip()
    source_ref = str(payload.get("ref") or ref or "").strip()
    if not branch_name:
        raise HTTPException(status_code=400, detail="branch is required")
    if not source_ref:
        raise HTTPException(status_code=400, detail="ref is required")

    await _validate_branch_name(project, branch_name)
    try:
        await _get_branch_json(project, branch_name, settings.BASE_URL)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
    else:
        raise HTTPException(status_code=400, detail="Branch already exists")

    sha = await _resolve_commit_ref(project, source_ref)
    try:
        await _git_text(project.disk_path, "update-ref", f"refs/heads/{branch_name}", sha)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail="Could not create branch") from exc
    return await _get_branch_json(project, branch_name, settings.BASE_URL)


@router.get("/projects/{project_ref:path}/repository/branches/{branch_name:path}")
async def get_project_branch(
    project_ref: str,
    branch_name: str,
    db: DbSession,
    current_user: CurrentUser,
):
    """Get one repository branch for a GitLab project."""
    project = await _get_project_or_404(project_ref, db, current_user)
    return await _get_branch_json(project, branch_name, settings.BASE_URL, db)


@router.delete("/projects/{project_ref:path}/repository/branches/{branch_name:path}")
async def delete_project_branch(
    project_ref: str,
    branch_name: str,
    user: AuthUser,
    db: DbSession,
):
    """Delete a repository branch for a GitLab project."""
    project = await _get_project_or_404(project_ref, db, user)
    decoded_name = unquote(branch_name)
    if decoded_name == project.default_branch:
        raise HTTPException(status_code=400, detail="Cannot delete default branch")
    await _get_branch_json(project, decoded_name, settings.BASE_URL)
    try:
        await _git_text(project.disk_path, "update-ref", "-d", f"refs/heads/{decoded_name}")
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail="Could not delete branch") from exc
    return {"branch_name": decoded_name}


@router.get("/projects/{project_ref:path}/repository/tags")
async def list_project_tags(
    project_ref: str,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List repository tags for a GitLab project."""
    project = await _get_project_or_404(project_ref, db, current_user)
    tags = await _tag_jsons(project, settings.BASE_URL)
    start = (page - 1) * per_page
    end = start + per_page
    return paginated_json(tags[start:end], request, page, per_page, len(tags))


@router.post("/projects/{project_ref:path}/repository/tags", status_code=201)
async def create_project_tag(
    project_ref: str,
    user: AuthUser,
    db: DbSession,
    body: dict | None = Body(None),
    tag_name: str | None = Query(None),
    ref: str | None = Query(None),
):
    """Create a lightweight repository tag for a GitLab project."""
    project = await _get_project_or_404(project_ref, db, user)
    payload = body or {}
    name = unquote(str(payload.get("tag_name") or tag_name or "")).strip()
    source_ref = str(payload.get("ref") or ref or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="tag_name is required")
    if not source_ref:
        raise HTTPException(status_code=400, detail="ref is required")

    await _validate_tag_name(project, name)
    try:
        await _get_tag_json(project, name, settings.BASE_URL)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
    else:
        raise HTTPException(status_code=400, detail="Tag already exists")

    sha = await _resolve_commit_ref(project, source_ref)
    try:
        await _git_text(project.disk_path, "update-ref", f"refs/tags/{name}", sha)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail="Could not create tag") from exc
    return await _get_tag_json(project, name, settings.BASE_URL)


@router.get("/projects/{project_ref:path}/repository/tags/{tag_name:path}")
async def get_project_tag(
    project_ref: str,
    tag_name: str,
    db: DbSession,
    current_user: CurrentUser,
):
    """Get one repository tag for a GitLab project."""
    project = await _get_project_or_404(project_ref, db, current_user)
    return await _get_tag_json(project, tag_name, settings.BASE_URL)


@router.delete("/projects/{project_ref:path}/repository/tags/{tag_name:path}")
async def delete_project_tag(
    project_ref: str,
    tag_name: str,
    user: AuthUser,
    db: DbSession,
):
    """Delete a repository tag for a GitLab project."""
    project = await _get_project_or_404(project_ref, db, user)
    decoded_name = unquote(tag_name)
    await _get_tag_json(project, decoded_name, settings.BASE_URL)
    try:
        await _git_text(project.disk_path, "update-ref", "-d", f"refs/tags/{decoded_name}")
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail="Could not delete tag") from exc
    return {"tag_name": decoded_name}


@router.get("/projects/{project_ref:path}/protected_branches")
async def list_project_protected_branches(
    project_ref: str,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List protected branches for a GitLab project."""
    project = await _get_project_or_404(project_ref, db, current_user)
    query = (
        select(Branch)
        .options(selectinload(Branch.protection), selectinload(Branch.repository))
        .where(Branch.repo_id == project.id, Branch.protected.is_(True))
        .order_by(Branch.name)
    )
    total = (
        await db.execute(select(sa_func.count()).select_from(query.subquery()))
    ).scalar() or 0
    result = await db.execute(query.offset((page - 1) * per_page).limit(per_page))
    return paginated_json(
        [
            _protected_branch_json(branch, settings.BASE_URL, project)
            for branch in result.scalars().all()
        ],
        request,
        page,
        per_page,
        total,
    )


@router.post("/projects/{project_ref:path}/protected_branches", status_code=201)
async def protect_project_branch(
    project_ref: str,
    user: AuthUser,
    db: DbSession,
    body: dict | None = Body(None),
    name: str | None = Query(None),
    push_access_level: int | None = Query(None),
    merge_access_level: int | None = Query(None),
    unprotect_access_level: int | None = Query(None),
    allow_force_push: bool | None = Query(None),
):
    """Protect a branch for a GitLab project."""
    project = await _get_project_or_404(project_ref, db, user)
    payload = body or {}
    branch_name = unquote(str(payload.get("name") or name or "")).strip()
    if not branch_name:
        raise HTTPException(status_code=400, detail="name is required")

    branch_json = await _get_branch_json(project, branch_name, settings.BASE_URL, db)
    record = await _get_branch_record(project, branch_name, db)
    if record is None:
        record = Branch(
            repo_id=project.id,
            name=branch_name,
            sha=branch_json["commit"]["id"],
            protected=True,
        )
        db.add(record)
        await db.flush()
    else:
        record.sha = branch_json["commit"]["id"]
        record.protected = True

    push_value = payload.get("push_access_level", push_access_level)
    merge_value = payload.get("merge_access_level", merge_access_level)
    unprotect_value = payload.get("unprotect_access_level", unprotect_access_level)
    restrictions = {
        "push_access_levels": _access_levels(
            payload.get("allowed_to_push", push_value),
            40,
        ),
        "merge_access_levels": _access_levels(
            payload.get("allowed_to_merge", merge_value),
            40,
        ),
        "unprotect_access_levels": _access_levels(
            payload.get("allowed_to_unprotect", unprotect_value),
            40,
        ),
        "allow_force_push": bool(
            payload.get(
                "allow_force_push",
                False if allow_force_push is None else allow_force_push,
            )
        ),
        "code_owner_approval_required": bool(
            payload.get("code_owner_approval_required", False)
        ),
    }
    protection_result = await db.execute(
        select(BranchProtection).where(BranchProtection.branch_id == record.id)
    )
    protection = protection_result.scalar_one_or_none()
    if protection is None:
        db.add(
            BranchProtection(
                branch_id=record.id,
                required_status_checks={},
                enforce_admins=False,
                required_pull_request_reviews={},
                restrictions=restrictions,
            )
        )
    else:
        protection.required_status_checks = protection.required_status_checks or {}
        protection.enforce_admins = bool(protection.enforce_admins)
        protection.required_pull_request_reviews = (
            protection.required_pull_request_reviews or {}
        )
        protection.restrictions = restrictions
    await db.commit()
    record = await _get_branch_record(project, branch_name, db)
    return _protected_branch_json(record, settings.BASE_URL, project)


@router.get("/projects/{project_ref:path}/protected_branches/{branch_name:path}")
async def get_project_protected_branch(
    project_ref: str,
    branch_name: str,
    db: DbSession,
    current_user: CurrentUser,
):
    """Get one protected branch for a GitLab project."""
    project = await _get_project_or_404(project_ref, db, current_user)
    decoded_name = unquote(branch_name)
    record = await _get_branch_record(project, decoded_name, db)
    if record is None or not record.protected:
        raise HTTPException(status_code=404, detail="404 Protected Branch Not Found")
    return _protected_branch_json(record, settings.BASE_URL, project)


@router.delete(
    "/projects/{project_ref:path}/protected_branches/{branch_name:path}",
    status_code=204,
)
async def unprotect_project_branch(
    project_ref: str,
    branch_name: str,
    user: AuthUser,
    db: DbSession,
):
    """Unprotect a branch for a GitLab project."""
    project = await _get_project_or_404(project_ref, db, user)
    decoded_name = unquote(branch_name)
    record = await _get_branch_record(project, decoded_name, db)
    if record is None or not record.protected:
        raise HTTPException(status_code=404, detail="404 Protected Branch Not Found")
    record.protected = False
    if record.protection is not None:
        await db.delete(record.protection)
    await db.commit()
    return Response(status_code=204)


@router.get("/projects/{project_ref:path}/variables")
async def list_project_variables(
    project_ref: str,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
):
    """List project CI/CD variables."""
    project = await _get_project_or_404(project_ref, db, user)
    _require_project_owner(project, user)
    query = select(CiVariable).where(
        CiVariable.scope_type == "project",
        CiVariable.scope_id == project.id,
    )
    if environment_scope is not None:
        query = query.where(CiVariable.environment_scope == environment_scope)
    query = query.order_by(CiVariable.key, CiVariable.environment_scope)
    variables = (await db.execute(query)).scalars().all()
    return [_variable_json(variable) for variable in variables]


@router.post("/projects/{project_ref:path}/variables", status_code=201)
async def create_project_variable(
    project_ref: str,
    body: ProjectVariableCreate,
    user: AuthUser,
    db: DbSession,
):
    """Create a project CI/CD variable."""
    project = await _get_project_or_404(project_ref, db, user)
    _require_project_owner(project, user)
    variable = CiVariable(
        scope_type="project",
        scope_id=project.id,
        key=_validate_variable_key(body.key),
        value=body.value,
        variable_type=_validate_variable_type(body.variable_type),
        visibility=_variable_visibility(body.masked, body.hidden),
        protected=body.protected,
        raw=body.raw,
        environment_scope=body.environment_scope or "*",
        description=body.description,
    )
    db.add(variable)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Variable already exists") from exc
    await db.refresh(variable)
    return _variable_json(variable)


@router.get("/projects/{project_ref:path}/variables/{key}")
async def get_project_variable(
    project_ref: str,
    key: str,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
):
    """Get one project CI/CD variable."""
    project = await _get_project_or_404(project_ref, db, user)
    _require_project_owner(project, user)
    variable = await _get_project_variable_or_404(project, db, key, environment_scope)
    return _variable_json(variable)


@router.put("/projects/{project_ref:path}/variables/{key}")
async def update_project_variable(
    project_ref: str,
    key: str,
    body: ProjectVariableUpdate,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
):
    """Update one project CI/CD variable."""
    project = await _get_project_or_404(project_ref, db, user)
    _require_project_owner(project, user)
    variable = await _get_project_variable_or_404(project, db, key, environment_scope)
    if body.value is not None:
        variable.value = body.value
    if body.variable_type is not None:
        variable.variable_type = _validate_variable_type(body.variable_type)
    if body.protected is not None:
        variable.protected = body.protected
    if body.raw is not None:
        variable.raw = body.raw
    if body.description is not None:
        variable.description = body.description
    if body.masked is not None or body.hidden is not None:
        current_masked = variable.visibility in {"masked", "masked_and_hidden"}
        current_hidden = variable.visibility == "masked_and_hidden"
        variable.visibility = _variable_visibility(
            current_masked if body.masked is None else body.masked,
            current_hidden if body.hidden is None else body.hidden,
        )
    if body.environment_scope is not None:
        variable.environment_scope = body.environment_scope or "*"
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Variable already exists") from exc
    await db.refresh(variable)
    return _variable_json(variable)


@router.delete("/projects/{project_ref:path}/variables/{key}", status_code=204)
async def delete_project_variable(
    project_ref: str,
    key: str,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
):
    """Delete one project CI/CD variable."""
    project = await _get_project_or_404(project_ref, db, user)
    _require_project_owner(project, user)
    variable = await _get_project_variable_or_404(project, db, key, environment_scope)
    await db.delete(variable)
    await db.commit()
    return Response(status_code=204)


@router.get("/projects/{project_ref:path}/secrets")
async def list_project_secrets(
    project_ref: str,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
    branch_scope: str | None = Query(None, alias="filter[branch_scope]"),
):
    """List project CI/CD secrets."""
    project = await _get_project_or_404(project_ref, db, user)
    _require_project_owner(project, user)
    query = select(CiSecret).where(
        CiSecret.scope_type == "project",
        CiSecret.scope_id == project.id,
    )
    if environment_scope is not None:
        query = query.where(CiSecret.environment_scope == environment_scope)
    if branch_scope is not None:
        query = query.where(CiSecret.branch_scope == branch_scope)
    query = query.order_by(CiSecret.name, CiSecret.environment_scope, CiSecret.branch_scope)
    secrets = (await db.execute(query)).scalars().all()
    return [_secret_json(secret) for secret in secrets]


@router.post("/projects/{project_ref:path}/secrets", status_code=201)
async def create_project_secret(
    project_ref: str,
    body: ProjectSecretCreate,
    user: AuthUser,
    db: DbSession,
):
    """Create a project CI/CD secret."""
    project = await _get_project_or_404(project_ref, db, user)
    _require_project_owner(project, user)
    secret = CiSecret(
        scope_type="project",
        scope_id=project.id,
        name=_validate_secret_name(body.name),
        value=body.value,
        description=body.description,
        environment_scope=body.environment_scope or "*",
        branch_scope=body.branch_scope or "*",
        protected=body.protected,
        rotation_reminder_days=body.rotation_reminder_days,
        status=body.status or "healthy",
    )
    db.add(secret)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Secret already exists") from exc
    await db.refresh(secret)
    return _secret_json(secret)


@router.get("/projects/{project_ref:path}/secrets/{name}")
async def get_project_secret(
    project_ref: str,
    name: str,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
    branch_scope: str | None = Query(None, alias="filter[branch_scope]"),
):
    """Get a project CI/CD secret by name."""
    project = await _get_project_or_404(project_ref, db, user)
    _require_project_owner(project, user)
    secret = await _get_project_secret_or_404(
        project,
        db,
        name,
        environment_scope,
        branch_scope,
    )
    return _secret_json(secret)


@router.put("/projects/{project_ref:path}/secrets/{name}")
async def update_project_secret(
    project_ref: str,
    name: str,
    body: ProjectSecretUpdate,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
    branch_scope: str | None = Query(None, alias="filter[branch_scope]"),
):
    """Update a project CI/CD secret."""
    project = await _get_project_or_404(project_ref, db, user)
    _require_project_owner(project, user)
    secret = await _get_project_secret_or_404(
        project,
        db,
        name,
        environment_scope,
        branch_scope,
    )
    if body.value is not None:
        secret.value = body.value
    if body.description is not None:
        secret.description = body.description
    if body.environment_scope is not None:
        secret.environment_scope = body.environment_scope or "*"
    if body.branch_scope is not None:
        secret.branch_scope = body.branch_scope or "*"
    if body.protected is not None:
        secret.protected = body.protected
    if body.rotation_reminder_days is not None:
        secret.rotation_reminder_days = body.rotation_reminder_days
    if body.status is not None:
        secret.status = body.status or "healthy"
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Secret already exists") from exc
    await db.refresh(secret)
    return _secret_json(secret)


@router.delete("/projects/{project_ref:path}/secrets/{name}", status_code=204)
async def delete_project_secret(
    project_ref: str,
    name: str,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
    branch_scope: str | None = Query(None, alias="filter[branch_scope]"),
):
    """Delete a project CI/CD secret."""
    project = await _get_project_or_404(project_ref, db, user)
    _require_project_owner(project, user)
    secret = await _get_project_secret_or_404(
        project,
        db,
        name,
        environment_scope,
        branch_scope,
    )
    await db.delete(secret)
    await db.commit()
    return Response(status_code=204)


@router.get("/users/{user_id}/projects")
async def list_user_projects(
    user_id: int,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List GitLab projects owned by a user."""
    result = await db.execute(select(User).where(User.id == user_id))
    owner = result.scalar_one_or_none()
    if owner is None:
        raise HTTPException(status_code=404, detail="404 User Not Found")

    query = select(Project).where(Project.owner_id == owner.id)
    if current_user is None or (
        current_user.id != owner.id and not current_user.site_admin
    ):
        query = query.where(Project.private == False)
    query = query.order_by(Project.id)
    total = (
        await db.execute(select(sa_func.count()).select_from(query.subquery()))
    ).scalar() or 0
    query = query.offset((page - 1) * per_page).limit(per_page)
    projects = (await db.execute(query)).scalars().all()
    return paginated_json(
        [await _project_json(project, settings.BASE_URL, db) for project in projects],
        request,
        page,
        per_page,
        total,
    )


@router.get("/projects")
async def list_projects(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    search: str | None = Query(None),
    membership: bool = Query(False),
    owned: bool = Query(False),
    archived: bool | None = Query(None),
    visibility: str | None = Query(None),
    with_issues_enabled: bool | None = Query(None),
    order_by: str = Query("id"),
    sort: str = Query("asc"),
    ids: list[int] | None = Query(None),
    simple: bool = Query(False),
    include_pending_delete: bool = Query(False),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List GitLab projects visible to the current user."""
    query = select(Project)
    if current_user is None:
        query = query.where(Project.private == False)
    elif (membership or owned) and not current_user.site_admin:
        query = query.where(Project.owner_id == current_user.id)
    elif owned and current_user is not None:
        query = query.where(Project.owner_id == current_user.id)
    if ids:
        query = query.where(Project.id.in_(ids))
    if search:
        query = query.where(
            Project.name.ilike(f"%{search}%")
            | Project.full_name.ilike(f"%{search}%")
            | Project.description.ilike(f"%{search}%")
        )
    if archived is not None:
        query = query.where(Project.archived == archived)
    if visibility:
        query = query.where(Project.visibility == visibility)
    if with_issues_enabled is not None:
        query = query.where(Project.has_issues == with_issues_enabled)
    order_col = _project_order_column(order_by)
    query = query.order_by(order_col.desc() if sort == "desc" else order_col.asc())
    total = (
        await db.execute(select(sa_func.count()).select_from(query.subquery()))
    ).scalar() or 0
    query = query.offset((page - 1) * per_page).limit(per_page)
    projects = (await db.execute(query)).scalars().all()
    return paginated_json(
        [await _project_json(project, settings.BASE_URL, db) for project in projects],
        request,
        page,
        per_page,
        total,
    )


@router.delete("/projects/{project_ref:path}", status_code=202)
async def delete_project(project_ref: str, user: AuthUser, db: DbSession):
    """Delete a GitLab project and its backing bare repository."""
    project = await _get_project_or_404(project_ref, db, user)
    if not user.site_admin and project.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    disk_path = project.disk_path
    await db.delete(project)
    await db.commit()
    if disk_path and os.path.isdir(disk_path):
        shutil.rmtree(disk_path, ignore_errors=True)
    return {"message": "202 Accepted"}


@router.get("/projects/{project_ref:path}")
async def get_project(project_ref: str, db: DbSession, current_user: CurrentUser):
    """Get a GitLab project by numeric ID or URL-encoded path_with_namespace."""
    project = await _get_project_or_404(project_ref, db, current_user)
    return await _project_json(project, settings.BASE_URL, db)
