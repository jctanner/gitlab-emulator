"""Public web frontend routes for browsing repos, issues, PRs, and code."""

import os
import re
import shutil
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jose import JWSError, jws
from sqlalchemy import select, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.pipelines import (
    CreatePipelineRequest,
    PipelineVariable,
    _create_pipeline,
    _derive_pipeline_status,
    _reset_job_for_retry,
)
from app.api.runner import explain_job_scheduling, registered_runner_diagnostics
from app.api.releases import _ensure_release_tag
from app.config import settings
from app.database import get_db
from app.git.bare_repo import (
    delete_file,
    get_branches,
    get_commit_count,
    get_commit_diff,
    get_commit_info,
    get_file_content,
    get_log,
    get_tags,
    list_tree,
    write_file,
)
from app.models.comment import IssueComment
from app.models.ci import (
    CiSecret,
    CiVariable,
    JobArtifact,
    Pipeline,
    PipelineJob,
    PipelineSchedule,
)
from app.models.deploy_key import DeployKey
from app.models.issue import Issue
from app.models.label import Label
from app.models.milestone import Milestone
from app.models.organization import Organization, OrgMembership
from app.models.pull_request import PullRequest
from app.models.release import Release
from app.models.repository import Collaborator, Repository
from app.models.snippet import Snippet
from app.models.user import User
from app.models.webhook import Webhook
from app.services.auth_service import verify_password
from app.services.ci_security import normalize_ci_security_settings
from app.services.pipeline_schedules import (
    play_pipeline_schedule as materialize_pipeline_schedule,
    set_schedule_next_run,
)
from app.services import issue_service, pr_service, repo_service
from app.services.repo_service import REPO_NAME_PATTERN

_WEB_DIR = os.path.dirname(os.path.abspath(__file__))
_TEMPLATES_DIR = os.path.join(_WEB_DIR, "templates")

templates = Jinja2Templates(directory=_TEMPLATES_DIR)

router = APIRouter(prefix="/ui", tags=["web"])

_URL_PREFIX = "/ui"
_CI_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CI_VARIABLE_TYPES = {"env_var", "file"}
_MEMBER_ACCESS_LEVELS = {
    10: "Guest",
    20: "Reporter",
    30: "Developer",
    40: "Maintainer",
    50: "Owner",
}
_ACCESS_TO_PERMISSION = {
    10: "pull",
    20: "pull",
    30: "push",
    40: "maintain",
    50: "admin",
}
_WEBHOOK_EVENTS = {
    "push_events": "Push events",
    "merge_requests_events": "Merge request events",
    "issues_events": "Issue events",
    "tag_push_events": "Tag push events",
    "note_events": "Comment events",
    "job_events": "Job events",
    "pipeline_events": "Pipeline events",
    "releases_events": "Release events",
}


# ---------------------------------------------------------------------------
# Session helpers (signed cookie via python-jose JWS)
# ---------------------------------------------------------------------------

_ALGORITHM = "HS256"


def _sign_session(username: str) -> str:
    """Create a JWS-signed session token containing the username."""
    return jws.sign(
        username.encode("utf-8"),
        settings.SECRET_KEY,
        algorithm=_ALGORITHM,
    )


def _verify_session(token: str) -> Optional[str]:
    """Verify a JWS session token and return the username, or None."""
    try:
        payload = jws.verify(token, settings.SECRET_KEY, algorithms=[_ALGORITHM])
        return payload.decode("utf-8")
    except (JWSError, Exception):
        return None


async def _get_current_user(
    request: Request, db: AsyncSession
) -> Optional[User]:
    """Extract the logged-in user from the ui_session cookie."""
    token = request.cookies.get("ui_session")
    if not token:
        return None
    username = _verify_session(token)
    if not username:
        return None
    result = await db.execute(select(User).where(User.login == username))
    return result.scalar_one_or_none()


def _ctx(request: Request, **extra) -> dict:
    context = dict(extra)
    context["request"] = request
    context["url_prefix"] = _URL_PREFIX
    # current_user is set by individual route handlers via extra kwargs
    context.setdefault("current_user", None)
    repo = context.get("repo")
    current_user = context.get("current_user")
    if "can_manage_repo" not in context and repo is not None and current_user is not None:
        context["can_manage_repo"] = bool(
            current_user.site_admin
            or (
                getattr(repo, "owner_type", "User") == "User"
                and current_user.id == repo.owner_id
            )
            or getattr(repo, "_ui_can_manage_repo", False)
        )
    return context


async def _can_manage_repo(user: Optional[User], repo: Repository, db: AsyncSession) -> bool:
    """Return whether a UI user can mutate repository settings or source."""
    if not user:
        setattr(repo, "_ui_can_manage_repo", False)
        return False
    if user.site_admin:
        setattr(repo, "_ui_can_manage_repo", True)
        return True
    if repo.owner_type == "User":
        allowed = user.id == repo.owner_id
        setattr(repo, "_ui_can_manage_repo", allowed)
        return allowed
    if repo.owner_type == "Organization":
        result = await db.execute(
            select(OrgMembership.id).where(
                OrgMembership.org_id == repo.owner_id,
                OrgMembership.user_id == user.id,
                OrgMembership.role == "admin",
                OrgMembership.state == "active",
            )
        )
        allowed = result.scalar_one_or_none() is not None
        setattr(repo, "_ui_can_manage_repo", allowed)
        return allowed
    setattr(repo, "_ui_can_manage_repo", False)
    return False


def _ci_visibility(masked: bool, hidden: bool) -> str:
    if hidden:
        return "masked_and_hidden"
    if masked:
        return "masked"
    return "visible"


def _ci_variable_flags(variable: CiVariable) -> dict:
    return {
        "masked": variable.visibility in {"masked", "masked_and_hidden"},
        "hidden": variable.visibility == "masked_and_hidden",
    }


def _validate_ci_key(value: str, label: str = "Key") -> str:
    normalized = (value or "").strip()
    if not _CI_KEY_RE.match(normalized):
        raise ValueError(f"{label} must start with a letter or underscore and contain only letters, numbers, and underscores.")
    return normalized


def _bool_form(value: str | None) -> bool:
    return value == "1"


def _access_level_label(level: int | None) -> str:
    return _MEMBER_ACCESS_LEVELS.get(int(level or 20), f"Access level {level}")


def _normalize_access_level(value: str | int | None) -> int:
    try:
        access_level = int(value or 20)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid access level.") from exc
    if access_level not in _MEMBER_ACCESS_LEVELS:
        raise ValueError("Access level must be Guest, Reporter, Developer, Maintainer, or Owner.")
    return access_level


def _label_color(value: str | None) -> str:
    raw = (value or "6699cc").strip()
    if raw.startswith("#"):
        raw = raw[1:]
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", raw):
        raise ValueError("Label color must be a six-digit hex color.")
    return raw.lower()


def _parse_date_input(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("Due date must be YYYY-MM-DD.") from exc


def _selected_webhook_events(events: list[str] | None) -> dict[str, bool]:
    selected = set(events or [])
    return {event: event in selected for event in _WEBHOOK_EVENTS}


def _webhook_events_from_form(events: list[str] | None) -> list[str]:
    selected = [event for event in (events or []) if event in _WEBHOOK_EVENTS]
    return selected or ["push_events"]


def _parse_schedule_variables(value: str | None) -> list[dict[str, str]]:
    variables: list[dict[str, str]] = []
    for line in (value or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "=" not in stripped:
            raise ValueError("Schedule variables must use KEY=VALUE lines.")
        key, variable_value = stripped.split("=", 1)
        variables.append(
            {"key": _validate_ci_key(key.strip(), "Variable key"), "value": variable_value}
        )
    return variables


def _schedule_variables_from_form(
    variables_text: str | None,
    variable_key: str | None = None,
    variable_value: str | None = None,
    variable_type: str | None = None,
) -> list[dict[str, str]]:
    variables = _parse_schedule_variables(variables_text)
    key = (variable_key or "").strip()
    if key:
        item = {
            "key": _validate_ci_key(key, "Variable key"),
            "value": variable_value or "",
        }
        if variable_type and variable_type != "variable":
            item["variable_type"] = variable_type
        variables.append(item)
    return variables


def _schedule_variables_text(variables: list | None) -> str:
    lines = []
    for variable in variables or []:
        if not isinstance(variable, dict):
            continue
        key = str(variable.get("key") or "").strip()
        if key:
            lines.append(f"{key}={variable.get('value') or ''}")
    return "\n".join(lines)


def _masked_token(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return f"****{value[-4:]}"


async def _managed_repo_or_response(
    request: Request,
    db: AsyncSession,
    owner: str,
    repo_name: str,
) -> tuple[User | None, Repository | None, HTMLResponse | RedirectResponse | None]:
    current_user = await _get_current_user(request, db)
    if not current_user:
        return None, None, RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return current_user, None, HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    if not await _can_manage_repo(current_user, repo, db):
        return current_user, repo, HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)
    return current_user, repo, None


async def _namespace_options(db: AsyncSession, current_user: User) -> list[dict]:
    """Return user and group namespaces available in the web create flow."""
    options = [
        {
            "kind": "user",
            "path": current_user.login,
            "label": f"{current_user.login} (user)",
            "owner": current_user,
        }
    ]
    query = select(Organization).order_by(Organization.login.asc())
    if not current_user.site_admin:
        query = (
            query.join(OrgMembership, OrgMembership.org_id == Organization.id)
            .where(
                OrgMembership.user_id == current_user.id,
                OrgMembership.role == "admin",
                OrgMembership.state == "active",
            )
        )
    groups = (await db.execute(query)).scalars().all()
    for group in groups:
        options.append(
            {
                "kind": "group",
                "path": group.login,
                "label": f"{group.login} (group)",
                "owner": group,
            }
        )
    return options


async def _resolve_namespace_owner(
    db: AsyncSession, current_user: User, namespace_path: str
) -> User | Organization | None:
    """Resolve a selected namespace into a user or organization owner."""
    normalized = namespace_path.strip("/")
    if normalized == current_user.login:
        return current_user
    result = await db.execute(select(User).where(User.login == normalized))
    user = result.scalar_one_or_none()
    if user is not None and (current_user.site_admin or user.id == current_user.id):
        return user
    query = select(Organization).where(Organization.login == normalized)
    if not current_user.site_admin:
        query = query.join(OrgMembership, OrgMembership.org_id == Organization.id).where(
            OrgMembership.user_id == current_user.id,
            OrgMembership.role == "admin",
            OrgMembership.state == "active",
        )
    return (await db.execute(query)).scalar_one_or_none()


def _repo_ci_redirect(
    owner: str,
    repo_name: str,
    *,
    pipeline_id: int | None = None,
    job_id: int | None = None,
    flash_message: str | None = None,
    flash_type: str = "info",
) -> RedirectResponse:
    if job_id is not None:
        base_url = _repo_job_url(owner, repo_name, job_id)
    elif pipeline_id is not None:
        base_url = _repo_pipeline_url(owner, repo_name, pipeline_id)
    else:
        base_url = f"/ui/{owner}/{repo_name}/-/pipelines"
    params = []
    if flash_message:
        params.append(("flash_message", flash_message))
        params.append(("flash_type", flash_type))
    suffix = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(url=f"{base_url}{suffix}", status_code=302)


def _repo_pipeline_url(owner: str, repo_name: str, pipeline_id: int) -> str:
    return f"/ui/{owner}/{repo_name}/-/pipelines/{pipeline_id}"


def _repo_job_url(owner: str, repo_name: str, job_id: int) -> str:
    return f"/ui/{owner}/{repo_name}/-/jobs/{job_id}"


async def _repo_ref_choices(repo: Repository) -> tuple[list[dict], list[dict]]:
    branches = []
    tags = []
    if repo.disk_path and os.path.isdir(repo.disk_path):
        branches = await get_branches(repo.disk_path)
        tags = await get_tags(repo.disk_path)
    default_branch = repo.default_branch or "main"
    branches.sort(key=lambda b: (0 if b.get("name") == default_branch else 1, b.get("name") or ""))
    tags.sort(key=lambda t: t.get("name") or "")
    return branches, tags


async def _job_scheduling_diagnostics(
    db: AsyncSession, jobs: list[PipelineJob]
) -> dict[int, dict]:
    if not jobs:
        return {}
    runner_diagnostics = await registered_runner_diagnostics(db)
    jobs_by_pipeline: dict[int | None, list[PipelineJob]] = {}
    for job in jobs:
        jobs_by_pipeline.setdefault(job.pipeline_id, []).append(job)

    diagnostics: dict[int, dict] = {}
    for pipeline_jobs in jobs_by_pipeline.values():
        diagnostics.update(explain_job_scheduling(pipeline_jobs, runner_diagnostics))
    return diagnostics


async def _downstream_pipeline_context(
    db: AsyncSession, jobs: list[PipelineJob]
) -> dict[int, dict]:
    downstream_ids = [
        job.downstream_pipeline_id
        for job in jobs
        if job.downstream_pipeline_id is not None
    ]
    if not downstream_ids:
        return {}
    downstream_pipelines = list(
        (
            await db.execute(
                select(Pipeline)
                .options(
                    selectinload(Pipeline.project),
                    selectinload(Pipeline.jobs),
                )
                .where(Pipeline.id.in_(downstream_ids))
            )
        )
        .scalars()
        .all()
    )
    downstream_by_id = {pipeline.id: pipeline for pipeline in downstream_pipelines}
    context: dict[int, dict] = {}
    for job in jobs:
        if job.downstream_pipeline_id is None:
            continue
        downstream = downstream_by_id.get(job.downstream_pipeline_id)
        if downstream is None:
            continue
        downstream_jobs = sorted(
            downstream.jobs, key=lambda item: (item.stage_index, item.id)
        )
        diagnostics = await _job_scheduling_diagnostics(db, downstream_jobs)
        blocked_reasons = []
        for diagnostic in diagnostics.values():
            if diagnostic.get("blocked"):
                blocked_reasons.extend(diagnostic.get("reasons") or [])
        context[job.id] = {
            "pipeline": downstream,
            "jobs": downstream_jobs,
            "diagnostics": diagnostics,
            "blocked_reasons": list(dict.fromkeys(blocked_reasons)),
        }
    return context


def _pipeline_variables_from_form(
    variable_key: str,
    variable_value: str,
    variable_type: str,
) -> list[PipelineVariable]:
    key = variable_key.strip()
    if not key:
        return []
    if not _CI_KEY_RE.match(key):
        raise ValueError(f"Invalid variable key: {key}")
    kind = variable_type.strip() or "env_var"
    if kind == "variable":
        kind = "env_var"
    if kind not in _CI_VARIABLE_TYPES:
        raise ValueError(f"Invalid variable type: {kind}")
    return [PipelineVariable(key=key, value=variable_value, variable_type=kind)]


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Login form."""
    current_user = await _get_current_user(request, db)
    if current_user:
        return RedirectResponse(url="/ui/", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context=_ctx(request, error=None),
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Validate credentials and set session cookie."""
    result = await db.execute(select(User).where(User.login == username))
    user = result.scalar_one_or_none()

    if user and verify_password(password, user.hashed_password):
        response = RedirectResponse(url="/ui/", status_code=302)
        response.set_cookie(
            key="ui_session",
            value=_sign_session(username),
            path="/ui",
            httponly=True,
            samesite="lax",
        )
        return response

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context=_ctx(request, error="Invalid username or password."),
    )


@router.get("/logout")
async def logout():
    """Clear session cookie and redirect to landing page."""
    response = RedirectResponse(url="/ui/", status_code=302)
    response.delete_cookie(key="ui_session", path="/ui")
    return response


# ---------------------------------------------------------------------------
# New repository
# ---------------------------------------------------------------------------

@router.get("/new", response_class=HTMLResponse)
async def new_repo_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Form for creating a new repository."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    namespaces = await _namespace_options(db, current_user)
    return templates.TemplateResponse(
        request=request,
        name="new_repo.html",
        context=_ctx(
            request,
            current_user=current_user,
            error=None,
            namespaces=namespaces,
            selected_namespace=current_user.login,
        ),
    )


@router.post("/new", response_class=HTMLResponse)
async def new_repo_submit(
    request: Request,
    name: str = Form(...),
    namespace_path: str = Form(""),
    description: str = Form(""),
    private: bool = Form(False),
    auto_init: bool = Form(False),
    db: AsyncSession = Depends(get_db),
):
    """Create a new repository."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)

    namespaces = await _namespace_options(db, current_user)
    selected_namespace = namespace_path.strip("/") or current_user.login
    owner = await _resolve_namespace_owner(db, current_user, selected_namespace)
    if owner is None:
        return templates.TemplateResponse(
            request=request,
            name="new_repo.html",
            context=_ctx(
                request,
                current_user=current_user,
                error=f"Namespace '{selected_namespace}' is not available.",
                namespaces=namespaces,
                selected_namespace=selected_namespace,
            ),
        )

    try:
        repo = await repo_service.create_repo(
            db,
            owner=owner,
            name=name,
            description=description or None,
            private=private,
            auto_init=auto_init,
        )
        return RedirectResponse(
            url=f"/ui/{repo.full_name}", status_code=302
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="new_repo.html",
            context=_ctx(
                request,
                current_user=current_user,
                error=str(exc),
                namespaces=namespaces,
                selected_namespace=selected_namespace,
            ),
        )


# ---------------------------------------------------------------------------
# Landing page
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def landing(request: Request, db: AsyncSession = Depends(get_db)):
    """Landing page showing recent repositories."""
    current_user = await _get_current_user(request, db)

    result = await db.execute(
        select(Repository).order_by(Repository.updated_at.desc()).limit(20)
    )
    repos = list(result.scalars().all())

    # Attach owner_login for template use
    repo_list = []
    for repo in repos:
        repo.owner_login = repo.owner.login if repo.owner else "unknown"
        repo_list.append(repo)

    return templates.TemplateResponse(
        request=request,
        name="landing.html",
        context=_ctx(request, repos=repo_list, current_user=current_user),
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Search repositories and users."""
    current_user = await _get_current_user(request, db)
    repos = []
    users = []
    query_text = (q or "").strip()
    offset = (page - 1) * per_page
    repo_total = 0
    has_prev = page > 1
    has_next = False

    if query_text:
        pattern = f"%{query_text}%"
        repo_filter = or_(
            Repository.name.ilike(pattern),
            Repository.full_name.ilike(pattern),
            Repository.description.ilike(pattern),
        )
        repo_total = (await db.execute(
            select(func.count(Repository.id)).where(repo_filter)
        )).scalar() or 0
        result = await db.execute(
            select(Repository)
            .where(repo_filter)
            .order_by(Repository.updated_at.desc(), Repository.id.desc())
            .offset(offset)
            .limit(per_page)
        )
        repos = list(result.scalars().all())
        for repo in repos:
            repo.owner_login = repo.owner.login if repo.owner else "unknown"

        result = await db.execute(
            select(User).where(
                or_(
                    User.login.ilike(pattern),
                    User.name.ilike(pattern),
                )
            ).limit(20)
        )
        users = list(result.scalars().all())
    else:
        repo_total = (await db.execute(select(func.count(Repository.id)))).scalar() or 0
        result = await db.execute(
            select(Repository)
            .order_by(Repository.updated_at.desc(), Repository.id.desc())
            .offset(offset)
            .limit(per_page)
        )
        repos = list(result.scalars().all())
        for repo in repos:
            repo.owner_login = repo.owner.login if repo.owner else "unknown"

    has_next = (offset + len(repos)) < repo_total

    def page_url(next_page: int) -> str:
        params = {"page": next_page, "per_page": per_page}
        if query_text:
            params["q"] = query_text
        return f"/ui/search?{urlencode(params)}"

    return templates.TemplateResponse(
        request=request,
        name="search.html",
        context=_ctx(
            request,
            query=query_text,
            repos=repos,
            users=users,
            page=page,
            per_page=per_page,
            repo_total=repo_total,
            has_prev=has_prev,
            has_next=has_next,
            prev_url=page_url(page - 1) if has_prev else None,
            next_url=page_url(page + 1) if has_next else None,
            current_user=current_user,
        ),
    )


# ---------------------------------------------------------------------------
# User / Org profile
# ---------------------------------------------------------------------------

@router.get("/{owner}", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    owner: str,
    db: AsyncSession = Depends(get_db),
):
    """User or organization profile page with their repositories."""
    current_user = await _get_current_user(request, db)

    # Try user first
    result = await db.execute(select(User).where(User.login == owner))
    profile = result.scalar_one_or_none()

    if profile is None:
        # Try organization
        result = await db.execute(
            select(Organization).where(Organization.login == owner)
        )
        profile = result.scalar_one_or_none()

    if profile is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    # Get repos
    result = await db.execute(
        select(Repository).where(
            Repository.owner_id == profile.id
        ).order_by(Repository.updated_at.desc())
    )
    repos = list(result.scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context=_ctx(request, profile=profile, repos=repos, current_user=current_user),
    )


# ---------------------------------------------------------------------------
# Repository overview
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}", response_class=HTMLResponse)
async def repo_page(
    request: Request,
    owner: str,
    repo_name: str,
    db: AsyncSession = Depends(get_db),
):
    """Repository overview with file tree and README."""
    current_user = await _get_current_user(request, db)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    tree_entries = None
    readme_content = None
    default_branch = repo.default_branch or "main"
    commit_count = 0
    branch_count = 0
    tag_count = 0

    if repo.disk_path and os.path.isdir(repo.disk_path):
        tree_entries = await list_tree(repo.disk_path, default_branch)
        if tree_entries:
            # Sort: directories first, then files
            tree_entries.sort(key=lambda e: (0 if e["type"] == "tree" else 1, e["name"]))
            # Try to find and read README
            for entry in tree_entries:
                if entry["name"].lower().startswith("readme"):
                    raw = await get_file_content(
                        repo.disk_path, default_branch, entry["name"]
                    )
                    if raw:
                        try:
                            readme_content = raw.decode("utf-8", errors="replace")
                        except Exception:
                            readme_content = None
                    break

        commit_count = await get_commit_count(repo.disk_path, default_branch)
        branches = await get_branches(repo.disk_path)
        branch_count = len(branches)
        tags = await get_tags(repo.disk_path)
        tag_count = len(tags)

    # Open issue/PR counts for tab counters
    pr_issue_ids = select(PullRequest.issue_id)
    open_issues_count = (await db.execute(
        select(func.count(Issue.id)).where(
            Issue.repo_id == repo.id, Issue.state == "open",
            ~Issue.id.in_(pr_issue_ids),
        )
    )).scalar() or 0

    open_pulls_count = (await db.execute(
        select(func.count(Issue.id)).where(
            Issue.repo_id == repo.id, Issue.state == "open",
            Issue.id.in_(pr_issue_ids),
        )
    )).scalar() or 0

    return templates.TemplateResponse(
        request=request,
        name="repo.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            tree_entries=tree_entries,
            readme_content=readme_content,
            default_branch=default_branch,
            open_issues_count=open_issues_count,
            open_pulls_count=open_pulls_count,
            commit_count=commit_count,
            branch_count=branch_count,
            tag_count=tag_count,
            current_user=current_user,
        ),
    )


# ---------------------------------------------------------------------------
# Repository settings
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/settings", response_class=HTMLResponse)
async def repo_settings_page(
    request: Request,
    owner: str,
    repo_name: str,
    saved: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """Repository settings form for metadata and destructive actions."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    if not await _can_manage_repo(current_user, repo, db):
        return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)

    return templates.TemplateResponse(
        request=request,
        name="repo_settings.html",
        context=_ctx(
            request, owner=owner, repo=repo, repo_name=repo.name,
            current_user=current_user, error=None,
            ci_security_settings=normalize_ci_security_settings(
                repo.ci_security_settings
            ),
            message="Repository settings saved." if saved else None,
        ),
    )


@router.post("/{owner}/{repo_name}/settings", response_class=HTMLResponse)
async def repo_settings_submit(
    request: Request,
    owner: str,
    repo_name: str,
    name: str = Form(...),
    description: str = Form(""),
    default_branch: str = Form("main"),
    private: str = Form(""),
    ci_pipeline_variables_minimum_override_role: str = Form("developer"),
    ci_strict_security_mode: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Update repository metadata from the web UI."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    if not await _can_manage_repo(current_user, repo, db):
        return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)

    new_name = name.strip()
    if not REPO_NAME_PATTERN.match(new_name):
        return templates.TemplateResponse(
            request=request,
            name="repo_settings.html",
            context=_ctx(
                request, owner=owner, repo=repo, repo_name=repo.name,
                current_user=current_user,
                ci_security_settings=normalize_ci_security_settings(
                    repo.ci_security_settings
                ),
                error="Repository name may only contain letters, numbers, dots, dashes, and underscores.",
                message=None,
            ),
        )

    if ci_pipeline_variables_minimum_override_role not in {
        "developer",
        "maintainer",
        "owner",
        "no_one_allowed",
    }:
        return templates.TemplateResponse(
            request=request,
            name="repo_settings.html",
            context=_ctx(
                request, owner=owner, repo=repo, repo_name=repo.name,
                current_user=current_user,
                ci_security_settings=normalize_ci_security_settings(
                    repo.ci_security_settings
                ),
                error="Invalid CI pipeline variable permission.",
                message=None,
            ),
        )

    new_full_name = f"{owner}/{new_name}"
    if new_full_name != repo.full_name:
        existing = (
            await db.execute(select(Repository).where(Repository.full_name == new_full_name))
        ).scalar_one_or_none()
        if existing is not None:
            return templates.TemplateResponse(
                request=request,
                name="repo_settings.html",
                    context=_ctx(
                        request, owner=owner, repo=repo, repo_name=repo.name,
                        current_user=current_user,
                        ci_security_settings=normalize_ci_security_settings(
                            repo.ci_security_settings
                        ),
                        error=f"Repository '{new_full_name}' already exists.",
                        message=None,
                    ),
            )
        old_disk_path = repo.disk_path
        new_disk_path = os.path.join(settings.DATA_DIR, owner, f"{new_name}.git")
        if old_disk_path and old_disk_path != new_disk_path and os.path.isdir(old_disk_path):
            os.makedirs(os.path.dirname(new_disk_path), exist_ok=True)
            if os.path.exists(new_disk_path):
                return templates.TemplateResponse(
                    request=request,
                    name="repo_settings.html",
                    context=_ctx(
                        request, owner=owner, repo=repo, repo_name=repo.name,
                        current_user=current_user,
                        ci_security_settings=normalize_ci_security_settings(
                            repo.ci_security_settings
                        ),
                        error=f"Repository storage already exists for '{new_name}'.",
                        message=None,
                    ),
                )
            shutil.move(old_disk_path, new_disk_path)
        repo.name = new_name
        repo.full_name = new_full_name
        repo.disk_path = new_disk_path

    repo.description = description.strip() or None
    repo.default_branch = default_branch.strip() or "main"
    repo.private = private == "1"
    repo.visibility = "private" if repo.private else "public"
    repo.ci_security_settings = {
        "ci_pipeline_variables_minimum_override_role": (
            ci_pipeline_variables_minimum_override_role
        ),
        "ci_strict_security_mode": ci_strict_security_mode == "1",
    }
    await db.commit()
    await db.refresh(repo)

    return RedirectResponse(url=f"/ui/{owner}/{repo.name}/settings?saved=1", status_code=302)


@router.post("/{owner}/{repo_name}/settings/delete", response_class=HTMLResponse)
async def repo_delete_submit(
    request: Request,
    owner: str,
    repo_name: str,
    confirm_repository: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Delete a repository from the web UI after explicit confirmation."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    if not await _can_manage_repo(current_user, repo, db):
        return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)

    expected = repo.full_name
    if confirm_repository.strip() != expected:
        return templates.TemplateResponse(
            request=request,
            name="repo_settings.html",
            context=_ctx(
                request, owner=owner, repo=repo, repo_name=repo.name,
                current_user=current_user,
                error=f"Type '{expected}' to confirm deletion.",
                message=None,
            ),
        )

    await repo_service.delete_repo(db, repo)
    return RedirectResponse(url="/ui/", status_code=302)


# ---------------------------------------------------------------------------
# Repository CI/CD variables and secrets
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/-/variables", response_class=HTMLResponse)
async def repo_variables_page(
    request: Request,
    owner: str,
    repo_name: str,
    message: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Manage project CI/CD variables."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response

    result = await db.execute(
        select(CiVariable)
        .where(CiVariable.scope_type == "project", CiVariable.scope_id == repo.id)
        .order_by(CiVariable.key, CiVariable.environment_scope)
    )
    variables = result.scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="repo_ci_variables.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            current_user=current_user,
            variables=variables,
            variable_flags=_ci_variable_flags,
            message=message,
            error=error,
        ),
    )


@router.post("/{owner}/{repo_name}/-/variables", response_class=HTMLResponse)
async def repo_variable_create(
    request: Request,
    owner: str,
    repo_name: str,
    key: str = Form(...),
    value: str = Form(...),
    variable_type: str = Form("env_var"),
    environment_scope: str = Form("*"),
    description: str = Form(""),
    masked: str = Form(""),
    hidden: str = Form(""),
    protected: str = Form(""),
    raw: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Create a project CI/CD variable from the web UI."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/variables"
    try:
        normalized_key = _validate_ci_key(key)
        if variable_type not in _CI_VARIABLE_TYPES:
            raise ValueError("Variable type must be env_var or file.")
        variable = CiVariable(
            scope_type="project",
            scope_id=repo.id,
            key=normalized_key,
            value=value,
            variable_type=variable_type,
            visibility=_ci_visibility(_bool_form(masked), _bool_form(hidden)),
            protected=_bool_form(protected),
            raw=_bool_form(raw),
            environment_scope=environment_scope.strip() or "*",
            description=description.strip() or None,
        )
        db.add(variable)
        await db.commit()
    except (ValueError, IntegrityError) as exc:
        await db.rollback()
        message = "Variable already exists for that environment scope." if isinstance(exc, IntegrityError) else str(exc)
        return RedirectResponse(
            url=f"{redirect}?error={urlencode({'x': message})[2:]}",
            status_code=302,
        )
    return RedirectResponse(
        url=f"{redirect}?message={urlencode({'x': 'Variable created.'})[2:]}",
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/variables/{variable_id}/update", response_class=HTMLResponse)
async def repo_variable_update(
    request: Request,
    owner: str,
    repo_name: str,
    variable_id: int,
    value: str = Form(""),
    variable_type: str = Form("env_var"),
    environment_scope: str = Form("*"),
    description: str = Form(""),
    masked: str = Form(""),
    hidden: str = Form(""),
    protected: str = Form(""),
    raw: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Update a project CI/CD variable. Empty value keeps the existing value."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/variables"
    variable = (
        await db.execute(
            select(CiVariable).where(
                CiVariable.id == variable_id,
                CiVariable.scope_type == "project",
                CiVariable.scope_id == repo.id,
            )
        )
    ).scalar_one_or_none()
    if variable is None:
        return RedirectResponse(url=f"{redirect}?error=Variable%20not%20found.", status_code=302)
    try:
        if variable_type not in _CI_VARIABLE_TYPES:
            raise ValueError("Variable type must be env_var or file.")
        if value:
            variable.value = value
        variable.variable_type = variable_type
        variable.environment_scope = environment_scope.strip() or "*"
        variable.description = description.strip() or None
        variable.visibility = _ci_visibility(_bool_form(masked), _bool_form(hidden))
        variable.protected = _bool_form(protected)
        variable.raw = _bool_form(raw)
        await db.commit()
    except (ValueError, IntegrityError) as exc:
        await db.rollback()
        message = "Variable already exists for that environment scope." if isinstance(exc, IntegrityError) else str(exc)
        return RedirectResponse(
            url=f"{redirect}?error={urlencode({'x': message})[2:]}",
            status_code=302,
        )
    return RedirectResponse(
        url=f"{redirect}?message={urlencode({'x': 'Variable updated.'})[2:]}",
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/variables/{variable_id}/delete", response_class=HTMLResponse)
async def repo_variable_delete(
    request: Request,
    owner: str,
    repo_name: str,
    variable_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a project CI/CD variable."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    variable = (
        await db.execute(
            select(CiVariable).where(
                CiVariable.id == variable_id,
                CiVariable.scope_type == "project",
                CiVariable.scope_id == repo.id,
            )
        )
    ).scalar_one_or_none()
    if variable is not None:
        await db.delete(variable)
        await db.commit()
    return RedirectResponse(
        url=f"/ui/{owner}/{repo.name}/-/variables?message={urlencode({'x': 'Variable deleted.'})[2:]}",
        status_code=302,
    )


@router.get("/{owner}/{repo_name}/-/secrets", response_class=HTMLResponse)
async def repo_secrets_page(
    request: Request,
    owner: str,
    repo_name: str,
    message: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Manage project CI/CD secrets."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    result = await db.execute(
        select(CiSecret)
        .where(CiSecret.scope_type == "project", CiSecret.scope_id == repo.id)
        .order_by(CiSecret.name, CiSecret.environment_scope, CiSecret.branch_scope)
    )
    secrets = result.scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="repo_ci_secrets.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            current_user=current_user,
            secrets=secrets,
            message=message,
            error=error,
        ),
    )


@router.post("/{owner}/{repo_name}/-/secrets", response_class=HTMLResponse)
async def repo_secret_create(
    request: Request,
    owner: str,
    repo_name: str,
    name: str = Form(...),
    value: str = Form(...),
    environment_scope: str = Form("*"),
    branch_scope: str = Form("*"),
    description: str = Form(""),
    rotation_reminder_days: str = Form(""),
    protected: str = Form(""),
    status: str = Form("healthy"),
    db: AsyncSession = Depends(get_db),
):
    """Create a project CI/CD secret from the web UI."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/secrets"
    try:
        normalized_name = _validate_ci_key(name, "Name")
        reminder = int(rotation_reminder_days) if rotation_reminder_days.strip() else None
        secret = CiSecret(
            scope_type="project",
            scope_id=repo.id,
            name=normalized_name,
            value=value,
            description=description.strip() or None,
            environment_scope=environment_scope.strip() or "*",
            branch_scope=branch_scope.strip() or "*",
            protected=_bool_form(protected),
            rotation_reminder_days=reminder,
            status=status.strip() or "healthy",
        )
        db.add(secret)
        await db.commit()
    except (ValueError, IntegrityError) as exc:
        await db.rollback()
        message = "Secret already exists for those scopes." if isinstance(exc, IntegrityError) else str(exc)
        return RedirectResponse(
            url=f"{redirect}?error={urlencode({'x': message})[2:]}",
            status_code=302,
        )
    return RedirectResponse(
        url=f"{redirect}?message={urlencode({'x': 'Secret created.'})[2:]}",
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/secrets/{secret_id}/update", response_class=HTMLResponse)
async def repo_secret_update(
    request: Request,
    owner: str,
    repo_name: str,
    secret_id: int,
    value: str = Form(""),
    environment_scope: str = Form("*"),
    branch_scope: str = Form("*"),
    description: str = Form(""),
    rotation_reminder_days: str = Form(""),
    protected: str = Form(""),
    status: str = Form("healthy"),
    db: AsyncSession = Depends(get_db),
):
    """Update a project CI/CD secret. Empty value keeps the existing value."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/secrets"
    secret = (
        await db.execute(
            select(CiSecret).where(
                CiSecret.id == secret_id,
                CiSecret.scope_type == "project",
                CiSecret.scope_id == repo.id,
            )
        )
    ).scalar_one_or_none()
    if secret is None:
        return RedirectResponse(url=f"{redirect}?error=Secret%20not%20found.", status_code=302)
    try:
        if value:
            secret.value = value
        secret.description = description.strip() or None
        secret.environment_scope = environment_scope.strip() or "*"
        secret.branch_scope = branch_scope.strip() or "*"
        secret.protected = _bool_form(protected)
        secret.rotation_reminder_days = (
            int(rotation_reminder_days) if rotation_reminder_days.strip() else None
        )
        secret.status = status.strip() or "healthy"
        await db.commit()
    except (ValueError, IntegrityError) as exc:
        await db.rollback()
        message = "Secret already exists for those scopes." if isinstance(exc, IntegrityError) else str(exc)
        return RedirectResponse(
            url=f"{redirect}?error={urlencode({'x': message})[2:]}",
            status_code=302,
        )
    return RedirectResponse(
        url=f"{redirect}?message={urlencode({'x': 'Secret updated.'})[2:]}",
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/secrets/{secret_id}/delete", response_class=HTMLResponse)
async def repo_secret_delete(
    request: Request,
    owner: str,
    repo_name: str,
    secret_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a project CI/CD secret."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    secret = (
        await db.execute(
            select(CiSecret).where(
                CiSecret.id == secret_id,
                CiSecret.scope_type == "project",
                CiSecret.scope_id == repo.id,
            )
        )
    ).scalar_one_or_none()
    if secret is not None:
        await db.delete(secret)
        await db.commit()
    return RedirectResponse(
        url=f"/ui/{owner}/{repo.name}/-/secrets?message={urlencode({'x': 'Secret deleted.'})[2:]}",
        status_code=302,
    )


# ---------------------------------------------------------------------------
# Repository members
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/-/members", response_class=HTMLResponse)
async def repo_members_page(
    request: Request,
    owner: str,
    repo_name: str,
    message: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Manage direct project members."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response

    result = await db.execute(
        select(Collaborator)
        .where(Collaborator.repo_id == repo.id)
        .order_by(Collaborator.user_id.asc())
    )
    collaborators = result.scalars().all()
    members = [
        {
            "user": repo.owner,
            "access_level": 50,
            "access_label": _access_level_label(50),
            "is_owner": True,
        }
    ]
    for collaborator in collaborators:
        if collaborator.user is None or collaborator.user_id == repo.owner_id:
            continue
        access_level = collaborator.access_level or 20
        members.append(
            {
                "user": collaborator.user,
                "access_level": access_level,
                "access_label": _access_level_label(access_level),
                "is_owner": False,
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="repo_members.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            current_user=current_user,
            members=members,
            access_levels=_MEMBER_ACCESS_LEVELS,
            message=message,
            error=error,
        ),
    )


@router.post("/{owner}/{repo_name}/-/members", response_class=HTMLResponse)
async def repo_member_create(
    request: Request,
    owner: str,
    repo_name: str,
    username: str = Form(...),
    access_level: str = Form("20"),
    db: AsyncSession = Depends(get_db),
):
    """Add or update a direct project member."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/members"
    try:
        level = _normalize_access_level(access_level)
    except ValueError as exc:
        return RedirectResponse(
            url=f"{redirect}?error={urlencode({'x': str(exc)})[2:]}",
            status_code=302,
        )

    target = (
        await db.execute(select(User).where(User.login == username.strip()))
    ).scalar_one_or_none()
    if target is None:
        return RedirectResponse(
            url=f"{redirect}?error={urlencode({'x': 'User not found.'})[2:]}",
            status_code=302,
        )
    if target.id == repo.owner_id:
        return RedirectResponse(
            url=f"{redirect}?error={urlencode({'x': 'The owner is already a project member.'})[2:]}",
            status_code=302,
        )

    collaborator = (
        await db.execute(
            select(Collaborator).where(
                Collaborator.repo_id == repo.id,
                Collaborator.user_id == target.id,
            )
        )
    ).scalar_one_or_none()
    if collaborator is None:
        collaborator = Collaborator(
            repo_id=repo.id,
            user_id=target.id,
        )
        db.add(collaborator)
    collaborator.access_level = level
    collaborator.permission = _ACCESS_TO_PERMISSION[level]
    await db.commit()
    return RedirectResponse(
        url=f"{redirect}?message={urlencode({'x': 'Member saved.'})[2:]}",
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/members/{user_id}/update", response_class=HTMLResponse)
async def repo_member_update(
    request: Request,
    owner: str,
    repo_name: str,
    user_id: int,
    access_level: str = Form("20"),
    db: AsyncSession = Depends(get_db),
):
    """Update a direct project member access level."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/members"
    if user_id == repo.owner_id:
        return RedirectResponse(url=f"{redirect}?error=Owner%20access%20cannot%20be%20changed.", status_code=302)
    try:
        level = _normalize_access_level(access_level)
    except ValueError as exc:
        return RedirectResponse(
            url=f"{redirect}?error={urlencode({'x': str(exc)})[2:]}",
            status_code=302,
        )
    collaborator = (
        await db.execute(
            select(Collaborator).where(
                Collaborator.repo_id == repo.id,
                Collaborator.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if collaborator is None:
        return RedirectResponse(url=f"{redirect}?error=Member%20not%20found.", status_code=302)
    collaborator.access_level = level
    collaborator.permission = _ACCESS_TO_PERMISSION[level]
    await db.commit()
    return RedirectResponse(
        url=f"{redirect}?message={urlencode({'x': 'Member updated.'})[2:]}",
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/members/{user_id}/delete", response_class=HTMLResponse)
async def repo_member_delete(
    request: Request,
    owner: str,
    repo_name: str,
    user_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Remove a direct project member."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/members"
    if user_id == repo.owner_id:
        return RedirectResponse(url=f"{redirect}?error=Owner%20cannot%20be%20removed.", status_code=302)
    collaborator = (
        await db.execute(
            select(Collaborator).where(
                Collaborator.repo_id == repo.id,
                Collaborator.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if collaborator is not None:
        await db.delete(collaborator)
        await db.commit()
    return RedirectResponse(
        url=f"{redirect}?message={urlencode({'x': 'Member removed.'})[2:]}",
        status_code=302,
    )


# ---------------------------------------------------------------------------
# Repository labels and milestones
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/-/labels", response_class=HTMLResponse)
async def repo_labels_page(
    request: Request,
    owner: str,
    repo_name: str,
    message: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Manage project labels."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    labels = (
        await db.execute(
            select(Label).where(Label.repo_id == repo.id).order_by(Label.name.asc())
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="repo_labels.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            current_user=current_user,
            labels=labels,
            message=message,
            error=error,
        ),
    )


@router.post("/{owner}/{repo_name}/-/labels", response_class=HTMLResponse)
async def repo_label_create(
    request: Request,
    owner: str,
    repo_name: str,
    name: str = Form(...),
    color: str = Form("6699cc"),
    description: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Create a project label from the web UI."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/labels"
    try:
        label_name = name.strip()
        if not label_name:
            raise ValueError("Label name is required.")
        label = Label(
            repo_id=repo.id,
            name=label_name,
            color=_label_color(color),
            description=description.strip() or None,
        )
        db.add(label)
        await db.commit()
    except (ValueError, IntegrityError) as exc:
        await db.rollback()
        message = "Label already exists." if isinstance(exc, IntegrityError) else str(exc)
        return RedirectResponse(
            url=f"{redirect}?error={urlencode({'x': message})[2:]}",
            status_code=302,
        )
    return RedirectResponse(
        url=f"{redirect}?message={urlencode({'x': 'Label created.'})[2:]}",
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/labels/{label_id}/update", response_class=HTMLResponse)
async def repo_label_update(
    request: Request,
    owner: str,
    repo_name: str,
    label_id: int,
    name: str = Form(...),
    color: str = Form("6699cc"),
    description: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Update a project label from the web UI."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/labels"
    label = (
        await db.execute(
            select(Label).where(Label.id == label_id, Label.repo_id == repo.id)
        )
    ).scalar_one_or_none()
    if label is None:
        return RedirectResponse(url=f"{redirect}?error=Label%20not%20found.", status_code=302)
    try:
        label_name = name.strip()
        if not label_name:
            raise ValueError("Label name is required.")
        label.name = label_name
        label.color = _label_color(color)
        label.description = description.strip() or None
        await db.commit()
    except (ValueError, IntegrityError) as exc:
        await db.rollback()
        message = "Label already exists." if isinstance(exc, IntegrityError) else str(exc)
        return RedirectResponse(
            url=f"{redirect}?error={urlencode({'x': message})[2:]}",
            status_code=302,
        )
    return RedirectResponse(
        url=f"{redirect}?message={urlencode({'x': 'Label updated.'})[2:]}",
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/labels/{label_id}/delete", response_class=HTMLResponse)
async def repo_label_delete(
    request: Request,
    owner: str,
    repo_name: str,
    label_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a project label from the web UI."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    label = (
        await db.execute(
            select(Label).where(Label.id == label_id, Label.repo_id == repo.id)
        )
    ).scalar_one_or_none()
    if label is not None:
        await db.delete(label)
        await db.commit()
    return RedirectResponse(
        url=f"/ui/{owner}/{repo.name}/-/labels?message={urlencode({'x': 'Label deleted.'})[2:]}",
        status_code=302,
    )


# ---------------------------------------------------------------------------
# Repository snippets
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/-/snippets", response_class=HTMLResponse)
async def repo_snippets_page(
    request: Request,
    owner: str,
    repo_name: str,
    message: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Manage project snippets."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    snippets = (
        await db.execute(
            select(Snippet)
            .where(Snippet.project_id == repo.id)
            .order_by(Snippet.id.desc())
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="repo_snippets.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            current_user=current_user,
            snippets=snippets,
            selected_snippet=None,
            message=message,
            error=error,
        ),
    )


@router.get("/{owner}/{repo_name}/-/snippets/{snippet_id}", response_class=HTMLResponse)
async def repo_snippet_detail_page(
    request: Request,
    owner: str,
    repo_name: str,
    snippet_id: int,
    message: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Show a project snippet."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    snippets = (
        await db.execute(
            select(Snippet)
            .where(Snippet.project_id == repo.id)
            .order_by(Snippet.id.desc())
        )
    ).scalars().all()
    selected = next((snippet for snippet in snippets if snippet.id == snippet_id), None)
    if selected is None:
        return RedirectResponse(
            url=f"/ui/{owner}/{repo.name}/-/snippets?error=Snippet%20not%20found.",
            status_code=302,
        )
    return templates.TemplateResponse(
        request=request,
        name="repo_snippets.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            current_user=current_user,
            snippets=snippets,
            selected_snippet=selected,
            message=message,
            error=error,
        ),
    )


@router.post("/{owner}/{repo_name}/-/snippets", response_class=HTMLResponse)
async def repo_snippet_create(
    request: Request,
    owner: str,
    repo_name: str,
    title: str = Form(...),
    file_name: str = Form("snippet.txt"),
    content: str = Form(...),
    description: str = Form(""),
    visibility: str = Form("private"),
    db: AsyncSession = Depends(get_db),
):
    """Create a project snippet from the web UI."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/snippets"
    try:
        snippet_title = title.strip()
        snippet_file = file_name.strip() or "snippet.txt"
        snippet_content = content.strip("\n")
        snippet_visibility = visibility.strip() or "private"
        if not snippet_title:
            raise ValueError("Title is required.")
        if not snippet_content:
            raise ValueError("Content is required.")
        if snippet_visibility not in {"private", "internal", "public"}:
            raise ValueError("Visibility must be private, internal, or public.")
        snippet = Snippet(
            user_id=current_user.id,
            project_id=repo.id,
            title=snippet_title,
            description=description.strip() or None,
            file_name=snippet_file,
            content=snippet_content,
            visibility=snippet_visibility,
        )
        db.add(snippet)
        await db.commit()
        await db.refresh(snippet)
    except ValueError as exc:
        await db.rollback()
        return RedirectResponse(
            url=f"{redirect}?error={urlencode({'x': str(exc)})[2:]}",
            status_code=302,
        )
    return RedirectResponse(
        url=(
            f"{redirect}/{snippet.id}"
            f"?message={urlencode({'x': 'Snippet created.'})[2:]}"
        ),
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/snippets/{snippet_id}/delete", response_class=HTMLResponse)
async def repo_snippet_delete(
    request: Request,
    owner: str,
    repo_name: str,
    snippet_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a project snippet from the web UI."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    snippet = (
        await db.execute(
            select(Snippet).where(Snippet.id == snippet_id, Snippet.project_id == repo.id)
        )
    ).scalar_one_or_none()
    if snippet is not None:
        await db.delete(snippet)
        await db.commit()
    return RedirectResponse(
        url=f"/ui/{owner}/{repo.name}/-/snippets?message={urlencode({'x': 'Snippet deleted.'})[2:]}",
        status_code=302,
    )


@router.get("/{owner}/{repo_name}/-/milestones", response_class=HTMLResponse)
async def repo_milestones_page(
    request: Request,
    owner: str,
    repo_name: str,
    message: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Manage project milestones."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    milestones = (
        await db.execute(
            select(Milestone)
            .where(Milestone.repo_id == repo.id)
            .order_by(Milestone.number.asc())
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="repo_milestones.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            current_user=current_user,
            milestones=milestones,
            message=message,
            error=error,
        ),
    )


@router.post("/{owner}/{repo_name}/-/milestones", response_class=HTMLResponse)
async def repo_milestone_create(
    request: Request,
    owner: str,
    repo_name: str,
    title: str = Form(...),
    description: str = Form(""),
    state: str = Form("open"),
    due_on: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Create a project milestone from the web UI."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/milestones"
    try:
        milestone_title = title.strip()
        if not milestone_title:
            raise ValueError("Milestone title is required.")
        if state not in {"open", "closed"}:
            raise ValueError("Milestone state must be open or closed.")
        next_number = (
            await db.execute(
                select(func.coalesce(func.max(Milestone.number), 0)).where(
                    Milestone.repo_id == repo.id
                )
            )
        ).scalar() + 1
        closed_at = datetime.now(timezone.utc) if state == "closed" else None
        milestone = Milestone(
            repo_id=repo.id,
            number=next_number,
            title=milestone_title,
            description=description.strip() or None,
            state=state,
            due_on=_parse_date_input(due_on),
            closed_at=closed_at,
        )
        db.add(milestone)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        return RedirectResponse(
            url=f"{redirect}?error={urlencode({'x': str(exc)})[2:]}",
            status_code=302,
        )
    return RedirectResponse(
        url=f"{redirect}?message={urlencode({'x': 'Milestone created.'})[2:]}",
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/milestones/{milestone_id}/update", response_class=HTMLResponse)
async def repo_milestone_update(
    request: Request,
    owner: str,
    repo_name: str,
    milestone_id: int,
    title: str = Form(...),
    description: str = Form(""),
    state: str = Form("open"),
    due_on: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Update a project milestone from the web UI."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/milestones"
    milestone = (
        await db.execute(
            select(Milestone).where(
                Milestone.id == milestone_id,
                Milestone.repo_id == repo.id,
            )
        )
    ).scalar_one_or_none()
    if milestone is None:
        return RedirectResponse(url=f"{redirect}?error=Milestone%20not%20found.", status_code=302)
    try:
        milestone_title = title.strip()
        if not milestone_title:
            raise ValueError("Milestone title is required.")
        if state not in {"open", "closed"}:
            raise ValueError("Milestone state must be open or closed.")
        old_state = milestone.state
        milestone.title = milestone_title
        milestone.description = description.strip() or None
        milestone.state = state
        milestone.due_on = _parse_date_input(due_on)
        if state == "closed" and old_state != "closed":
            milestone.closed_at = datetime.now(timezone.utc)
        elif state == "open":
            milestone.closed_at = None
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        return RedirectResponse(
            url=f"{redirect}?error={urlencode({'x': str(exc)})[2:]}",
            status_code=302,
        )
    return RedirectResponse(
        url=f"{redirect}?message={urlencode({'x': 'Milestone updated.'})[2:]}",
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/milestones/{milestone_id}/delete", response_class=HTMLResponse)
async def repo_milestone_delete(
    request: Request,
    owner: str,
    repo_name: str,
    milestone_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a project milestone from the web UI."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    milestone = (
        await db.execute(
            select(Milestone).where(
                Milestone.id == milestone_id,
                Milestone.repo_id == repo.id,
            )
        )
    ).scalar_one_or_none()
    if milestone is not None:
        await db.delete(milestone)
        await db.commit()
    return RedirectResponse(
        url=f"/ui/{owner}/{repo.name}/-/milestones?message={urlencode({'x': 'Milestone deleted.'})[2:]}",
        status_code=302,
    )


# ---------------------------------------------------------------------------
# Repository releases
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/-/releases", response_class=HTMLResponse)
async def repo_releases_page(
    request: Request,
    owner: str,
    repo_name: str,
    message: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Manage project releases."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    releases = (
        await db.execute(
            select(Release)
            .where(Release.repo_id == repo.id)
            .order_by(Release.created_at.desc(), Release.id.desc())
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="repo_releases.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            current_user=current_user,
            releases=releases,
            message=message,
            error=error,
        ),
    )


@router.post("/{owner}/{repo_name}/-/releases", response_class=HTMLResponse)
async def repo_release_create(
    request: Request,
    owner: str,
    repo_name: str,
    tag_name: str = Form(...),
    name: str = Form(""),
    ref: str = Form(""),
    description: str = Form(""),
    draft: str = Form(""),
    prerelease: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Create a project release from the web UI."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/releases"
    tag = tag_name.strip()
    if not tag:
        return RedirectResponse(url=f"{redirect}?error=Tag%20name%20is%20required.", status_code=302)
    existing = (
        await db.execute(
            select(Release).where(Release.repo_id == repo.id, Release.tag_name == tag)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return RedirectResponse(url=f"{redirect}?error=Release%20already%20exists.", status_code=302)
    try:
        target = ref.strip() or repo.default_branch or "main"
        await _ensure_release_tag(repo, tag, target)
        now = datetime.now(timezone.utc)
        release = Release(
            repo_id=repo.id,
            tag_name=tag,
            target_commitish=target,
            name=name.strip() or tag,
            body=description,
            draft=_bool_form(draft),
            prerelease=_bool_form(prerelease),
            author_id=current_user.id,
            published_at=None if _bool_form(draft) else now,
        )
        db.add(release)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        return RedirectResponse(
            url=f"{redirect}?error={urlencode({'x': str(exc)})[2:]}",
            status_code=302,
        )
    return RedirectResponse(
        url=f"{redirect}?message={urlencode({'x': 'Release created.'})[2:]}",
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/releases/{release_id}/update", response_class=HTMLResponse)
async def repo_release_update(
    request: Request,
    owner: str,
    repo_name: str,
    release_id: int,
    name: str = Form(""),
    description: str = Form(""),
    draft: str = Form(""),
    prerelease: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Update a project release from the web UI."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/releases"
    release = (
        await db.execute(
            select(Release).where(Release.id == release_id, Release.repo_id == repo.id)
        )
    ).scalar_one_or_none()
    if release is None:
        return RedirectResponse(url=f"{redirect}?error=Release%20not%20found.", status_code=302)
    was_draft = release.draft
    release.name = name.strip() or release.tag_name
    release.body = description
    release.draft = _bool_form(draft)
    release.prerelease = _bool_form(prerelease)
    if was_draft and not release.draft and release.published_at is None:
        release.published_at = datetime.now(timezone.utc)
    await db.commit()
    return RedirectResponse(
        url=f"{redirect}?message={urlencode({'x': 'Release updated.'})[2:]}",
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/releases/{release_id}/delete", response_class=HTMLResponse)
async def repo_release_delete(
    request: Request,
    owner: str,
    repo_name: str,
    release_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a project release from the web UI without deleting its git tag."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    release = (
        await db.execute(
            select(Release).where(Release.id == release_id, Release.repo_id == repo.id)
        )
    ).scalar_one_or_none()
    if release is not None:
        await db.delete(release)
        await db.commit()
    return RedirectResponse(
        url=f"/ui/{owner}/{repo.name}/-/releases?message={urlencode({'x': 'Release deleted.'})[2:]}",
        status_code=302,
    )


# ---------------------------------------------------------------------------
# Repository deploy keys
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/-/deploy_keys", response_class=HTMLResponse)
async def repo_deploy_keys_page(
    request: Request,
    owner: str,
    repo_name: str,
    message: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Manage repository deploy keys."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    deploy_keys = (
        await db.execute(
            select(DeployKey)
            .where(DeployKey.repo_id == repo.id)
            .order_by(DeployKey.created_at.desc(), DeployKey.id.desc())
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="repo_deploy_keys.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            current_user=current_user,
            deploy_keys=deploy_keys,
            message=message,
            error=error,
        ),
    )


@router.post("/{owner}/{repo_name}/-/deploy_keys", response_class=HTMLResponse)
async def repo_deploy_key_create(
    request: Request,
    owner: str,
    repo_name: str,
    title: str = Form(...),
    key: str = Form(...),
    read_only: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Create a repository deploy key from the web UI."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/deploy_keys"
    title_value = title.strip()
    key_value = key.strip()
    if not title_value:
        return RedirectResponse(url=f"{redirect}?error=Title%20is%20required.", status_code=302)
    if not key_value:
        return RedirectResponse(url=f"{redirect}?error=Key%20is%20required.", status_code=302)
    deploy_key = DeployKey(
        repo_id=repo.id,
        title=title_value,
        key=key_value,
        read_only=_bool_form(read_only),
        verified=True,
    )
    db.add(deploy_key)
    await db.commit()
    return RedirectResponse(
        url=f"{redirect}?message={urlencode({'x': 'Deploy key added.'})[2:]}",
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/deploy_keys/{key_id}/delete", response_class=HTMLResponse)
async def repo_deploy_key_delete(
    request: Request,
    owner: str,
    repo_name: str,
    key_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a repository deploy key from the web UI."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    deploy_key = (
        await db.execute(
            select(DeployKey).where(DeployKey.id == key_id, DeployKey.repo_id == repo.id)
        )
    ).scalar_one_or_none()
    if deploy_key is not None:
        await db.delete(deploy_key)
        await db.commit()
    return RedirectResponse(
        url=f"/ui/{owner}/{repo.name}/-/deploy_keys?message={urlencode({'x': 'Deploy key removed.'})[2:]}",
        status_code=302,
    )


# ---------------------------------------------------------------------------
# Repository webhooks
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/-/hooks", response_class=HTMLResponse)
async def repo_webhooks_page(
    request: Request,
    owner: str,
    repo_name: str,
    message: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Manage project webhooks."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    hooks = (
        await db.execute(
            select(Webhook)
            .where(Webhook.repo_id == repo.id)
            .order_by(Webhook.created_at.desc(), Webhook.id.desc())
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="repo_webhooks.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            current_user=current_user,
            hooks=hooks,
            webhook_events=_WEBHOOK_EVENTS,
            selected_events=_selected_webhook_events,
            masked_token=_masked_token,
            message=message,
            error=error,
        ),
    )


@router.post("/{owner}/{repo_name}/-/hooks", response_class=HTMLResponse)
async def repo_webhook_create(
    request: Request,
    owner: str,
    repo_name: str,
    url: str = Form(...),
    token: str = Form(""),
    enable_ssl_verification: str = Form(""),
    active: str = Form(""),
    events: list[str] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
):
    """Create a project webhook from the web UI."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/hooks"
    hook_url = url.strip()
    if not hook_url:
        return RedirectResponse(url=f"{redirect}?error=URL%20is%20required.", status_code=302)
    hook = Webhook(
        repo_id=repo.id,
        url=hook_url,
        secret=token.strip() or None,
        content_type="json",
        insecure_ssl=not _bool_form(enable_ssl_verification),
        events=_webhook_events_from_form(events),
        active=_bool_form(active),
    )
    db.add(hook)
    await db.commit()
    return RedirectResponse(
        url=f"{redirect}?message={urlencode({'x': 'Webhook created.'})[2:]}",
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/hooks/{hook_id}/update", response_class=HTMLResponse)
async def repo_webhook_update(
    request: Request,
    owner: str,
    repo_name: str,
    hook_id: int,
    url: str = Form(...),
    token: str = Form(""),
    enable_ssl_verification: str = Form(""),
    active: str = Form(""),
    events: list[str] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
):
    """Update a project webhook from the web UI."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/hooks"
    hook = (
        await db.execute(
            select(Webhook).where(Webhook.id == hook_id, Webhook.repo_id == repo.id)
        )
    ).scalar_one_or_none()
    if hook is None:
        return RedirectResponse(url=f"{redirect}?error=Webhook%20not%20found.", status_code=302)
    hook_url = url.strip()
    if not hook_url:
        return RedirectResponse(url=f"{redirect}?error=URL%20is%20required.", status_code=302)
    hook.url = hook_url
    if token.strip():
        hook.secret = token.strip()
    hook.insecure_ssl = not _bool_form(enable_ssl_verification)
    hook.active = _bool_form(active)
    hook.events = _webhook_events_from_form(events)
    await db.commit()
    return RedirectResponse(
        url=f"{redirect}?message={urlencode({'x': 'Webhook updated.'})[2:]}",
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/hooks/{hook_id}/delete", response_class=HTMLResponse)
async def repo_webhook_delete(
    request: Request,
    owner: str,
    repo_name: str,
    hook_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a project webhook from the web UI."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    hook = (
        await db.execute(
            select(Webhook).where(Webhook.id == hook_id, Webhook.repo_id == repo.id)
        )
    ).scalar_one_or_none()
    if hook is not None:
        await db.delete(hook)
        await db.commit()
    return RedirectResponse(
        url=f"/ui/{owner}/{repo.name}/-/hooks?message={urlencode({'x': 'Webhook deleted.'})[2:]}",
        status_code=302,
    )


# ---------------------------------------------------------------------------
# Repository CI pipelines and jobs
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/-/pipeline_schedules", response_class=HTMLResponse)
async def repo_pipeline_schedules_page(
    request: Request,
    owner: str,
    repo_name: str,
    message: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Manage project pipeline schedules."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    schedules = (
        await db.execute(
            select(PipelineSchedule)
            .options(
                selectinload(PipelineSchedule.owner),
                selectinload(PipelineSchedule.last_pipeline),
            )
            .where(PipelineSchedule.project_id == repo.id)
            .order_by(PipelineSchedule.id.asc())
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="repo_pipeline_schedules.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            current_user=current_user,
            schedules=schedules,
            variables_text=_schedule_variables_text,
            message=message,
            error=error,
        ),
    )


@router.get("/{owner}/{repo_name}/-/pipeline_schedules/new", response_class=HTMLResponse)
async def repo_pipeline_schedule_new_page(
    request: Request,
    owner: str,
    repo_name: str,
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Render the GitLab-style new pipeline schedule page."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    return templates.TemplateResponse(
        request=request,
        name="repo_pipeline_schedule_new.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            current_user=current_user,
            error=error,
        ),
    )


@router.post("/{owner}/{repo_name}/-/pipeline_schedules", response_class=HTMLResponse)
async def repo_pipeline_schedule_create(
    request: Request,
    owner: str,
    repo_name: str,
    description: str = Form(""),
    ref: str = Form("main"),
    cron: str = Form("0 0 * * *"),
    cron_timezone: str = Form("UTC"),
    active: str = Form(""),
    variables_text: str = Form(""),
    variable_type: str = Form("variable"),
    variable_key: str = Form(""),
    variable_value: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Create a project pipeline schedule."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/pipeline_schedules"
    try:
        schedule = PipelineSchedule(
            project_id=repo.id,
            description=description.strip(),
            ref=ref.strip() or repo.default_branch or "main",
            cron=cron.strip() or "0 0 * * *",
            cron_timezone=cron_timezone.strip() or "UTC",
            active=_bool_form(active),
            variables=_schedule_variables_from_form(
                variables_text, variable_key, variable_value, variable_type
            ),
            owner_id=current_user.id if current_user else repo.owner_id,
        )
        set_schedule_next_run(schedule)
        db.add(schedule)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        return RedirectResponse(
            url=f"{redirect}?error={urlencode({'x': str(exc)})[2:]}",
            status_code=302,
        )
    return RedirectResponse(
        url=f"{redirect}?message={urlencode({'x': 'Pipeline schedule created.'})[2:]}",
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/pipeline_schedules/{schedule_id}/update", response_class=HTMLResponse)
async def repo_pipeline_schedule_update(
    request: Request,
    owner: str,
    repo_name: str,
    schedule_id: int,
    description: str = Form(""),
    ref: str = Form("main"),
    cron: str = Form("0 0 * * *"),
    cron_timezone: str = Form("UTC"),
    active: str = Form(""),
    variables_text: str = Form(""),
    variable_type: str = Form("variable"),
    variable_key: str = Form(""),
    variable_value: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Update a project pipeline schedule."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/pipeline_schedules"
    schedule = (
        await db.execute(
            select(PipelineSchedule).where(
                PipelineSchedule.project_id == repo.id,
                PipelineSchedule.id == schedule_id,
            )
        )
    ).scalar_one_or_none()
    if schedule is None:
        return RedirectResponse(url=f"{redirect}?error=Schedule%20not%20found.", status_code=302)
    try:
        schedule.description = description.strip()
        schedule.ref = ref.strip() or repo.default_branch or "main"
        schedule.cron = cron.strip() or "0 0 * * *"
        schedule.cron_timezone = cron_timezone.strip() or "UTC"
        schedule.active = _bool_form(active)
        schedule.variables = _schedule_variables_from_form(
            variables_text, variable_key, variable_value, variable_type
        )
        set_schedule_next_run(schedule)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        return RedirectResponse(
            url=f"{redirect}?error={urlencode({'x': str(exc)})[2:]}",
            status_code=302,
        )
    return RedirectResponse(
        url=f"{redirect}?message={urlencode({'x': 'Pipeline schedule updated.'})[2:]}",
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/pipeline_schedules/{schedule_id}/play", response_class=HTMLResponse)
async def repo_pipeline_schedule_play(
    request: Request,
    owner: str,
    repo_name: str,
    schedule_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Run a project pipeline schedule immediately."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    redirect = f"/ui/{owner}/{repo.name}/-/pipeline_schedules"
    schedule = (
        await db.execute(
            select(PipelineSchedule).where(
                PipelineSchedule.project_id == repo.id,
                PipelineSchedule.id == schedule_id,
            )
        )
    ).scalar_one_or_none()
    if schedule is None:
        return RedirectResponse(url=f"{redirect}?error=Schedule%20not%20found.", status_code=302)
    try:
        pipeline = await materialize_pipeline_schedule(
            schedule,
            repo.id,
            db,
            actor=current_user,
        )
    except Exception as exc:
        await db.rollback()
        detail = exc.detail if hasattr(exc, "detail") else str(exc)
        return RedirectResponse(
            url=f"{redirect}?error={urlencode({'x': detail})[2:]}",
            status_code=302,
        )
    return RedirectResponse(
        url=f"/ui/{owner}/{repo.name}/-/pipelines/{pipeline.id}",
        status_code=302,
    )


@router.post("/{owner}/{repo_name}/-/pipeline_schedules/{schedule_id}/delete", response_class=HTMLResponse)
async def repo_pipeline_schedule_delete(
    request: Request,
    owner: str,
    repo_name: str,
    schedule_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a project pipeline schedule."""
    current_user, repo, response = await _managed_repo_or_response(
        request, db, owner, repo_name
    )
    if response is not None:
        return response
    schedule = (
        await db.execute(
            select(PipelineSchedule).where(
                PipelineSchedule.project_id == repo.id,
                PipelineSchedule.id == schedule_id,
            )
        )
    ).scalar_one_or_none()
    if schedule is not None:
        await db.delete(schedule)
        await db.commit()
    return RedirectResponse(
        url=f"/ui/{owner}/{repo.name}/-/pipeline_schedules?message={urlencode({'x': 'Pipeline schedule deleted.'})[2:]}",
        status_code=302,
    )


async def _repo_ci_template(
    request: Request,
    owner: str,
    repo: Repository,
    current_user: Optional[User],
    db: AsyncSession,
    *,
    pipeline_id: int | None = None,
    job_id: int | None = None,
    flash_message: str | None = None,
    flash_type: str = "info",
) -> HTMLResponse:
    pipelines = list((
        await db.execute(
            select(Pipeline)
            .where(Pipeline.project_id == repo.id)
            .order_by(Pipeline.id.desc())
            .limit(30)
        )
    ).scalars().all())

    selected_pipeline = None
    jobs: list[PipelineJob] = []
    selected_job = None
    trace_text = ""

    if pipeline_id is not None:
        selected_pipeline = (
            await db.execute(
                select(Pipeline)
                .options(
                    selectinload(Pipeline.jobs).selectinload(PipelineJob.trace),
                    selectinload(Pipeline.jobs).selectinload(PipelineJob.artifacts),
                )
                .where(Pipeline.project_id == repo.id, Pipeline.id == pipeline_id)
            )
        ).scalar_one_or_none()
        if selected_pipeline is None:
            return HTMLResponse(content="<h1>404 - Pipeline Not Found</h1>", status_code=404)
        jobs = sorted(selected_pipeline.jobs, key=lambda job: (job.stage_index, job.id))

    if job_id is not None:
        selected_job = (
            await db.execute(
                select(PipelineJob)
                .options(
                    selectinload(PipelineJob.pipeline).selectinload(Pipeline.jobs),
                    selectinload(PipelineJob.trace),
                    selectinload(PipelineJob.artifacts),
                )
                .where(PipelineJob.project_id == repo.id, PipelineJob.id == job_id)
            )
        ).scalar_one_or_none()
        if selected_job is None:
            return HTMLResponse(content="<h1>404 - Job Not Found</h1>", status_code=404)
        trace_text = selected_job.trace.content if selected_job.trace else ""
        if selected_pipeline is None:
            selected_pipeline = selected_job.pipeline
            jobs = sorted(selected_pipeline.jobs, key=lambda job: (job.stage_index, job.id))

    job_diagnostics = await _job_scheduling_diagnostics(db, jobs)
    downstream_by_job = await _downstream_pipeline_context(db, jobs)

    return templates.TemplateResponse(
        request=request,
        name="repo_pipeline_detail.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            current_user=current_user,
            pipelines=pipelines,
            selected_pipeline=selected_pipeline,
            jobs=jobs,
            selected_job=selected_job,
            job_diagnostics=job_diagnostics,
            downstream_by_job=downstream_by_job,
            trace_text=trace_text,
            default_branch=repo.default_branch or "main",
            flash_message=flash_message,
            flash_type=flash_type,
        ),
    )


@router.get("/{owner}/{repo_name}/-/pipelines", response_class=HTMLResponse)
async def repo_pipelines_page(
    request: Request,
    owner: str,
    repo_name: str,
    scope: str = Query("all"),
    flash_message: str | None = Query(None),
    flash_type: str = Query("info"),
    db: AsyncSession = Depends(get_db),
):
    """Repository-scoped CI pipeline list."""
    current_user = await _get_current_user(request, db)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    valid_scopes = {"all", "finished", "branches", "tags"}
    scope = scope if scope in valid_scopes else "all"
    branches, tags = await _repo_ref_choices(repo)
    branch_names = {branch["name"] for branch in branches}
    tag_names = {tag["name"] for tag in tags}

    query = select(Pipeline).options(selectinload(Pipeline.user)).where(Pipeline.project_id == repo.id)
    if scope == "finished":
        query = query.where(Pipeline.status.in_(["success", "failed", "canceled", "skipped"]))
    pipelines = list((
        await db.execute(query.order_by(Pipeline.id.desc()).limit(50))
    ).scalars().all())
    if scope == "branches":
        pipelines = [pipeline for pipeline in pipelines if pipeline.ref in branch_names]
    elif scope == "tags":
        pipelines = [pipeline for pipeline in pipelines if pipeline.ref in tag_names]

    pipeline_ids = [pipeline.id for pipeline in pipelines]
    jobs_by_pipeline: dict[int, list[PipelineJob]] = {pipeline.id: [] for pipeline in pipelines}
    if pipeline_ids:
        jobs = list((
            await db.execute(
                select(PipelineJob)
                .where(PipelineJob.pipeline_id.in_(pipeline_ids))
                .order_by(PipelineJob.stage_index.asc(), PipelineJob.id.asc())
            )
        ).scalars().all())
        for job in jobs:
            jobs_by_pipeline.setdefault(job.pipeline_id, []).append(job)

    return templates.TemplateResponse(
        request=request,
        name="repo_pipelines.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            current_user=current_user,
            pipelines=pipelines,
            jobs_by_pipeline=jobs_by_pipeline,
            default_branch=repo.default_branch or "main",
            active_scope=scope,
            flash_message=flash_message,
            flash_type=flash_type,
        ),
    )


@router.get("/{owner}/{repo_name}/-/pipelines/new", response_class=HTMLResponse)
async def repo_new_pipeline_page(
    request: Request,
    owner: str,
    repo_name: str,
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Render a dedicated run-new-pipeline page."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    if not await _can_manage_repo(current_user, repo, db):
        return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)
    branches, tags = await _repo_ref_choices(repo)
    return templates.TemplateResponse(
        request=request,
        name="repo_pipeline_new.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            current_user=current_user,
            default_branch=repo.default_branch or "main",
            branches=branches,
            tags=tags,
            error=error,
        ),
    )


@router.get("/{owner}/{repo_name}/-/pipelines/{pipeline_id}", response_class=HTMLResponse)
async def repo_pipeline_detail_page(
    request: Request,
    owner: str,
    repo_name: str,
    pipeline_id: int,
    flash_message: str | None = Query(None),
    flash_type: str = Query("info"),
    db: AsyncSession = Depends(get_db),
):
    """Pipeline detail page with the pipeline's jobs."""
    current_user = await _get_current_user(request, db)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    return await _repo_ci_template(
        request,
        owner,
        repo,
        current_user,
        db,
        pipeline_id=pipeline_id,
        flash_message=flash_message,
        flash_type=flash_type,
    )


@router.get("/{owner}/{repo_name}/-/ci/editor", response_class=HTMLResponse)
async def repo_pipeline_editor_page(
    request: Request,
    owner: str,
    repo_name: str,
    ref: str | None = Query(None),
    error: str | None = Query(None),
    saved: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """Dedicated `.gitlab-ci.yml` editor page."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    if not await _can_manage_repo(current_user, repo, db):
        return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)
    selected_ref = (ref or repo.default_branch or "main").strip()
    branches, tags = await _repo_ref_choices(repo)
    content = ""
    config_status = ".gitlab-ci.yml not found on this ref."
    if repo.disk_path and os.path.isdir(repo.disk_path):
        raw = await get_file_content(repo.disk_path, selected_ref, ".gitlab-ci.yml")
        if raw is not None:
            content = raw.decode("utf-8", errors="replace")
            config_status = "Configuration file loaded."
    return templates.TemplateResponse(
        request=request,
        name="repo_pipeline_editor.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            current_user=current_user,
            ref=selected_ref,
            branches=branches,
            tags=tags,
            content=content,
            config_status=config_status,
            error=error,
            saved=saved,
        ),
    )


@router.post("/{owner}/{repo_name}/-/ci/editor", response_class=HTMLResponse)
async def repo_pipeline_editor_submit(
    request: Request,
    owner: str,
    repo_name: str,
    ref: str = Form("main"),
    content: str = Form(""),
    commit_message: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Save `.gitlab-ci.yml` from the dedicated pipeline editor."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    if not await _can_manage_repo(current_user, repo, db):
        return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)
    selected_ref = ref.strip() or repo.default_branch or "main"
    if not commit_message:
        commit_message = "Update .gitlab-ci.yml"
    try:
        await write_file(
            disk_path=repo.disk_path,
            branch=selected_ref,
            path=".gitlab-ci.yml",
            content=content.encode("utf-8"),
            message=commit_message,
            author_name=current_user.name or current_user.login,
            author_email=current_user.email or f"{current_user.login}@users.noreply.gitlab-emulator.local",
        )
    except Exception as exc:
        return RedirectResponse(
            url=f"/ui/{owner}/{repo.name}/-/ci/editor?{urlencode({'ref': selected_ref, 'error': str(exc)})}",
            status_code=302,
        )
    return RedirectResponse(
        url=f"/ui/{owner}/{repo.name}/-/ci/editor?{urlencode({'ref': selected_ref, 'saved': 'true'})}",
        status_code=302,
    )


@router.get("/{owner}/{repo_name}/-/jobs", response_class=HTMLResponse)
async def repo_jobs_page(
    request: Request,
    owner: str,
    repo_name: str,
    db: AsyncSession = Depends(get_db),
):
    """Repository-scoped CI jobs list across pipelines."""
    current_user = await _get_current_user(request, db)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    jobs = list((
        await db.execute(
            select(PipelineJob)
            .options(selectinload(PipelineJob.pipeline))
            .where(PipelineJob.project_id == repo.id)
            .order_by(PipelineJob.id.desc())
            .limit(100)
        )
    ).scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="repo_jobs.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            current_user=current_user,
            jobs=jobs,
            job_diagnostics=await _job_scheduling_diagnostics(db, jobs),
        ),
    )


@router.get("/{owner}/{repo_name}/-/artifacts", response_class=HTMLResponse)
async def repo_artifacts_page(
    request: Request,
    owner: str,
    repo_name: str,
    db: AsyncSession = Depends(get_db),
):
    """Repository-scoped CI artifact list across jobs."""
    current_user = await _get_current_user(request, db)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    rows = (
        await db.execute(
            select(JobArtifact, PipelineJob, Pipeline)
            .join(PipelineJob, JobArtifact.job_id == PipelineJob.id)
            .join(Pipeline, PipelineJob.pipeline_id == Pipeline.id)
            .where(Pipeline.project_id == repo.id)
            .order_by(JobArtifact.created_at.desc(), JobArtifact.id.desc())
            .limit(100)
        )
    ).all()
    artifacts = [
        {
            "artifact": artifact,
            "job": job,
            "pipeline": pipeline,
            "download_url": f"/api/v4/projects/{repo.id}/jobs/{job.id}/artifacts",
        }
        for artifact, job, pipeline in rows
    ]

    return templates.TemplateResponse(
        request=request,
        name="repo_artifacts.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            current_user=current_user,
            artifacts=artifacts,
        ),
    )


@router.get("/{owner}/{repo_name}/-/jobs/{job_id}", response_class=HTMLResponse)
async def repo_job_detail_page(
    request: Request,
    owner: str,
    repo_name: str,
    job_id: int,
    flash_message: str | None = Query(None),
    flash_type: str = Query("info"),
    db: AsyncSession = Depends(get_db),
):
    """Job detail page with trace/log output."""
    current_user = await _get_current_user(request, db)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    return await _repo_ci_template(
        request,
        owner,
        repo,
        current_user,
        db,
        job_id=job_id,
        flash_message=flash_message,
        flash_type=flash_type,
    )


@router.post("/{owner}/{repo_name}/-/pipelines", response_class=HTMLResponse)
async def repo_create_pipeline(
    request: Request,
    owner: str,
    repo_name: str,
    ref: str = Form("main"),
    ref_type: str = Form("branch"),
    variable_key: str = Form(""),
    variable_value: str = Form(""),
    variable_type: str = Form("env_var"),
    db: AsyncSession = Depends(get_db),
):
    """Create a repository pipeline from committed `.gitlab-ci.yml`."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    if not await _can_manage_repo(current_user, repo, db):
        return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)

    repo_id = repo.id
    repo_name_value = repo.name
    repo_full_name = repo.full_name
    default_branch = repo.default_branch or "main"
    try:
        variables = _pipeline_variables_from_form(
            variable_key,
            variable_value,
            variable_type,
        )
        pipeline = await _create_pipeline(
            repo_id,
            CreatePipelineRequest(
                ref=ref.strip() or default_branch,
                variables=variables,
            ),
            db,
            source="web",
            actor=current_user,
        )
    except Exception as exc:
        await db.rollback()
        detail = exc.detail if hasattr(exc, "detail") else str(exc)
        if request.url.path.endswith("/pipelines"):
            redirect_url = f"/ui/{repo_full_name}/-/pipelines/new"
            return RedirectResponse(
                url=f"{redirect_url}?{urlencode({'error': detail})}",
                status_code=302,
            )
        return _repo_ci_redirect(
            owner,
            repo_name_value,
            flash_message=f"Could not create pipeline: {detail}",
            flash_type="error",
        )

    return _repo_ci_redirect(
        owner,
        repo_name_value,
        pipeline_id=pipeline.id,
        flash_message=f"Pipeline #{pipeline.id} created.",
        flash_type="success",
    )


@router.post("/{owner}/{repo_name}/-/pipelines/{pipeline_id}/cancel", response_class=HTMLResponse)
async def repo_cancel_pipeline(
    request: Request,
    owner: str,
    repo_name: str,
    pipeline_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Cancel runnable jobs in a repository pipeline."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    if not await _can_manage_repo(current_user, repo, db):
        return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)

    pipeline = (
        await db.execute(
            select(Pipeline)
            .options(selectinload(Pipeline.jobs))
            .where(Pipeline.project_id == repo.id, Pipeline.id == pipeline_id)
        )
    ).scalar_one_or_none()
    if pipeline is None:
        return HTMLResponse(content="<h1>404 - Pipeline Not Found</h1>", status_code=404)
    now = datetime.now(timezone.utc)
    for job in pipeline.jobs:
        if job.status in {"pending", "running", "manual", "scheduled"}:
            job.status = "canceled"
            job.finished_at = job.finished_at or now
    await _derive_pipeline_status(pipeline, db)
    await db.commit()
    return _repo_ci_redirect(
        owner,
        repo.name,
        pipeline_id=pipeline.id,
        flash_message="Pipeline canceled.",
        flash_type="success",
    )


@router.post("/{owner}/{repo_name}/-/jobs/{job_id}/{action}", response_class=HTMLResponse)
async def repo_job_action(
    request: Request,
    owner: str,
    repo_name: str,
    job_id: int,
    action: str,
    db: AsyncSession = Depends(get_db),
):
    """Play, cancel, or retry a repository CI job."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    if not await _can_manage_repo(current_user, repo, db):
        return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)

    job = (
        await db.execute(
            select(PipelineJob)
            .options(
                selectinload(PipelineJob.pipeline).selectinload(Pipeline.jobs),
                selectinload(PipelineJob.trace),
            )
            .where(PipelineJob.project_id == repo.id, PipelineJob.id == job_id)
        )
    ).scalar_one_or_none()
    if job is None:
        return HTMLResponse(content="<h1>404 - Job Not Found</h1>", status_code=404)

    now = datetime.now(timezone.utc)
    message = "Job updated."
    if action == "play":
        if job.status != "manual":
            return _repo_ci_redirect(
                owner, repo.name, pipeline_id=job.pipeline_id, job_id=job.id,
                flash_message="Job is not playable.", flash_type="error",
            )
        job.status = "pending"
        job.queued_at = now
        job.failure_reason = None
        job.exit_code = None
        message = "Job queued."
    elif action == "cancel":
        if job.status in {"pending", "running", "manual", "scheduled"}:
            job.status = "canceled"
            job.finished_at = job.finished_at or now
        message = "Job canceled."
    elif action == "retry":
        if job.status in {"failed", "canceled", "skipped", "success"}:
            _reset_job_for_retry(job, now)
            message = "Job retried."
        else:
            return _repo_ci_redirect(
                owner, repo.name, pipeline_id=job.pipeline_id, job_id=job.id,
                flash_message="Job is not retryable.", flash_type="error",
            )
    else:
        return _repo_ci_redirect(
            owner, repo.name, pipeline_id=job.pipeline_id, job_id=job.id,
            flash_message="Unsupported job action.", flash_type="error",
        )

    await _derive_pipeline_status(job.pipeline, db)
    if job.pipeline.status in {"pending", "running"}:
        job.pipeline.finished_at = None
    await db.commit()
    return _repo_ci_redirect(
        owner,
        repo.name,
        pipeline_id=job.pipeline_id,
        job_id=job.id,
        flash_message=message,
        flash_type="success",
    )


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/issues/new", response_class=HTMLResponse)
async def new_issue_page(
    request: Request,
    owner: str,
    repo_name: str,
    db: AsyncSession = Depends(get_db),
):
    """Form for creating a new issue."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    return templates.TemplateResponse(
        request=request,
        name="new_issue.html",
        context=_ctx(
            request, owner=owner, repo=repo, repo_name=repo.name,
            current_user=current_user, error=None,
        ),
    )


@router.post("/{owner}/{repo_name}/issues/new", response_class=HTMLResponse)
async def new_issue_submit(
    request: Request,
    owner: str,
    repo_name: str,
    title: str = Form(...),
    body: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Create a new issue."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    issue = await issue_service.create_issue(
        db, repo=repo, user=current_user,
        title=title, body=body or None,
    )
    return RedirectResponse(
        url=f"/ui/{owner}/{repo_name}/issues/{issue.number}", status_code=302
    )


@router.get("/{owner}/{repo_name}/issues", response_class=HTMLResponse)
async def issues_list(
    request: Request,
    owner: str,
    repo_name: str,
    state: str = Query("all"),
    db: AsyncSession = Depends(get_db),
):
    """List issues for a repository."""
    current_user = await _get_current_user(request, db)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    pr_issue_ids = select(PullRequest.issue_id)
    query = select(Issue).where(
        Issue.repo_id == repo.id,
        ~Issue.id.in_(pr_issue_ids),
    )
    if state in ("open", "closed"):
        query = query.where(Issue.state == state)
    query = query.order_by(Issue.number.desc())

    result = await db.execute(query)
    issues = list(result.scalars().all())
    for issue in issues:
        issue.user_login = issue.user.login if issue.user else "unknown"

    # Counts
    open_count = (await db.execute(
        select(func.count(Issue.id)).where(
            Issue.repo_id == repo.id, Issue.state == "open",
            ~Issue.id.in_(pr_issue_ids),
        )
    )).scalar() or 0
    closed_count = (await db.execute(
        select(func.count(Issue.id)).where(
            Issue.repo_id == repo.id, Issue.state == "closed",
            ~Issue.id.in_(pr_issue_ids),
        )
    )).scalar() or 0

    return templates.TemplateResponse(
        request=request,
        name="issues.html",
        context=_ctx(
            request, owner=owner, repo=repo, repo_name=repo.name,
            issues=issues, state=state,
            open_count=open_count, closed_count=closed_count,
            open_issues_count=open_count,
            selected_issue=None,
            selected_comments=[],
            current_user=current_user,
        ),
    )


@router.get("/{owner}/{repo_name}/issues/{number:int}", response_class=HTMLResponse)
async def issue_detail(
    request: Request,
    owner: str,
    repo_name: str,
    number: int,
    db: AsyncSession = Depends(get_db),
):
    """Single issue detail with comments."""
    current_user = await _get_current_user(request, db)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    pr_issue_ids = select(PullRequest.issue_id)
    issues_query = (
        select(Issue)
        .where(Issue.repo_id == repo.id, ~Issue.id.in_(pr_issue_ids))
        .order_by(Issue.number.desc())
    )
    issues = list((await db.execute(issues_query)).scalars().all())
    for item in issues:
        item.user_login = item.user.login if item.user else "unknown"

    issue = next((item for item in issues if item.number == number), None)
    if issue is None:
        return HTMLResponse(content="<h1>404 - Issue Not Found</h1>", status_code=404)

    issue.user_login = issue.user.login if issue.user else "unknown"

    result = await db.execute(
        select(IssueComment).where(
            IssueComment.issue_id == issue.id
        ).order_by(IssueComment.created_at)
    )
    comments = list(result.scalars().all())
    for c in comments:
        c.user_login = c.user.login if c.user else "unknown"

    open_count = (await db.execute(
        select(func.count(Issue.id)).where(
            Issue.repo_id == repo.id, Issue.state == "open",
            ~Issue.id.in_(pr_issue_ids),
        )
    )).scalar() or 0
    closed_count = (await db.execute(
        select(func.count(Issue.id)).where(
            Issue.repo_id == repo.id, Issue.state == "closed",
            ~Issue.id.in_(pr_issue_ids),
        )
    )).scalar() or 0

    return templates.TemplateResponse(
        request=request,
        name="issues.html",
        context=_ctx(
            request, owner=owner, repo=repo, repo_name=repo.name,
            issues=issues,
            state="all",
            open_count=open_count,
            closed_count=closed_count,
            open_issues_count=open_count,
            selected_issue=issue,
            selected_comments=comments,
            current_user=current_user,
        ),
    )


# ---------------------------------------------------------------------------
# Pull Requests
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/pulls/new", response_class=HTMLResponse)
async def new_pull_page(
    request: Request,
    owner: str,
    repo_name: str,
    db: AsyncSession = Depends(get_db),
):
    """Form for creating a new pull request."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    branches = []
    default_branch = repo.default_branch or "main"
    if repo.disk_path and os.path.isdir(repo.disk_path):
        branches = await get_branches(repo.disk_path)

    return templates.TemplateResponse(
        request=request,
        name="new_pull.html",
        context=_ctx(
            request, owner=owner, repo=repo, repo_name=repo.name,
            branches=branches, default_branch=default_branch,
            current_user=current_user, error=None,
        ),
    )


@router.post("/{owner}/{repo_name}/pulls/new", response_class=HTMLResponse)
async def new_pull_submit(
    request: Request,
    owner: str,
    repo_name: str,
    title: str = Form(...),
    body: str = Form(""),
    head_ref: str = Form(...),
    base_ref: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Create a new pull request."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    try:
        issue, pr = await pr_service.create_pr(
            db, repo=repo, user=current_user,
            title=title, body=body or None,
            head_ref=head_ref, base_ref=base_ref,
        )
        return RedirectResponse(
            url=f"/ui/{owner}/{repo_name}/pulls/{issue.number}", status_code=302
        )
    except Exception as exc:
        branches = []
        default_branch = repo.default_branch or "main"
        if repo.disk_path and os.path.isdir(repo.disk_path):
            branches = await get_branches(repo.disk_path)
        return templates.TemplateResponse(
            request=request,
            name="new_pull.html",
            context=_ctx(
                request, owner=owner, repo=repo, repo_name=repo.name,
                branches=branches, default_branch=default_branch,
                current_user=current_user, error=str(exc),
            ),
        )


@router.get("/{owner}/{repo_name}/pulls", response_class=HTMLResponse)
async def pulls_list(
    request: Request,
    owner: str,
    repo_name: str,
    state: str = Query("open"),
    db: AsyncSession = Depends(get_db),
):
    """List pull requests for a repository."""
    current_user = await _get_current_user(request, db)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    result = await db.execute(
        select(PullRequest).where(
            PullRequest.repo_id == repo.id
        ).order_by(PullRequest.id.desc())
    )
    all_pulls = list(result.scalars().all())

    # Enrich and filter
    pulls = []
    open_count = 0
    closed_count = 0
    for pr in all_pulls:
        pr.number = pr.issue.number if pr.issue else 0
        pr.title = pr.issue.title if pr.issue else "Untitled"
        pr.state = pr.issue.state if pr.issue else "open"
        pr.user_login = pr.issue.user.login if pr.issue and pr.issue.user else "unknown"
        pr.updated_at = pr.issue.updated_at if pr.issue else None
        if pr.state == "open":
            open_count += 1
        else:
            closed_count += 1
        if state == "open" and pr.state == "open":
            pulls.append(pr)
        elif state == "closed" and pr.state != "open":
            pulls.append(pr)
        elif state not in ("open", "closed"):
            pulls.append(pr)

    return templates.TemplateResponse(
        request=request,
        name="pulls.html",
        context=_ctx(
            request, owner=owner, repo=repo, repo_name=repo.name,
            pulls=pulls, state=state,
            open_count=open_count, closed_count=closed_count,
            open_pulls_count=open_count,
            current_user=current_user,
        ),
    )


@router.get("/{owner}/{repo_name}/pulls/{number:int}", response_class=HTMLResponse)
async def pull_detail(
    request: Request,
    owner: str,
    repo_name: str,
    number: int,
    db: AsyncSession = Depends(get_db),
):
    """Single pull request detail."""
    current_user = await _get_current_user(request, db)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    # Find the issue for this PR number
    result = await db.execute(
        select(Issue).where(Issue.repo_id == repo.id, Issue.number == number)
    )
    issue = result.scalar_one_or_none()
    if issue is None or issue.pull_request is None:
        return HTMLResponse(content="<h1>404 - PR Not Found</h1>", status_code=404)

    pr = issue.pull_request
    pr.number = issue.number
    pr.title = issue.title
    pr.body = issue.body
    pr.state = issue.state
    pr.user_login = issue.user.login if issue.user else "unknown"
    pr.created_at = issue.created_at

    # Get comments on the issue
    result = await db.execute(
        select(IssueComment).where(
            IssueComment.issue_id == issue.id
        ).order_by(IssueComment.created_at)
    )
    comments = list(result.scalars().all())
    for c in comments:
        c.user_login = c.user.login if c.user else "unknown"

    return templates.TemplateResponse(
        request=request,
        name="pull_detail.html",
        context=_ctx(
            request, owner=owner, repo=repo, repo_name=repo.name,
            pr=pr, comments=comments,
            current_user=current_user,
        ),
    )


# ---------------------------------------------------------------------------
# Create new file
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/new/{ref}", response_class=HTMLResponse)
@router.get("/{owner}/{repo_name}/new/{ref}/{path:path}", response_class=HTMLResponse)
async def new_file_page(
    request: Request,
    owner: str,
    repo_name: str,
    ref: str,
    path: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Form for creating a new file."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    if not await _can_manage_repo(current_user, repo, db):
        return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)

    return templates.TemplateResponse(
        request=request,
        name="new_file.html",
        context=_ctx(
            request, owner=owner, repo=repo, repo_name=repo.name,
            ref=ref, dir_path=path, current_user=current_user, error=None,
        ),
    )


@router.post("/{owner}/{repo_name}/new/{ref}", response_class=HTMLResponse)
@router.post("/{owner}/{repo_name}/new/{ref}/{path:path}", response_class=HTMLResponse)
async def new_file_submit(
    request: Request,
    owner: str,
    repo_name: str,
    ref: str,
    path: str = "",
    filename: str = Form(...),
    content: str = Form(""),
    commit_message: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Create a new file in the repository."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    if not await _can_manage_repo(current_user, repo, db):
        return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)

    # Build full file path
    full_path = f"{path}/{filename}" if path else filename

    if not commit_message:
        commit_message = f"Create {full_path}"

    try:
        await write_file(
            disk_path=repo.disk_path,
            branch=ref,
            path=full_path,
            content=content.encode("utf-8"),
            message=commit_message,
            author_name=current_user.name or current_user.login,
            author_email=current_user.email or f"{current_user.login}@users.noreply.gitlab-emulator.local",
        )
        return RedirectResponse(
            url=f"/ui/{owner}/{repo_name}/blob/{ref}/{full_path}",
            status_code=302,
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request=request,
            name="new_file.html",
            context=_ctx(
                request, owner=owner, repo=repo, repo_name=repo.name,
                ref=ref, dir_path=path, current_user=current_user,
                error=str(exc),
            ),
        )


# ---------------------------------------------------------------------------
# Edit file
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/edit/{ref}/{path:path}", response_class=HTMLResponse)
async def edit_file_page(
    request: Request,
    owner: str,
    repo_name: str,
    ref: str,
    path: str,
    db: AsyncSession = Depends(get_db),
):
    """Edit form for an existing file, pre-filled with current content."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    if not await _can_manage_repo(current_user, repo, db):
        return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)

    content = ""
    if repo.disk_path and os.path.isdir(repo.disk_path):
        raw = await get_file_content(repo.disk_path, ref, path)
        if raw:
            content = raw.decode("utf-8", errors="replace")

    return templates.TemplateResponse(
        request=request,
        name="edit_file.html",
        context=_ctx(
            request, owner=owner, repo=repo, repo_name=repo.name,
            ref=ref, path=path, content=content,
            current_user=current_user, error=None,
        ),
    )


@router.post("/{owner}/{repo_name}/edit/{ref}/{path:path}", response_class=HTMLResponse)
async def edit_file_submit(
    request: Request,
    owner: str,
    repo_name: str,
    ref: str,
    path: str,
    content: str = Form(""),
    commit_message: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Save edits to an existing file."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    if not await _can_manage_repo(current_user, repo, db):
        return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)

    if not commit_message:
        commit_message = f"Update {path}"

    try:
        await write_file(
            disk_path=repo.disk_path,
            branch=ref,
            path=path,
            content=content.encode("utf-8"),
            message=commit_message,
            author_name=current_user.name or current_user.login,
            author_email=current_user.email or f"{current_user.login}@users.noreply.gitlab-emulator.local",
        )
        return RedirectResponse(
            url=f"/ui/{owner}/{repo_name}/blob/{ref}/{path}",
            status_code=302,
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request=request,
            name="edit_file.html",
            context=_ctx(
                request, owner=owner, repo=repo, repo_name=repo.name,
                ref=ref, path=path, content=content,
                current_user=current_user, error=str(exc),
            ),
        )


# ---------------------------------------------------------------------------
# Delete file
# ---------------------------------------------------------------------------

@router.post("/{owner}/{repo_name}/delete-file/{ref}/{path:path}", response_class=HTMLResponse)
async def delete_file_submit(
    request: Request,
    owner: str,
    repo_name: str,
    ref: str,
    path: str,
    commit_message: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Delete an existing file from the repository."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    if not await _can_manage_repo(current_user, repo, db):
        return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)

    if not commit_message:
        commit_message = f"Delete {path}"

    try:
        await delete_file(
            disk_path=repo.disk_path,
            branch=ref,
            path=path,
            message=commit_message,
            author_name=current_user.name or current_user.login,
            author_email=current_user.email or f"{current_user.login}@users.noreply.gitlab-emulator.local",
        )
        parent_path = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent_path:
            url = f"/ui/{owner}/{repo.name}/tree/{ref}/{parent_path}"
        else:
            url = f"/ui/{owner}/{repo.name}"
        return RedirectResponse(url=url, status_code=302)
    except Exception as exc:
        content = ""
        if repo.disk_path and os.path.isdir(repo.disk_path):
            raw = await get_file_content(repo.disk_path, ref, path)
            if raw:
                content = raw.decode("utf-8", errors="replace")
        return templates.TemplateResponse(
            request=request,
            name="edit_file.html",
            context=_ctx(
                request, owner=owner, repo=repo, repo_name=repo.name,
                ref=ref, path=path, content=content,
                current_user=current_user, error=str(exc),
            ),
        )


# ---------------------------------------------------------------------------
# Commits list
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/commits/{ref}", response_class=HTMLResponse)
async def commits_list(
    request: Request,
    owner: str,
    repo_name: str,
    ref: str,
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
):
    """Commit history for a branch."""
    current_user = await _get_current_user(request, db)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    per_page = 30
    commits = []
    total = 0
    if repo.disk_path and os.path.isdir(repo.disk_path):
        total = await get_commit_count(repo.disk_path, ref)
        commits = await get_log(
            repo.disk_path, ref=ref,
            max_count=per_page, skip=(page - 1) * per_page,
        )

    has_next = (page * per_page) < total

    return templates.TemplateResponse(
        request=request,
        name="commits.html",
        context=_ctx(
            request, owner=owner, repo=repo, repo_name=repo.name,
            ref=ref, commits=commits, page=page, has_next=has_next,
            total=total, current_user=current_user,
        ),
    )


# ---------------------------------------------------------------------------
# Single commit detail
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/commit/{sha}", response_class=HTMLResponse)
async def commit_detail_view(
    request: Request,
    owner: str,
    repo_name: str,
    sha: str,
    db: AsyncSession = Depends(get_db),
):
    """Single commit detail with diff."""
    current_user = await _get_current_user(request, db)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    commit_info = None
    diff_files = []
    if repo.disk_path and os.path.isdir(repo.disk_path):
        commit_info = await get_commit_info(repo.disk_path, sha)
        diff_files = await get_commit_diff(repo.disk_path, sha)

    if commit_info is None:
        return HTMLResponse(content="<h1>404 - Commit Not Found</h1>", status_code=404)

    return templates.TemplateResponse(
        request=request,
        name="commit_detail.html",
        context=_ctx(
            request, owner=owner, repo=repo, repo_name=repo.name,
            commit_info=commit_info, diff_files=diff_files,
            current_user=current_user,
        ),
    )


# ---------------------------------------------------------------------------
# Branches list
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/branches", response_class=HTMLResponse)
async def branches_list(
    request: Request,
    owner: str,
    repo_name: str,
    db: AsyncSession = Depends(get_db),
):
    """List all branches."""
    current_user = await _get_current_user(request, db)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    branches = []
    default_branch = repo.default_branch or "main"
    if repo.disk_path and os.path.isdir(repo.disk_path):
        branches = await get_branches(repo.disk_path)
        # For each branch, fetch latest commit
        for branch in branches:
            log = await get_log(repo.disk_path, ref=branch["name"], max_count=1)
            branch["last_commit"] = log[0] if log else None
        # Sort: default branch first
        branches.sort(key=lambda b: (0 if b["name"] == default_branch else 1, b["name"]))

    return templates.TemplateResponse(
        request=request,
        name="branches.html",
        context=_ctx(
            request, owner=owner, repo=repo, repo_name=repo.name,
            branches=branches, default_branch=default_branch,
            current_user=current_user,
        ),
    )


# ---------------------------------------------------------------------------
# Tags list
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/tags", response_class=HTMLResponse)
async def tags_list(
    request: Request,
    owner: str,
    repo_name: str,
    db: AsyncSession = Depends(get_db),
):
    """List all tags."""
    current_user = await _get_current_user(request, db)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    tags = []
    if repo.disk_path and os.path.isdir(repo.disk_path):
        tags = await get_tags(repo.disk_path)

    return templates.TemplateResponse(
        request=request,
        name="tags.html",
        context=_ctx(
            request, owner=owner, repo=repo, repo_name=repo.name,
            tags=tags, current_user=current_user,
        ),
    )


# ---------------------------------------------------------------------------
# Tree (directory) view
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/tree/{ref}/{path:path}", response_class=HTMLResponse)
async def tree_view(
    request: Request,
    owner: str,
    repo_name: str,
    ref: str,
    path: str,
    db: AsyncSession = Depends(get_db),
):
    """Directory listing at a given ref and path."""
    current_user = await _get_current_user(request, db)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    entries = None
    if repo.disk_path and os.path.isdir(repo.disk_path):
        entries = await list_tree(repo.disk_path, ref, path)
        if entries:
            entries.sort(key=lambda e: (0 if e["type"] == "tree" else 1, e["name"]))

    return templates.TemplateResponse(
        request=request,
        name="tree.html",
        context=_ctx(
            request, owner=owner, repo=repo, repo_name=repo.name,
            ref=ref, path=path, entries=entries,
            current_user=current_user,
        ),
    )


# ---------------------------------------------------------------------------
# Blob (file) view
# ---------------------------------------------------------------------------

@router.get("/{owner}/{repo_name}/blob/{ref}/{path:path}", response_class=HTMLResponse)
async def blob_view(
    request: Request,
    owner: str,
    repo_name: str,
    ref: str,
    path: str,
    db: AsyncSession = Depends(get_db),
):
    """File content viewer."""
    current_user = await _get_current_user(request, db)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    content = None
    if repo.disk_path and os.path.isdir(repo.disk_path):
        raw = await get_file_content(repo.disk_path, ref, path)
        if raw:
            try:
                content = raw.decode("utf-8", errors="replace")
            except Exception:
                content = None

    return templates.TemplateResponse(
        request=request,
        name="blob.html",
        context=_ctx(
            request, owner=owner, repo=repo, repo_name=repo.name,
            ref=ref, path=path, content=content,
            current_user=current_user,
        ),
    )


@router.get("/{owner}/{repo_name}/raw/{ref}/{path:path}", response_class=PlainTextResponse)
async def raw_file_view(
    owner: str,
    repo_name: str,
    ref: str,
    path: str,
    db: AsyncSession = Depends(get_db),
):
    """Raw repository file content."""
    repo = await _get_repo(db, owner, repo_name)
    if repo is None or not repo.disk_path or not os.path.isdir(repo.disk_path):
        return PlainTextResponse(content="Not Found", status_code=404)

    raw = await get_file_content(repo.disk_path, ref, path)
    if raw is None:
        return PlainTextResponse(content="Not Found", status_code=404)
    return PlainTextResponse(content=raw.decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# Nested repository overview fallback
# ---------------------------------------------------------------------------

@router.get("/{project_path:path}", response_class=HTMLResponse)
@router.post("/{project_path:path}", response_class=HTMLResponse)
async def nested_repo_page(
    request: Request,
    project_path: str,
    db: AsyncSession = Depends(get_db),
):
    """Repository overview for nested GitLab paths such as group/subgroup/repo."""
    normalized = project_path.strip("/")
    if "/" not in normalized:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    repo, action_parts = await _resolve_repo_and_remainder(db, normalized)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    owner = repo.full_name.rsplit("/", 1)[0]
    repo_id = repo.id
    repo_name = repo.name
    repo_full_name = repo.full_name
    default_branch = repo.default_branch or "main"
    current_user = await _get_current_user(request, db)
    await _can_manage_repo(current_user, repo, db)

    if action_parts:
        if request.method == "GET" and action_parts[0] == "settings" and len(action_parts) == 1:
            return await repo_settings_page(
                request,
                owner,
                repo.name,
                saved=request.query_params.get("saved") in {"1", "true", "True"},
                db=db,
            )

        if request.method == "GET" and action_parts[0] == "issues":
            if len(action_parts) == 1:
                return await issues_list(
                    request,
                    owner,
                    repo.name,
                    state=request.query_params.get("state", "all"),
                    db=db,
                )
            if len(action_parts) == 2 and action_parts[1] == "new":
                return await new_issue_page(request, owner, repo.name, db=db)
            if len(action_parts) == 2 and action_parts[1].isdigit():
                return await issue_detail(request, owner, repo.name, int(action_parts[1]), db=db)

        if request.method == "POST" and action_parts[0] == "issues" and len(action_parts) == 2 and action_parts[1] == "new":
            form = await request.form()
            return await new_issue_submit(
                request,
                owner,
                repo.name,
                title=str(form.get("title") or ""),
                body=str(form.get("body") or ""),
                db=db,
            )

        if request.method == "GET" and action_parts[0] == "pulls":
            if len(action_parts) == 1:
                return await pulls_list(
                    request,
                    owner,
                    repo.name,
                    state=request.query_params.get("state", "open"),
                    db=db,
                )
            if len(action_parts) == 2 and action_parts[1] == "new":
                return await new_pull_page(request, owner, repo.name, db=db)
            if len(action_parts) == 2 and action_parts[1].isdigit():
                return await pull_detail(request, owner, repo.name, int(action_parts[1]), db=db)

        if request.method == "POST" and action_parts[0] == "pulls" and len(action_parts) == 2 and action_parts[1] == "new":
            form = await request.form()
            return await new_pull_submit(
                request,
                owner,
                repo.name,
                title=str(form.get("title") or ""),
                body=str(form.get("body") or ""),
                head_ref=str(form.get("head_ref") or ""),
                base_ref=str(form.get("base_ref") or ""),
                db=db,
            )

        if request.method == "GET" and action_parts[0] == "branches" and len(action_parts) == 1:
            return await branches_list(request, owner, repo.name, db=db)

        if request.method == "GET" and action_parts[0] == "tags" and len(action_parts) == 1:
            return await tags_list(request, owner, repo.name, db=db)

        if request.method == "GET" and action_parts[0] == "commits" and len(action_parts) >= 2:
            return await commits_list(
                request,
                owner,
                repo.name,
                "/".join(action_parts[1:]),
                page=int(request.query_params.get("page", "1")),
                db=db,
            )

        if request.method == "GET" and action_parts[0] == "commit" and len(action_parts) == 2:
            return await commit_detail_view(request, owner, repo.name, action_parts[1], db=db)

        if request.method == "GET" and action_parts[0] == "edit" and len(action_parts) >= 3:
            return await edit_file_page(
                request,
                owner,
                repo.name,
                action_parts[1],
                "/".join(action_parts[2:]),
                db=db,
            )

        if request.method == "POST" and action_parts[0] == "edit" and len(action_parts) >= 3:
            form = await request.form()
            return await edit_file_submit(
                request,
                owner,
                repo.name,
                action_parts[1],
                "/".join(action_parts[2:]),
                content=str(form.get("content") or ""),
                commit_message=str(form.get("commit_message") or ""),
                db=db,
            )

        if request.method == "POST" and action_parts[0] == "delete-file" and len(action_parts) >= 3:
            form = await request.form()
            return await delete_file_submit(
                request,
                owner,
                repo.name,
                action_parts[1],
                "/".join(action_parts[2:]),
                commit_message=str(form.get("commit_message") or ""),
                db=db,
            )

        if action_parts[0] == "-" and len(action_parts) >= 2:
            section = action_parts[1]
            if request.method == "GET":
                message = request.query_params.get("message")
                error = request.query_params.get("error")
                if section == "members" and len(action_parts) == 2:
                    return await repo_members_page(
                        request, owner, repo.name, message=message, error=error, db=db
                    )
                if section == "labels" and len(action_parts) == 2:
                    return await repo_labels_page(
                        request, owner, repo.name, message=message, error=error, db=db
                    )
                if section == "variables" and len(action_parts) == 2:
                    return await repo_variables_page(
                        request, owner, repo.name, message=message, error=error, db=db
                    )
                if section == "secrets" and len(action_parts) == 2:
                    return await repo_secrets_page(
                        request, owner, repo.name, message=message, error=error, db=db
                    )
                if section == "snippets":
                    if len(action_parts) == 2:
                        return await repo_snippets_page(
                            request, owner, repo.name, message=message, error=error, db=db
                        )
                    if len(action_parts) == 3 and action_parts[2].isdigit():
                        return await repo_snippet_detail_page(
                            request,
                            owner,
                            repo.name,
                            int(action_parts[2]),
                            message=message,
                            error=error,
                            db=db,
                        )
                if section == "milestones" and len(action_parts) == 2:
                    return await repo_milestones_page(
                        request, owner, repo.name, message=message, error=error, db=db
                    )
                if section == "releases" and len(action_parts) == 2:
                    return await repo_releases_page(
                        request, owner, repo.name, message=message, error=error, db=db
                    )
                if section == "deploy_keys" and len(action_parts) == 2:
                    return await repo_deploy_keys_page(
                        request, owner, repo.name, message=message, error=error, db=db
                    )
                if section == "hooks" and len(action_parts) == 2:
                    return await repo_webhooks_page(
                        request, owner, repo.name, message=message, error=error, db=db
                    )
                if section == "pipeline_schedules":
                    if len(action_parts) == 2:
                        return await repo_pipeline_schedules_page(
                            request, owner, repo.name, message=message, error=error, db=db
                        )
                    if len(action_parts) == 3 and action_parts[2] == "new":
                        return await repo_pipeline_schedule_new_page(
                            request, owner, repo.name, error=error, db=db
                        )

            if request.method == "GET" and section == "pipelines":
                if len(action_parts) == 2:
                    return await repo_pipelines_page(
                        request,
                        owner,
                        repo.name,
                        scope=request.query_params.get("scope", "all"),
                        flash_message=request.query_params.get("flash_message"),
                        flash_type=request.query_params.get("flash_type", "info"),
                        db=db,
                    )
                if len(action_parts) == 3 and action_parts[2] == "new":
                    return await repo_new_pipeline_page(
                        request,
                        owner,
                        repo.name,
                        error=request.query_params.get("error"),
                        db=db,
                    )
                if len(action_parts) == 3 and action_parts[2].isdigit():
                    return await _repo_ci_template(
                        request,
                        owner,
                        repo,
                        current_user,
                        db,
                        pipeline_id=int(action_parts[2]),
                        flash_message=request.query_params.get("flash_message"),
                        flash_type=request.query_params.get("flash_type", "info"),
                    )

            if request.method == "POST" and section == "pipelines":
                if not current_user:
                    return RedirectResponse(url="/ui/login", status_code=302)
                if not await _can_manage_repo(current_user, repo, db):
                    return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)
                if len(action_parts) == 2:
                    form = await request.form()
                    ref = str(form.get("ref") or default_branch).strip() or default_branch
                    try:
                        variables = _pipeline_variables_from_form(
                            str(form.get("variable_key") or ""),
                            str(form.get("variable_value") or ""),
                            str(form.get("variable_type") or "env_var"),
                        )
                        pipeline = await _create_pipeline(
                            repo_id,
                            CreatePipelineRequest(ref=ref, variables=variables),
                            db,
                            source="web",
                            actor=current_user,
                        )
                    except Exception as exc:
                        await db.rollback()
                        detail = exc.detail if hasattr(exc, "detail") else str(exc)
                        return RedirectResponse(
                            url=f"/ui/{repo_full_name}/-/pipelines/new?{urlencode({'error': detail})}",
                            status_code=302,
                        )
                    return _repo_ci_redirect(
                        owner,
                        repo_name,
                        pipeline_id=pipeline.id,
                        flash_message=f"Pipeline #{pipeline.id} created.",
                        flash_type="success",
                    )
                if (
                    len(action_parts) == 4
                    and action_parts[2].isdigit()
                    and action_parts[3] == "cancel"
                ):
                    pipeline = (
                        await db.execute(
                            select(Pipeline)
                            .options(selectinload(Pipeline.jobs))
                            .where(Pipeline.project_id == repo.id, Pipeline.id == int(action_parts[2]))
                        )
                    ).scalar_one_or_none()
                    if pipeline is None:
                        return HTMLResponse(content="<h1>404 - Pipeline Not Found</h1>", status_code=404)
                    now = datetime.now(timezone.utc)
                    for job in pipeline.jobs:
                        if job.status in {"pending", "running", "manual", "scheduled"}:
                            job.status = "canceled"
                            job.finished_at = job.finished_at or now
                    await _derive_pipeline_status(pipeline, db)
                    await db.commit()
                    return _repo_ci_redirect(
                        owner,
                        repo.name,
                        pipeline_id=pipeline.id,
                        flash_message="Pipeline canceled.",
                        flash_type="success",
                    )

            if section == "ci" and len(action_parts) >= 3 and action_parts[2] == "editor":
                if request.method == "GET" and len(action_parts) == 3:
                    return await repo_pipeline_editor_page(
                        request,
                        owner,
                        repo.name,
                        ref=request.query_params.get("ref"),
                        error=request.query_params.get("error"),
                        saved=request.query_params.get("saved") in {"1", "true", "True"},
                        db=db,
                    )
                if request.method == "POST" and len(action_parts) == 3:
                    if not current_user:
                        return RedirectResponse(url="/ui/login", status_code=302)
                    if not await _can_manage_repo(current_user, repo, db):
                        return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)
                    form = await request.form()
                    selected_ref = str(form.get("ref") or default_branch).strip()
                    content = str(form.get("content") or "")
                    commit_message = str(form.get("commit_message") or "Update .gitlab-ci.yml")
                    try:
                        await write_file(
                            disk_path=repo.disk_path,
                            branch=selected_ref,
                            path=".gitlab-ci.yml",
                            content=content.encode("utf-8"),
                            message=commit_message,
                            author_name=current_user.name or current_user.login,
                            author_email=current_user.email or f"{current_user.login}@users.noreply.gitlab-emulator.local",
                        )
                    except Exception as exc:
                        return RedirectResponse(
                            url=f"/ui/{repo_full_name}/-/ci/editor?{urlencode({'ref': selected_ref, 'error': str(exc)})}",
                            status_code=302,
                        )
                    return RedirectResponse(
                        url=f"/ui/{repo_full_name}/-/ci/editor?{urlencode({'ref': selected_ref, 'saved': 'true'})}",
                        status_code=302,
                    )

            if request.method == "GET" and section == "jobs":
                if len(action_parts) == 2:
                    jobs = list((
                        await db.execute(
                            select(PipelineJob)
                            .options(selectinload(PipelineJob.pipeline))
                            .where(PipelineJob.project_id == repo.id)
                            .order_by(PipelineJob.id.desc())
                            .limit(100)
                        )
                    ).scalars().all())
                    return templates.TemplateResponse(
                        request=request,
                        name="repo_jobs.html",
                        context=_ctx(
                            request,
                            owner=owner,
                            repo=repo,
                            repo_name=repo.name,
                            current_user=current_user,
                            jobs=jobs,
                            job_diagnostics=await _job_scheduling_diagnostics(db, jobs),
                        ),
                    )
                if len(action_parts) == 3 and action_parts[2].isdigit():
                    return await _repo_ci_template(
                        request,
                        owner,
                        repo,
                        current_user,
                        db,
                        job_id=int(action_parts[2]),
                        flash_message=request.query_params.get("flash_message"),
                        flash_type=request.query_params.get("flash_type", "info"),
                    )

            if (
                request.method == "POST"
                and section == "jobs"
                and len(action_parts) == 4
                and action_parts[2].isdigit()
            ):
                if not current_user:
                    return RedirectResponse(url="/ui/login", status_code=302)
                if not await _can_manage_repo(current_user, repo, db):
                    return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)
                action = action_parts[3]
                job = (
                    await db.execute(
                        select(PipelineJob)
                        .options(
                            selectinload(PipelineJob.pipeline).selectinload(Pipeline.jobs),
                            selectinload(PipelineJob.trace),
                        )
                        .where(PipelineJob.project_id == repo.id, PipelineJob.id == int(action_parts[2]))
                    )
                ).scalar_one_or_none()
                if job is None:
                    return HTMLResponse(content="<h1>404 - Job Not Found</h1>", status_code=404)
                now = datetime.now(timezone.utc)
                message = "Job updated."
                if action == "cancel":
                    if job.status in {"pending", "running", "manual", "scheduled"}:
                        job.status = "canceled"
                        job.finished_at = job.finished_at or now
                    message = "Job canceled."
                elif action == "play":
                    if job.status != "manual":
                        return _repo_ci_redirect(
                            owner, repo.name, pipeline_id=job.pipeline_id, job_id=job.id,
                            flash_message="Job is not playable.", flash_type="error",
                        )
                    job.status = "pending"
                    job.queued_at = now
                    job.failure_reason = None
                    job.exit_code = None
                    message = "Job queued."
                elif action == "retry":
                    if job.status not in {"failed", "canceled", "skipped", "success"}:
                        return _repo_ci_redirect(
                            owner, repo.name, pipeline_id=job.pipeline_id, job_id=job.id,
                            flash_message="Job is not retryable.", flash_type="error",
                        )
                    _reset_job_for_retry(job, now)
                    message = "Job retried."
                else:
                    return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
                await _derive_pipeline_status(job.pipeline, db)
                if job.pipeline.status in {"pending", "running"}:
                    job.pipeline.finished_at = None
                await db.commit()
                return _repo_ci_redirect(
                    owner,
                    repo.name,
                    job_id=job.id,
                    flash_message=message,
                    flash_type="success",
                )

            if request.method == "GET" and section == "artifacts" and len(action_parts) == 2:
                rows = (
                    await db.execute(
                        select(JobArtifact, PipelineJob, Pipeline)
                        .join(PipelineJob, JobArtifact.job_id == PipelineJob.id)
                        .join(Pipeline, PipelineJob.pipeline_id == Pipeline.id)
                        .where(Pipeline.project_id == repo.id)
                        .order_by(JobArtifact.created_at.desc(), JobArtifact.id.desc())
                        .limit(100)
                    )
                ).all()
                artifacts = [
                    {
                        "artifact": artifact,
                        "job": job,
                        "pipeline": pipeline,
                        "download_url": f"/api/v4/projects/{repo.id}/jobs/{job.id}/artifacts",
                    }
                    for artifact, job, pipeline in rows
                ]
                return templates.TemplateResponse(
                    request=request,
                    name="repo_artifacts.html",
                    context=_ctx(
                        request,
                        owner=owner,
                        repo=repo,
                        repo_name=repo.name,
                        current_user=current_user,
                        artifacts=artifacts,
                    ),
                )

            return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

        if action_parts[0] == "new" and len(action_parts) >= 2:
            if not current_user:
                return RedirectResponse(url="/ui/login", status_code=302)
            if not await _can_manage_repo(current_user, repo, db):
                return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)
            ref = action_parts[1]
            dir_path = "/".join(action_parts[2:])
            if request.method == "POST":
                form = await request.form()
                filename = str(form.get("filename") or "")
                content = str(form.get("content") or "")
                commit_message = str(form.get("commit_message") or "")
                full_path = f"{dir_path}/{filename}" if dir_path else filename
                if not commit_message:
                    commit_message = f"Create {full_path}"
                try:
                    await write_file(
                        disk_path=repo.disk_path,
                        branch=ref,
                        path=full_path,
                        content=content.encode("utf-8"),
                        message=commit_message,
                        author_name=current_user.name or current_user.login,
                        author_email=current_user.email or f"{current_user.login}@users.noreply.gitlab-emulator.local",
                    )
                    return RedirectResponse(
                        url=f"/ui/{repo.full_name}/blob/{ref}/{full_path}",
                        status_code=302,
                    )
                except Exception as exc:
                    return templates.TemplateResponse(
                        request=request,
                        name="new_file.html",
                        context=_ctx(
                            request, owner=owner, repo=repo, repo_name=repo.name,
                            ref=ref, dir_path=dir_path, current_user=current_user,
                            error=str(exc),
                        ),
                    )
            return templates.TemplateResponse(
                request=request,
                name="new_file.html",
                context=_ctx(
                    request, owner=owner, repo=repo, repo_name=repo.name,
                    ref=ref, dir_path=dir_path, current_user=current_user, error=None,
                ),
            )

        if request.method == "GET" and action_parts[0] == "blob" and len(action_parts) >= 3:
            ref = action_parts[1]
            path = "/".join(action_parts[2:])
            content = None
            if repo.disk_path and os.path.isdir(repo.disk_path):
                raw = await get_file_content(repo.disk_path, ref, path)
                if raw:
                    try:
                        content = raw.decode("utf-8", errors="replace")
                    except Exception:
                        content = None
            return templates.TemplateResponse(
                request=request,
                name="blob.html",
                context=_ctx(
                    request, owner=owner, repo=repo, repo_name=repo.name,
                    ref=ref, path=path, content=content,
                    current_user=current_user,
                ),
            )

        if request.method == "GET" and action_parts[0] == "tree" and len(action_parts) >= 3:
            return await tree_view(
                request,
                owner,
                repo.name,
                action_parts[1],
                "/".join(action_parts[2:]),
                db=db,
            )

        if request.method == "GET" and action_parts[0] == "raw" and len(action_parts) >= 3:
            return await raw_file_view(
                owner,
                repo.name,
                action_parts[1],
                "/".join(action_parts[2:]),
                db=db,
            )

        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)

    tree_entries = None
    readme_content = None
    default_branch = repo.default_branch or "main"
    commit_count = 0
    branch_count = 0
    tag_count = 0

    if repo.disk_path and os.path.isdir(repo.disk_path):
        tree_entries = await list_tree(repo.disk_path, default_branch)
        if tree_entries:
            tree_entries.sort(key=lambda e: (0 if e["type"] == "tree" else 1, e["name"]))
            for entry in tree_entries:
                if entry["name"].lower().startswith("readme"):
                    raw = await get_file_content(
                        repo.disk_path, default_branch, entry["name"]
                    )
                    if raw:
                        readme_content = raw.decode("utf-8", errors="replace")
                    break

        commit_count = await get_commit_count(repo.disk_path, default_branch)
        branch_count = len(await get_branches(repo.disk_path))
        tag_count = len(await get_tags(repo.disk_path))

    pr_issue_ids = select(PullRequest.issue_id)
    open_issues_count = (await db.execute(
        select(func.count(Issue.id)).where(
            Issue.repo_id == repo.id, Issue.state == "open",
            ~Issue.id.in_(pr_issue_ids),
        )
    )).scalar() or 0
    open_pulls_count = (await db.execute(
        select(func.count(Issue.id)).where(
            Issue.repo_id == repo.id, Issue.state == "open",
            Issue.id.in_(pr_issue_ids),
        )
    )).scalar() or 0

    return templates.TemplateResponse(
        request=request,
        name="repo.html",
        context=_ctx(
            request,
            owner=owner,
            repo=repo,
            repo_name=repo.name,
            tree_entries=tree_entries,
            readme_content=readme_content,
            default_branch=default_branch,
            open_issues_count=open_issues_count,
            open_pulls_count=open_pulls_count,
            commit_count=commit_count,
            branch_count=branch_count,
            tag_count=tag_count,
            current_user=current_user,
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_repo(
    db: AsyncSession, owner: str, repo_name: str
) -> Optional[Repository]:
    """Look up a repository by owner login and repo name."""
    full_name = f"{owner}/{repo_name}"
    return await _get_repo_by_full_path(db, full_name)


async def _get_repo_by_full_path(
    db: AsyncSession, full_name: str
) -> Optional[Repository]:
    """Look up a repository by its full GitLab project path."""
    normalized = full_name.strip("/")
    result = await db.execute(
        select(Repository).where(Repository.full_name == normalized)
    )
    return result.scalar_one_or_none()


async def _resolve_repo_and_remainder(
    db: AsyncSession, project_path: str
) -> tuple[Optional[Repository], list[str]]:
    """Resolve the longest repository full path prefix and return trailing parts."""
    parts = [part for part in project_path.strip("/").split("/") if part]
    for index in range(len(parts), 1, -1):
        repo = await _get_repo_by_full_path(db, "/".join(parts[:index]))
        if repo is not None:
            return repo, parts[index:]
    return None, []
