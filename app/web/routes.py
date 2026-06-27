"""Public web frontend routes for browsing repos, issues, PRs, and code."""

import os
import shutil
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jose import JWSError, jws
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.pipelines import (
    CreatePipelineRequest,
    _create_pipeline,
    _derive_pipeline_status,
    _reset_job_for_retry,
)
from app.config import settings
from app.database import get_db
from app.git.bare_repo import (
    delete_file,
    get_branches,
    get_commit_count,
    get_commit_diff,
    get_commit_info,
    get_default_branch,
    get_file_content,
    get_log,
    get_tags,
    list_tree,
    write_file,
)
from app.models.comment import IssueComment
from app.models.ci import Pipeline, PipelineJob
from app.models.issue import Issue
from app.models.organization import Organization
from app.models.pull_request import PullRequest
from app.models.repository import Repository
from app.models.user import User
from app.services.auth_service import verify_password
from app.services.ci_security import normalize_ci_security_settings
from app.services import issue_service, pr_service, repo_service
from app.services.repo_service import REPO_NAME_PATTERN

_WEB_DIR = os.path.dirname(os.path.abspath(__file__))
_TEMPLATES_DIR = os.path.join(_WEB_DIR, "templates")

templates = Jinja2Templates(directory=_TEMPLATES_DIR)

router = APIRouter(prefix="/ui", tags=["web"])

_URL_PREFIX = "/ui"


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
    return context


def _can_manage_repo(user: Optional[User], repo: Repository) -> bool:
    """Return whether a UI user can mutate repository settings or source."""
    return bool(user and (user.site_admin or user.id == repo.owner_id))


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
    return templates.TemplateResponse(
        request=request,
        name="new_repo.html",
        context=_ctx(request, current_user=current_user, error=None),
    )


@router.post("/new", response_class=HTMLResponse)
async def new_repo_submit(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    private: bool = Form(False),
    auto_init: bool = Form(False),
    db: AsyncSession = Depends(get_db),
):
    """Create a new repository."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)

    try:
        repo = await repo_service.create_repo(
            db,
            owner=current_user,
            name=name,
            description=description or None,
            private=private,
            auto_init=auto_init,
        )
        return RedirectResponse(
            url=f"/ui/{current_user.login}/{repo.name}", status_code=302
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="new_repo.html",
            context=_ctx(request, current_user=current_user, error=str(exc)),
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
    if not _can_manage_repo(current_user, repo):
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
    if not _can_manage_repo(current_user, repo):
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
    if not _can_manage_repo(current_user, repo):
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
# Repository CI pipelines and jobs
# ---------------------------------------------------------------------------

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
            selected_pipeline=selected_pipeline,
            jobs=jobs,
            selected_job=selected_job,
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
    pipeline_id: int | None = Query(None),
    job_id: int | None = Query(None),
    flash_message: str | None = Query(None),
    flash_type: str = Query("info"),
    db: AsyncSession = Depends(get_db),
):
    """Repository-scoped CI pipeline and job interface."""
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
        job_id=job_id,
        flash_message=flash_message,
        flash_type=flash_type,
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
    db: AsyncSession = Depends(get_db),
):
    """Create a repository pipeline from committed `.gitlab-ci.yml`."""
    current_user = await _get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/ui/login", status_code=302)
    repo = await _get_repo(db, owner, repo_name)
    if repo is None:
        return HTMLResponse(content="<h1>404 - Not Found</h1>", status_code=404)
    if not _can_manage_repo(current_user, repo):
        return HTMLResponse(content="<h1>403 - Forbidden</h1>", status_code=403)

    try:
        pipeline = await _create_pipeline(
            repo.id,
            CreatePipelineRequest(ref=ref.strip() or repo.default_branch or "main"),
            db,
            source="web",
            actor=current_user,
        )
    except Exception as exc:
        await db.rollback()
        detail = exc.detail if hasattr(exc, "detail") else str(exc)
        return _repo_ci_redirect(
            owner,
            repo.name,
            flash_message=f"Could not create pipeline: {detail}",
            flash_type="error",
        )

    return _repo_ci_redirect(
        owner,
        repo.name,
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
    if not _can_manage_repo(current_user, repo):
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
        if job.status in {"pending", "running", "manual"}:
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
    if not _can_manage_repo(current_user, repo):
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
        if job.status in {"pending", "running", "manual"}:
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
    if not _can_manage_repo(current_user, repo):
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
    if not _can_manage_repo(current_user, repo):
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
    if not _can_manage_repo(current_user, repo):
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
    if not _can_manage_repo(current_user, repo):
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
    if not _can_manage_repo(current_user, repo):
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
# Helpers
# ---------------------------------------------------------------------------

async def _get_repo(
    db: AsyncSession, owner: str, repo_name: str
) -> Optional[Repository]:
    """Look up a repository by owner login and repo name."""
    full_name = f"{owner}/{repo_name}"
    result = await db.execute(
        select(Repository).where(Repository.full_name == full_name)
    )
    return result.scalar_one_or_none()
