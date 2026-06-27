"""Admin panel routes for the GitLab Emulator.

Provides a web-based admin interface for managing users, tokens, and
repositories. Authentication is handled via a signed session cookie
using python-jose JWS.
"""

import os
import re
import base64
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jose import JWSError, jws
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pipelines import (
    CreatePipelineRequest,
    _create_pipeline,
    _derive_pipeline_status,
    _requeue_stale_or_pending_job,
    _reset_job_for_retry,
)
from app.api.projects import create_project as api_create_project
from app.api.repository_files import _commit_file_change, _file_metadata
from app.api.runner import explain_job_scheduling, registered_runner_diagnostics
from app.config import settings
from app.database import get_db
from app.models.ci import CiRunner, CiSecret, CiVariable, Pipeline, PipelineJob
from app.models.event import Event
from app.models.issue import Issue
from app.models.organization import Organization
from app.models.pull_request import PullRequest
from app.models.repository import Repository
from app.models.token import PersonalAccessToken
from app.models.user import User
from app.models.import_job import ImportJob
from app.services.auth_service import hash_password, verify_password
from app.services.import_service import start_single_import, start_bulk_import
from app.services.user_service import create_token, create_user

# ---------------------------------------------------------------------------
# Templates & Router setup
# ---------------------------------------------------------------------------

_ADMIN_DIR = os.path.dirname(os.path.abspath(__file__))
_TEMPLATES_DIR = os.path.join(_ADMIN_DIR, "templates")
_STATIC_DIR = os.path.join(_ADMIN_DIR, "static")

templates = Jinja2Templates(directory=_TEMPLATES_DIR)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Session helpers (signed cookie via python-jose JWS)
# ---------------------------------------------------------------------------

_ALGORITHM = "HS256"


def _sign_session(username: str) -> str:
    """Create a JWS-signed session token containing the admin username."""
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


def _get_admin_user(request: Request) -> Optional[str]:
    """Extract the admin username from the session cookie."""
    token = request.cookies.get("admin_session")
    if not token:
        return None
    return _verify_session(token)


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def _require_admin(request: Request) -> Optional[str]:
    """Return the admin username or None (used to decide redirect)."""
    return _get_admin_user(request)


# ---------------------------------------------------------------------------
# Helper to build template context
# ---------------------------------------------------------------------------

def _ctx(
    request: Request,
    admin_user: Optional[str],
    flash_message: Optional[str] = None,
    flash_type: str = "info",
    **extra,
) -> dict:
    """Build the base template context dictionary."""
    context = {
        "admin_user": admin_user,
        "flash_message": flash_message,
        "flash_type": flash_type,
    }
    context.update(extra)
    return context


# ---------------------------------------------------------------------------
# Static files mount helper
# ---------------------------------------------------------------------------

def get_static_files_app():
    """Return a StaticFiles app for the admin static directory."""
    return StaticFiles(directory=_STATIC_DIR)


async def _admin_user_object(admin_user: str, db: AsyncSession) -> User | None:
    result = await db.execute(select(User).where(User.login == admin_user))
    return result.scalar_one_or_none()


def _ci_lab_redirect(
    *,
    project_id: int | None = None,
    pipeline_id: int | None = None,
    job_id: int | None = None,
    flash_message: str | None = None,
    flash_type: str = "info",
) -> RedirectResponse:
    params: dict[str, str] = {}
    if project_id:
        params["project_id"] = str(project_id)
    if pipeline_id:
        params["pipeline_id"] = str(pipeline_id)
    if job_id:
        params["job_id"] = str(job_id)
    if flash_message:
        params["flash_message"] = flash_message
        params["flash_type"] = flash_type
    suffix = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(url=f"/admin/ci-lab{suffix}", status_code=302)


def _project_slug(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip().lower()).strip("-._")
    return slug or "ci-lab-project"


def _ci_lab_selected_url(
    project_id: int | None,
    pipeline_id: int | None,
    job_id: int | None,
) -> str:
    params: dict[str, str] = {}
    if project_id:
        params["project_id"] = str(project_id)
    if pipeline_id:
        params["pipeline_id"] = str(pipeline_id)
    if job_id:
        params["job_id"] = str(job_id)
    suffix = f"?{urlencode(params)}" if params else ""
    return f"/admin/ci-lab{suffix}"


def _runners_redirect(
    *,
    runner_id: int | None = None,
    flash_message: str | None = None,
    flash_type: str = "info",
) -> RedirectResponse:
    params: dict[str, str] = {}
    if flash_message:
        params["flash_message"] = flash_message
        params["flash_type"] = flash_type
    suffix = f"?{urlencode(params)}" if params else ""
    url = f"/admin/runners/{runner_id}{suffix}" if runner_id else f"/admin/runners{suffix}"
    return RedirectResponse(url=url, status_code=302)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _runner_readiness(runner_diagnostics: dict) -> dict:
    last_contact = _as_utc(runner_diagnostics.get("last_contact_at"))
    last_poll = _as_utc(runner_diagnostics.get("last_poll_at"))
    now = datetime.now(timezone.utc)
    if runner_diagnostics.get("paused"):
        return {
            "state": "paused",
            "label": "paused",
            "detail": "Runner is registered but paused.",
            "healthy": False,
        }
    if last_contact is None:
        return {
            "state": "offline",
            "label": "no contact",
            "detail": "Runner has not contacted the emulator.",
            "healthy": False,
        }
    if last_poll is not None and (now - last_poll).total_seconds() <= 120:
        return {
            "state": "polling",
            "label": "polling",
            "detail": "Runner has polled recently and can receive eligible jobs.",
            "healthy": True,
        }
    return {
        "state": "contacted",
        "label": "contacted",
        "detail": "Runner registered previously, but has not polled recently.",
        "healthy": True,
    }


def _runner_status(runner: CiRunner) -> str:
    if runner.paused:
        return "paused"
    if runner.last_poll_at is not None:
        return "polling"
    if runner.last_contact_at is not None:
        return "contacted"
    return "offline"


def _runner_job_names(runner: CiRunner) -> set[str]:
    return {name for name in [runner.runner_name, runner.description] if name}


_CI_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CI_VARIABLE_TYPES = {"env_var", "file"}


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


def _bool_form(value: str | None) -> bool:
    return value == "1"


def _validate_ci_key(value: str, label: str = "Key") -> str:
    normalized = (value or "").strip()
    if not _CI_KEY_RE.match(normalized):
        raise ValueError(
            f"{label} must start with a letter or underscore and contain only letters, numbers, and underscores."
        )
    return normalized


def _admin_error_url(base_url: str, message: str) -> str:
    return f"{base_url}?flash_type=error&flash_message={urlencode({'x': message})[2:]}"


def _admin_success_url(base_url: str, message: str) -> str:
    return f"{base_url}?flash_type=success&flash_message={urlencode({'x': message})[2:]}"


async def _runner_recent_jobs(
    db: AsyncSession,
    runner: CiRunner,
    *,
    limit: int = 25,
) -> list[PipelineJob]:
    runner_names = _runner_job_names(runner)
    if not runner_names:
        return []
    result = await db.execute(
        select(PipelineJob)
        .options(selectinload(PipelineJob.project), selectinload(PipelineJob.pipeline))
        .where(PipelineJob.runner_name.in_(runner_names))
        .order_by(PipelineJob.updated_at.desc(), PipelineJob.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Routes: Instance CI/CD variables
# ---------------------------------------------------------------------------

@router.get("/ci-variables", response_class=HTMLResponse)
async def instance_ci_variables_page(
    request: Request,
    flash_message: str | None = None,
    flash_type: str = "info",
    db: AsyncSession = Depends(get_db),
):
    """Manage instance-scoped CI/CD variables."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)
    result = await db.execute(
        select(CiVariable)
        .where(CiVariable.scope_type == "instance", CiVariable.scope_id.is_(None))
        .order_by(CiVariable.key, CiVariable.environment_scope)
    )
    return templates.TemplateResponse(
        request=request,
        name="ci_variables.html",
        context=_ctx(
            request,
            admin_user=admin_user,
            flash_message=flash_message,
            flash_type=flash_type,
            variables=result.scalars().all(),
            variable_flags=_ci_variable_flags,
        ),
    )


@router.post("/ci-variables", response_class=HTMLResponse)
async def create_instance_ci_variable(
    request: Request,
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
    """Create an instance-scoped CI/CD variable."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)
    redirect = "/admin/ci-variables"
    try:
        if variable_type not in _CI_VARIABLE_TYPES:
            raise ValueError("Variable type must be env_var or file.")
        db.add(
            CiVariable(
                scope_type="instance",
                scope_id=None,
                key=_validate_ci_key(key),
                value=value,
                variable_type=variable_type,
                visibility=_ci_visibility(_bool_form(masked), _bool_form(hidden)),
                protected=_bool_form(protected),
                raw=_bool_form(raw),
                environment_scope=environment_scope.strip() or "*",
                description=description.strip() or None,
            )
        )
        await db.commit()
    except (ValueError, IntegrityError) as exc:
        await db.rollback()
        message = (
            "Variable already exists for that environment scope."
            if isinstance(exc, IntegrityError)
            else str(exc)
        )
        return RedirectResponse(url=_admin_error_url(redirect, message), status_code=302)
    return RedirectResponse(
        url=_admin_success_url(redirect, "Variable created."),
        status_code=302,
    )


@router.post("/ci-variables/{variable_id}/update", response_class=HTMLResponse)
async def update_instance_ci_variable(
    request: Request,
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
    """Update an instance-scoped CI/CD variable."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)
    redirect = "/admin/ci-variables"
    variable = (
        await db.execute(
            select(CiVariable).where(
                CiVariable.id == variable_id,
                CiVariable.scope_type == "instance",
                CiVariable.scope_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    if variable is None:
        return RedirectResponse(
            url=_admin_error_url(redirect, "Variable not found."),
            status_code=302,
        )
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
        message = (
            "Variable already exists for that environment scope."
            if isinstance(exc, IntegrityError)
            else str(exc)
        )
        return RedirectResponse(url=_admin_error_url(redirect, message), status_code=302)
    return RedirectResponse(
        url=_admin_success_url(redirect, "Variable updated."),
        status_code=302,
    )


@router.post("/ci-variables/{variable_id}/delete", response_class=HTMLResponse)
async def delete_instance_ci_variable(
    request: Request,
    variable_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete an instance-scoped CI/CD variable."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)
    variable = (
        await db.execute(
            select(CiVariable).where(
                CiVariable.id == variable_id,
                CiVariable.scope_type == "instance",
                CiVariable.scope_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    if variable is not None:
        await db.delete(variable)
        await db.commit()
    return RedirectResponse(
        url=_admin_success_url("/admin/ci-variables", "Variable deleted."),
        status_code=302,
    )


async def _selected_ci_lab_state(
    project_id: int | None,
    pipeline_id: int | None,
    job_id: int | None,
    db: AsyncSession,
) -> dict:
    result = await db.execute(select(Repository).order_by(Repository.id.desc()))
    projects = list(result.scalars().all())
    selected_project = None
    if project_id is not None:
        selected_project = (
            await db.execute(select(Repository).where(Repository.id == project_id))
        ).scalar_one_or_none()
    elif projects:
        selected_project = projects[0]

    ci_yaml = ""
    pipelines: list[Pipeline] = []
    selected_pipeline = None
    jobs: list[PipelineJob] = []
    selected_job = None
    trace_text = ""
    runner_diagnostics = await registered_runner_diagnostics(db)
    runner_readiness = _runner_readiness(runner_diagnostics)
    job_diagnostics: dict[int, dict] = {}

    if selected_project is not None:
        try:
            metadata = await _file_metadata(
                selected_project,
                ".gitlab-ci.yml",
                selected_project.default_branch,
            )
            ci_yaml = base64.b64decode(metadata["content"]).decode()
        except Exception:
            ci_yaml = DEFAULT_CI_LAB_YAML

        pipeline_result = await db.execute(
            select(Pipeline)
            .where(Pipeline.project_id == selected_project.id)
            .order_by(Pipeline.id.desc())
            .limit(20)
        )
        pipelines = list(pipeline_result.scalars().all())
        if pipeline_id is not None:
            selected_pipeline = (
                await db.execute(
                    select(Pipeline)
                    .where(
                        Pipeline.project_id == selected_project.id,
                        Pipeline.id == pipeline_id,
                    )
                )
            ).scalar_one_or_none()
        elif pipelines:
            selected_pipeline = pipelines[0]

    if selected_pipeline is not None:
        jobs_result = await db.execute(
            select(PipelineJob)
            .options(selectinload(PipelineJob.trace), selectinload(PipelineJob.artifacts))
            .where(PipelineJob.pipeline_id == selected_pipeline.id)
            .order_by(PipelineJob.stage_index.asc(), PipelineJob.id.asc())
        )
        jobs = list(jobs_result.scalars().all())
        if job_id is not None:
            selected_job = next((job for job in jobs if job.id == job_id), None)
        elif jobs:
            selected_job = jobs[0]
        job_diagnostics = explain_job_scheduling(jobs, runner_diagnostics)

    if selected_job is not None and selected_job.trace is not None:
        trace_text = selected_job.trace.content or ""

    selected_project_id = selected_project.id if selected_project else None
    selected_pipeline_id = selected_pipeline.id if selected_pipeline else None
    selected_job_id = selected_job.id if selected_job else None
    selected_ci_lab_url = _ci_lab_selected_url(
        selected_project_id,
        selected_pipeline_id,
        selected_job_id,
    )

    return {
        "projects": projects,
        "selected_project": selected_project,
        "ci_yaml": ci_yaml,
        "pipelines": pipelines,
        "selected_pipeline": selected_pipeline,
        "jobs": jobs,
        "selected_job": selected_job,
        "trace_text": trace_text,
        "runner_diagnostics": runner_diagnostics,
        "runner_readiness": runner_readiness,
        "job_diagnostics": job_diagnostics,
        "selected_ci_lab_url": selected_ci_lab_url,
        "selected_trace_url": f"{selected_ci_lab_url}#job-trace" if selected_job else None,
        "selected_job_api_url": f"/api/v4/projects/{selected_project_id}/jobs/{selected_job_id}"
        if selected_project and selected_job
        else None,
        "selected_job_trace_api_url": f"/api/v4/projects/{selected_project_id}/jobs/{selected_job_id}/trace"
        if selected_project and selected_job
        else None,
        "selected_job_artifacts_api_url": f"/api/v4/projects/{selected_project_id}/jobs/{selected_job_id}/artifacts"
        if selected_project and selected_job
        else None,
    }


DEFAULT_CI_LAB_YAML = """stages:
  - build
  - test

build:
  image: alpine:3.20
  stage: build
  script:
    - echo building from CI Lab
    - mkdir -p out
    - echo result > out/result.txt
  artifacts:
    paths:
      - out/result.txt

manual_review:
  image: alpine:3.20
  stage: test
  script:
    - echo manual review
  rules:
    - when: manual
"""


# ---------------------------------------------------------------------------
# Routes: Login / Logout
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Render the admin login page."""
    admin_user = _get_admin_user(request)
    if admin_user:
        return RedirectResponse(url="/admin/", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context=_ctx(request, admin_user=None),
    )


@router.post("/login", response_class=HTMLResponse)
async def login_handler(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Handle admin login form submission."""
    # Look up the user
    result = await db.execute(select(User).where(User.login == username))
    user = result.scalar_one_or_none()

    if user is None or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context=_ctx(
                request,
                admin_user=None,
                flash_message="Invalid username or password.",
                flash_type="error",
            ),
        )

    if not user.site_admin:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context=_ctx(
                request,
                admin_user=None,
                flash_message="User is not a site administrator.",
                flash_type="error",
            ),
        )

    # Set signed session cookie
    response = RedirectResponse(url="/admin/", status_code=302)
    session_token = _sign_session(user.login)
    response.set_cookie(
        key="admin_session",
        value=session_token,
        httponly=True,
        samesite="lax",
        path="/admin",
    )
    return response


@router.get("/logout")
async def logout(request: Request):
    """Clear the admin session cookie and redirect to login."""
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie(key="admin_session", path="/admin")
    return response


# ---------------------------------------------------------------------------
# Routes: Dashboard
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Render the admin dashboard with system statistics."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    # Gather stats
    users_count = (await db.execute(select(func.count(User.id)))).scalar() or 0
    repos_count = (await db.execute(select(func.count(Repository.id)))).scalar() or 0
    issues_count = (
        await db.execute(
            select(func.count(Issue.id)).where(Issue.state == "open")
        )
    ).scalar() or 0
    prs_count = (
        await db.execute(
            select(func.count(PullRequest.id)).where(PullRequest.merged == False)  # noqa: E712
        )
    ).scalar() or 0
    tokens_count = (
        await db.execute(select(func.count(PersonalAccessToken.id)))
    ).scalar() or 0

    # Recent events
    result = await db.execute(
        select(Event).order_by(Event.created_at.desc()).limit(20)
    )
    recent_events = list(result.scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=_ctx(
            request,
            admin_user=admin_user,
            users_count=users_count,
            repos_count=repos_count,
            issues_count=issues_count,
            prs_count=prs_count,
            tokens_count=tokens_count,
            recent_events=recent_events,
        ),
    )


# ---------------------------------------------------------------------------
# Routes: CI Lab
# ---------------------------------------------------------------------------

@router.get("/ci-lab", response_class=HTMLResponse)
async def ci_lab(
    request: Request,
    project_id: int | None = None,
    pipeline_id: int | None = None,
    job_id: int | None = None,
    flash_message: str | None = None,
    flash_type: str = "info",
    db: AsyncSession = Depends(get_db),
):
    """Render a compact CI job creation and testing console."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    state = await _selected_ci_lab_state(project_id, pipeline_id, job_id, db)
    return templates.TemplateResponse(
        request=request,
        name="ci_lab.html",
        context=_ctx(
            request,
            admin_user=admin_user,
            flash_message=flash_message,
            flash_type=flash_type,
            **state,
        ),
    )


@router.post("/ci-lab/projects", response_class=HTMLResponse)
async def ci_lab_create_project(
    request: Request,
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Create a project for CI Lab experiments."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)
    user = await _admin_user_object(admin_user, db)
    if user is None:
        return RedirectResponse(url="/admin/login", status_code=302)

    try:
        payload = await api_create_project(
            {
                "name": name.strip(),
                "path": _project_slug(name),
                "visibility": "public",
                "initialize_with_readme": True,
            },
            user,
            db,
        )
    except HTTPException as exc:
        return _ci_lab_redirect(flash_message=str(exc.detail), flash_type="error")

    return _ci_lab_redirect(
        project_id=payload["id"],
        flash_message="Project created.",
        flash_type="success",
    )


@router.post("/ci-lab/yaml", response_class=HTMLResponse)
async def ci_lab_save_yaml(
    request: Request,
    project_id: int = Form(...),
    ci_yaml: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Write `.gitlab-ci.yml` to the selected project."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    project = (
        await db.execute(select(Repository).where(Repository.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        return _ci_lab_redirect(flash_message="Project not found.", flash_type="error")

    try:
        try:
            await _file_metadata(project, ".gitlab-ci.yml", project.default_branch)
            message = "Update CI Lab pipeline"
        except Exception:
            message = "Create CI Lab pipeline"
        await _commit_file_change(
            project,
            project.default_branch,
            ".gitlab-ci.yml",
            message,
            ci_yaml.encode(),
        )
        await db.commit()
    except HTTPException as exc:
        return _ci_lab_redirect(
            project_id=project.id,
            flash_message=str(exc.detail),
            flash_type="error",
        )
    except Exception as exc:
        await db.rollback()
        return _ci_lab_redirect(
            project_id=project.id,
            flash_message=f"Could not save CI YAML: {exc}",
            flash_type="error",
        )

    return _ci_lab_redirect(
        project_id=project.id,
        flash_message=".gitlab-ci.yml saved.",
        flash_type="success",
    )


@router.post("/ci-lab/pipelines", response_class=HTMLResponse)
async def ci_lab_create_pipeline(
    request: Request,
    project_id: int = Form(...),
    ref: str = Form("main"),
    db: AsyncSession = Depends(get_db),
):
    """Create a pipeline from the selected project's `.gitlab-ci.yml`."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    try:
        pipeline = await _create_pipeline(
            project_id,
            CreatePipelineRequest(ref=ref.strip() or "main"),
            db,
            source="web",
        )
    except HTTPException as exc:
        return _ci_lab_redirect(
            project_id=project_id,
            flash_message=str(exc.detail),
            flash_type="error",
        )
    except Exception as exc:
        await db.rollback()
        return _ci_lab_redirect(
            project_id=project_id,
            flash_message=f"Could not create pipeline: {exc}",
            flash_type="error",
        )

    return _ci_lab_redirect(
        project_id=project_id,
        pipeline_id=pipeline.id,
        flash_message=f"Pipeline {pipeline.id} created.",
        flash_type="success",
    )


@router.post("/ci-lab/pipelines/{pipeline_id}/cancel", response_class=HTMLResponse)
async def ci_lab_cancel_pipeline(
    request: Request,
    pipeline_id: int,
    project_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Cancel runnable jobs in a pipeline."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    pipeline = (
        await db.execute(
            select(Pipeline)
            .options(selectinload(Pipeline.jobs))
            .where(Pipeline.id == pipeline_id, Pipeline.project_id == project_id)
        )
    ).scalar_one_or_none()
    if pipeline is None:
        return _ci_lab_redirect(project_id=project_id, flash_message="Pipeline not found.", flash_type="error")
    now = datetime.now(timezone.utc)
    for job in pipeline.jobs:
        if job.status in {"pending", "running", "manual"}:
            job.status = "canceled"
            job.finished_at = job.finished_at or now
    pipeline.status = "canceled"
    pipeline.finished_at = pipeline.finished_at or now
    await db.commit()
    return _ci_lab_redirect(
        project_id=project_id,
        pipeline_id=pipeline_id,
        flash_message="Pipeline canceled.",
        flash_type="success",
    )


@router.post("/ci-lab/jobs/{job_id}/{action}", response_class=HTMLResponse)
async def ci_lab_job_action(
    request: Request,
    job_id: int,
    action: str,
    project_id: int = Form(...),
    pipeline_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Play, cancel, retry, or requeue a CI job from the CI Lab."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(
        select(PipelineJob)
        .options(
            selectinload(PipelineJob.pipeline).selectinload(Pipeline.jobs),
            selectinload(PipelineJob.trace),
        )
        .where(PipelineJob.id == job_id, PipelineJob.project_id == project_id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        return _ci_lab_redirect(
            project_id=project_id,
            pipeline_id=pipeline_id,
            flash_message="Job not found.",
            flash_type="error",
        )

    now = datetime.now(timezone.utc)
    if action == "play":
        if job.status != "manual":
            return _ci_lab_redirect(
                project_id=project_id,
                pipeline_id=pipeline_id,
                job_id=job_id,
                flash_message="Job is not playable.",
                flash_type="error",
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
            return _ci_lab_redirect(
                project_id=project_id,
                pipeline_id=pipeline_id,
                job_id=job_id,
                flash_message="Job is not retryable.",
                flash_type="error",
            )
    elif action == "requeue":
        if job.status in {"pending", "running"}:
            _requeue_stale_or_pending_job(job, now)
            message = "Job requeued."
        else:
            return _ci_lab_redirect(
                project_id=project_id,
                pipeline_id=pipeline_id,
                job_id=job_id,
                flash_message="Only pending or running jobs can be requeued.",
                flash_type="error",
            )
    else:
        return _ci_lab_redirect(
            project_id=project_id,
            pipeline_id=pipeline_id,
            job_id=job_id,
            flash_message="Unsupported job action.",
            flash_type="error",
        )

    await _derive_pipeline_status(job.pipeline, db)
    if job.pipeline.status in {"pending", "running"}:
        job.pipeline.finished_at = None
    await db.commit()
    return _ci_lab_redirect(
        project_id=project_id,
        pipeline_id=pipeline_id,
        job_id=job_id,
        flash_message=message,
        flash_type="success",
    )


# ---------------------------------------------------------------------------
# Routes: Runners
# ---------------------------------------------------------------------------

@router.get("/runners", response_class=HTMLResponse)
async def list_runners(
    request: Request,
    flash_message: str | None = None,
    flash_type: str = "info",
    db: AsyncSession = Depends(get_db),
):
    """List registered CI runners."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(select(CiRunner).order_by(CiRunner.id.asc()))
    runners = list(result.scalars().all())
    runner_statuses = {runner.id: _runner_status(runner) for runner in runners}

    return templates.TemplateResponse(
        request=request,
        name="runners.html",
        context=_ctx(
            request,
            admin_user=admin_user,
            flash_message=flash_message,
            flash_type=flash_type,
            runners=runners,
            runner_statuses=runner_statuses,
        ),
    )


@router.get("/runners/{runner_id}", response_class=HTMLResponse)
async def runner_detail(
    request: Request,
    runner_id: int,
    flash_message: str | None = None,
    flash_type: str = "info",
    db: AsyncSession = Depends(get_db),
):
    """Show runner details and recent jobs."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(
        select(CiRunner)
        .options(selectinload(CiRunner.last_job))
        .where(CiRunner.id == runner_id)
    )
    runner = result.scalar_one_or_none()
    if runner is None:
        return RedirectResponse(url="/admin/runners", status_code=302)

    jobs = await _runner_recent_jobs(db, runner)

    return templates.TemplateResponse(
        request=request,
        name="runner_detail.html",
        context=_ctx(
            request,
            admin_user=admin_user,
            flash_message=flash_message,
            flash_type=flash_type,
            runner=runner,
            runner_status=_runner_status(runner),
            jobs=jobs,
        ),
    )


@router.post("/runners/{runner_id}/{action}", response_class=HTMLResponse)
async def runner_action(
    request: Request,
    runner_id: int,
    action: str,
    db: AsyncSession = Depends(get_db),
):
    """Pause, resume, or delete a persisted CI runner."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(select(CiRunner).where(CiRunner.id == runner_id))
    runner = result.scalar_one_or_none()
    if runner is None:
        return _runners_redirect(
            flash_message="Runner not found.",
            flash_type="error",
        )

    description = runner.description or f"runner #{runner.id}"
    if action == "pause":
        runner.paused = True
        await db.commit()
        return _runners_redirect(
            runner_id=runner.id,
            flash_message=f"Paused {description}.",
            flash_type="success",
        )
    if action == "resume":
        runner.paused = False
        await db.commit()
        return _runners_redirect(
            runner_id=runner.id,
            flash_message=f"Resumed {description}.",
            flash_type="success",
        )
    if action == "delete":
        await db.delete(runner)
        await db.commit()
        return _runners_redirect(
            flash_message=f"Deleted {description}.",
            flash_type="success",
        )

    return _runners_redirect(
        runner_id=runner.id,
        flash_message="Unsupported runner action.",
        flash_type="error",
    )


# ---------------------------------------------------------------------------
# Routes: Users
# ---------------------------------------------------------------------------

@router.get("/users", response_class=HTMLResponse)
async def list_users(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """List all users."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(select(User).order_by(User.id))
    users = list(result.scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context=_ctx(request, admin_user=admin_user, users=users),
    )


@router.get("/users/create", response_class=HTMLResponse)
async def create_user_form(request: Request):
    """Render the create-user form."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    return templates.TemplateResponse(
        request=request,
        name="user_form.html",
        context=_ctx(request, admin_user=admin_user, edit_user=None),
    )


@router.post("/users/create", response_class=HTMLResponse)
async def create_user_handler(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    name: str = Form(""),
    email: str = Form(""),
    site_admin: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Handle create-user form submission."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    # Check for duplicate login
    existing = await db.execute(select(User).where(User.login == login))
    if existing.scalar_one_or_none():
        return templates.TemplateResponse(
            request=request,
            name="user_form.html",
            context=_ctx(
                request,
                admin_user=admin_user,
                edit_user=None,
                flash_message=f"User '{login}' already exists.",
                flash_type="error",
            ),
        )

    is_admin = site_admin == "1"
    await create_user(
        db,
        login=login,
        password=password,
        name=name or None,
        email=email or None,
        site_admin=is_admin,
    )

    response = RedirectResponse(url="/admin/users", status_code=302)
    return response


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def edit_user_page(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Render the edit-user form."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return RedirectResponse(url="/admin/users", status_code=302)

    return templates.TemplateResponse(
        request=request,
        name="user_form.html",
        context=_ctx(request, admin_user=admin_user, edit_user=user),
    )


@router.post("/users/{user_id}", response_class=HTMLResponse)
async def update_user_handler(
    request: Request,
    user_id: int,
    login: str = Form(...),
    password: str = Form(""),
    name: str = Form(""),
    email: str = Form(""),
    site_admin: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Handle edit-user form submission."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return RedirectResponse(url="/admin/users", status_code=302)

    user.name = name or None
    user.email = email or None
    user.site_admin = site_admin == "1"

    if password:
        user.hashed_password = hash_password(password)

    await db.commit()

    return RedirectResponse(url="/admin/users", status_code=302)


@router.post("/users/{user_id}/delete", response_class=HTMLResponse)
async def delete_user(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a user."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user:
        await db.delete(user)
        await db.commit()

    return RedirectResponse(url="/admin/users", status_code=302)


# ---------------------------------------------------------------------------
# Routes: Tokens
# ---------------------------------------------------------------------------

@router.get("/tokens", response_class=HTMLResponse)
async def list_tokens(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """List all personal access tokens."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(
        select(PersonalAccessToken).order_by(PersonalAccessToken.id)
    )
    tokens = list(result.scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="tokens.html",
        context=_ctx(request, admin_user=admin_user, tokens=tokens),
    )


@router.get("/tokens/create", response_class=HTMLResponse)
async def create_token_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Render the create-token form."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(select(User).order_by(User.login))
    users = list(result.scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="token_form.html",
        context=_ctx(request, admin_user=admin_user, users=users, created_token=None),
    )


@router.post("/tokens/create", response_class=HTMLResponse)
async def create_token_handler(
    request: Request,
    user_id: int = Form(...),
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Handle create-token form submission."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    # Extract scopes from the form (multiple checkboxes with same name)
    form_data = await request.form()
    scopes = form_data.getlist("scopes")

    pat, raw_token = await create_token(
        db,
        user_id=user_id,
        name=name,
        scopes=scopes,
    )

    # Re-fetch users for the form (in case they want to create another)
    result = await db.execute(select(User).order_by(User.login))
    users = list(result.scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="token_form.html",
        context=_ctx(
            request,
            admin_user=admin_user,
            users=users,
            created_token=raw_token,
            flash_message="Token created successfully. Copy it now!",
            flash_type="success",
        ),
    )


@router.post("/tokens/{token_id}/revoke", response_class=HTMLResponse)
async def revoke_token(
    request: Request,
    token_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Revoke (delete) a personal access token."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(
        select(PersonalAccessToken).where(PersonalAccessToken.id == token_id)
    )
    token = result.scalar_one_or_none()
    if token:
        await db.delete(token)
        await db.commit()

    return RedirectResponse(url="/admin/tokens", status_code=302)


# ---------------------------------------------------------------------------
# Routes: Repositories
# ---------------------------------------------------------------------------

@router.get("/repos", response_class=HTMLResponse)
async def list_repos(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """List all repositories."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(select(Repository).order_by(Repository.id))
    repos = list(result.scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="repos.html",
        context=_ctx(request, admin_user=admin_user, repos=repos),
    )


@router.get("/repos/{repo_id}", response_class=HTMLResponse)
async def repo_detail(
    request: Request,
    repo_id: int,
    db: AsyncSession = Depends(get_db),
):
    """View repository details."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(select(Repository).where(Repository.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        return RedirectResponse(url="/admin/repos", status_code=302)

    return templates.TemplateResponse(
        request=request,
        name="repo_detail.html",
        context=_ctx(request, admin_user=admin_user, repo=repo),
    )


@router.post("/repos/{repo_id}/delete", response_class=HTMLResponse)
async def delete_repo(
    request: Request,
    repo_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a repository."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(select(Repository).where(Repository.id == repo_id))
    repo = result.scalar_one_or_none()
    if repo:
        await db.delete(repo)
        await db.commit()

    return RedirectResponse(url="/admin/repos", status_code=302)


# ---------------------------------------------------------------------------
# Routes: Organizations
# ---------------------------------------------------------------------------

@router.get("/orgs", response_class=HTMLResponse)
async def list_orgs(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """List all organizations."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(select(Organization).order_by(Organization.id))
    orgs = list(result.scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="orgs.html",
        context=_ctx(request, admin_user=admin_user, orgs=orgs),
    )


@router.get("/orgs/create", response_class=HTMLResponse)
async def create_org_form(request: Request):
    """Render the create-organization form."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    return templates.TemplateResponse(
        request=request,
        name="org_form.html",
        context=_ctx(request, admin_user=admin_user, edit_org=None),
    )


@router.post("/orgs/create", response_class=HTMLResponse)
async def create_org_handler(
    request: Request,
    login: str = Form(...),
    name: str = Form(""),
    description: str = Form(""),
    email: str = Form(""),
    blog: str = Form(""),
    location: str = Form(""),
    company: str = Form(""),
    billing_email: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Handle create-organization form submission."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    # Check for duplicate login
    existing = await db.execute(
        select(Organization).where(Organization.login == login)
    )
    if existing.scalar_one_or_none():
        return templates.TemplateResponse(
            request=request,
            name="org_form.html",
            context=_ctx(
                request,
                admin_user=admin_user,
                edit_org=None,
                flash_message=f"Organization '{login}' already exists.",
                flash_type="error",
            ),
        )

    org = Organization(
        login=login,
        name=name or None,
        description=description or None,
        email=email or None,
        blog=blog or None,
        location=location or None,
        company=company or None,
        billing_email=billing_email or None,
    )
    db.add(org)
    await db.commit()

    return RedirectResponse(url="/admin/orgs", status_code=302)


@router.get("/orgs/{org_id}", response_class=HTMLResponse)
async def edit_org_page(
    request: Request,
    org_id: int,
    flash_message: str | None = None,
    flash_type: str = "info",
    db: AsyncSession = Depends(get_db),
):
    """Render the edit-organization form."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(
        select(Organization).where(Organization.id == org_id)
    )
    org = result.scalar_one_or_none()
    if not org:
        return RedirectResponse(url="/admin/orgs", status_code=302)

    variables = (
        await db.execute(
            select(CiVariable)
            .where(CiVariable.scope_type == "group", CiVariable.scope_id == org.id)
            .order_by(CiVariable.key, CiVariable.environment_scope)
        )
    ).scalars().all()
    secrets = (
        await db.execute(
            select(CiSecret)
            .where(CiSecret.scope_type == "group", CiSecret.scope_id == org.id)
            .order_by(CiSecret.name, CiSecret.environment_scope, CiSecret.branch_scope)
        )
    ).scalars().all()

    return templates.TemplateResponse(
        request=request,
        name="org_form.html",
        context=_ctx(
            request,
            admin_user=admin_user,
            edit_org=org,
            group_variables=variables,
            group_secrets=secrets,
            variable_flags=_ci_variable_flags,
            flash_message=flash_message,
            flash_type=flash_type,
        ),
    )


@router.post("/orgs/{org_id}", response_class=HTMLResponse)
async def update_org_handler(
    request: Request,
    org_id: int,
    login: str = Form(...),
    name: str = Form(""),
    description: str = Form(""),
    email: str = Form(""),
    blog: str = Form(""),
    location: str = Form(""),
    company: str = Form(""),
    billing_email: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Handle edit-organization form submission."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(
        select(Organization).where(Organization.id == org_id)
    )
    org = result.scalar_one_or_none()
    if not org:
        return RedirectResponse(url="/admin/orgs", status_code=302)

    org.name = name or None
    org.description = description or None
    org.email = email or None
    org.blog = blog or None
    org.location = location or None
    org.company = company or None
    org.billing_email = billing_email or None

    await db.commit()

    return RedirectResponse(url="/admin/orgs", status_code=302)


@router.post("/orgs/{org_id}/variables", response_class=HTMLResponse)
async def create_group_ci_variable(
    request: Request,
    org_id: int,
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
    """Create a group-scoped CI/CD variable from the admin group page."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)
    redirect = f"/admin/orgs/{org_id}"
    org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalar_one_or_none()
    if org is None:
        return RedirectResponse(url="/admin/orgs", status_code=302)
    try:
        if variable_type not in _CI_VARIABLE_TYPES:
            raise ValueError("Variable type must be env_var or file.")
        db.add(
            CiVariable(
                scope_type="group",
                scope_id=org.id,
                key=_validate_ci_key(key),
                value=value,
                variable_type=variable_type,
                visibility=_ci_visibility(_bool_form(masked), _bool_form(hidden)),
                protected=_bool_form(protected),
                raw=_bool_form(raw),
                environment_scope=environment_scope.strip() or "*",
                description=description.strip() or None,
            )
        )
        await db.commit()
    except (ValueError, IntegrityError) as exc:
        await db.rollback()
        message = (
            "Variable already exists for that environment scope."
            if isinstance(exc, IntegrityError)
            else str(exc)
        )
        return RedirectResponse(url=_admin_error_url(redirect, message), status_code=302)
    return RedirectResponse(url=_admin_success_url(redirect, "Group variable created."), status_code=302)


@router.post("/orgs/{org_id}/variables/{variable_id}/update", response_class=HTMLResponse)
async def update_group_ci_variable(
    request: Request,
    org_id: int,
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
    """Update a group-scoped CI/CD variable."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)
    redirect = f"/admin/orgs/{org_id}"
    variable = (
        await db.execute(
            select(CiVariable).where(
                CiVariable.id == variable_id,
                CiVariable.scope_type == "group",
                CiVariable.scope_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if variable is None:
        return RedirectResponse(url=_admin_error_url(redirect, "Variable not found."), status_code=302)
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
        message = (
            "Variable already exists for that environment scope."
            if isinstance(exc, IntegrityError)
            else str(exc)
        )
        return RedirectResponse(url=_admin_error_url(redirect, message), status_code=302)
    return RedirectResponse(url=_admin_success_url(redirect, "Group variable updated."), status_code=302)


@router.post("/orgs/{org_id}/variables/{variable_id}/delete", response_class=HTMLResponse)
async def delete_group_ci_variable(
    request: Request,
    org_id: int,
    variable_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a group-scoped CI/CD variable."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)
    variable = (
        await db.execute(
            select(CiVariable).where(
                CiVariable.id == variable_id,
                CiVariable.scope_type == "group",
                CiVariable.scope_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if variable is not None:
        await db.delete(variable)
        await db.commit()
    return RedirectResponse(
        url=_admin_success_url(f"/admin/orgs/{org_id}", "Group variable deleted."),
        status_code=302,
    )


@router.post("/orgs/{org_id}/secrets", response_class=HTMLResponse)
async def create_group_ci_secret(
    request: Request,
    org_id: int,
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
    """Create a group-scoped CI/CD secret from the admin group page."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)
    redirect = f"/admin/orgs/{org_id}"
    org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalar_one_or_none()
    if org is None:
        return RedirectResponse(url="/admin/orgs", status_code=302)
    try:
        reminder = int(rotation_reminder_days) if rotation_reminder_days.strip() else None
        db.add(
            CiSecret(
                scope_type="group",
                scope_id=org.id,
                name=_validate_ci_key(name, "Name"),
                value=value,
                description=description.strip() or None,
                environment_scope=environment_scope.strip() or "*",
                branch_scope=branch_scope.strip() or "*",
                protected=_bool_form(protected),
                rotation_reminder_days=reminder,
                status=status.strip() or "healthy",
            )
        )
        await db.commit()
    except (ValueError, IntegrityError) as exc:
        await db.rollback()
        message = "Secret already exists for those scopes." if isinstance(exc, IntegrityError) else str(exc)
        return RedirectResponse(url=_admin_error_url(redirect, message), status_code=302)
    return RedirectResponse(url=_admin_success_url(redirect, "Group secret created."), status_code=302)


@router.post("/orgs/{org_id}/secrets/{secret_id}/update", response_class=HTMLResponse)
async def update_group_ci_secret(
    request: Request,
    org_id: int,
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
    """Update a group-scoped CI/CD secret."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)
    redirect = f"/admin/orgs/{org_id}"
    secret = (
        await db.execute(
            select(CiSecret).where(
                CiSecret.id == secret_id,
                CiSecret.scope_type == "group",
                CiSecret.scope_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if secret is None:
        return RedirectResponse(url=_admin_error_url(redirect, "Secret not found."), status_code=302)
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
        return RedirectResponse(url=_admin_error_url(redirect, message), status_code=302)
    return RedirectResponse(url=_admin_success_url(redirect, "Group secret updated."), status_code=302)


@router.post("/orgs/{org_id}/secrets/{secret_id}/delete", response_class=HTMLResponse)
async def delete_group_ci_secret(
    request: Request,
    org_id: int,
    secret_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a group-scoped CI/CD secret."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)
    secret = (
        await db.execute(
            select(CiSecret).where(
                CiSecret.id == secret_id,
                CiSecret.scope_type == "group",
                CiSecret.scope_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if secret is not None:
        await db.delete(secret)
        await db.commit()
    return RedirectResponse(
        url=_admin_success_url(f"/admin/orgs/{org_id}", "Group secret deleted."),
        status_code=302,
    )


@router.post("/orgs/{org_id}/delete", response_class=HTMLResponse)
async def delete_org(
    request: Request,
    org_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete an organization."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(
        select(Organization).where(Organization.id == org_id)
    )
    org = result.scalar_one_or_none()
    if org:
        await db.delete(org)
        await db.commit()

    return RedirectResponse(url="/admin/orgs", status_code=302)


# ---------------------------------------------------------------------------
# Routes: Issues & Pull Requests (read-only browse)
# ---------------------------------------------------------------------------

@router.get("/issues", response_class=HTMLResponse)
async def list_issues(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """List all issues and pull requests (read-only admin view)."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(
        select(Issue).order_by(Issue.updated_at.desc())
    )
    issues = list(result.scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="issues.html",
        context=_ctx(request, admin_user=admin_user, issues=issues),
    )


# ---------------------------------------------------------------------------
# Routes: Import
# ---------------------------------------------------------------------------

@router.get("/import", response_class=HTMLResponse)
async def import_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Render the import form."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(select(User).order_by(User.login))
    users = list(result.scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="import_form.html",
        context=_ctx(request, admin_user=admin_user, users=users),
    )


@router.post("/import", response_class=HTMLResponse)
async def import_handler(
    request: Request,
    source_type: str = Form(...),
    source: str = Form(...),
    owner_id: int = Form(...),
    gitlab_token: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Handle import form submission."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    source = source.strip()
    token = gitlab_token.strip() or None

    if not source:
        result = await db.execute(select(User).order_by(User.login))
        users = list(result.scalars().all())
        return templates.TemplateResponse(
            request=request,
            name="import_form.html",
            context=_ctx(
                request,
                admin_user=admin_user,
                users=users,
                flash_message="Source is required.",
                flash_type="error",
            ),
        )

    # Validate owner_id
    result = await db.execute(select(User).where(User.id == owner_id))
    owner = result.scalar_one_or_none()
    if not owner:
        result = await db.execute(select(User).order_by(User.login))
        users = list(result.scalars().all())
        return templates.TemplateResponse(
            request=request,
            name="import_form.html",
            context=_ctx(
                request,
                admin_user=admin_user,
                users=users,
                flash_message="Invalid user selected.",
                flash_type="error",
            ),
        )

    try:
        if source_type == "single":
            await start_single_import(db, source, owner_id, token)
        else:
            await start_bulk_import(db, source, owner_id, token, source_type)
    except ValueError as exc:
        result = await db.execute(select(User).order_by(User.login))
        users = list(result.scalars().all())
        return templates.TemplateResponse(
            request=request,
            name="import_form.html",
            context=_ctx(
                request,
                admin_user=admin_user,
                users=users,
                flash_message=str(exc),
                flash_type="error",
            ),
        )

    return RedirectResponse(url="/admin/import/jobs", status_code=302)


@router.get("/import/jobs", response_class=HTMLResponse)
async def import_jobs(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """List all import jobs."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(
        select(ImportJob).order_by(ImportJob.created_at.desc())
    )
    jobs = list(result.scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="import_jobs.html",
        context=_ctx(request, admin_user=admin_user, jobs=jobs),
    )


@router.get("/import/jobs/{job_id}", response_class=HTMLResponse)
async def import_job_detail(
    request: Request,
    job_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Show import job detail."""
    admin_user = _get_admin_user(request)
    if not admin_user:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(
        select(ImportJob).where(ImportJob.id == job_id)
    )
    job = result.scalar_one_or_none()
    if not job:
        return RedirectResponse(url="/admin/import/jobs", status_code=302)

    # Load child jobs for bulk imports
    child_jobs = []
    if job.job_type == "bulk":
        result = await db.execute(
            select(ImportJob)
            .where(ImportJob.parent_job_id == job.id)
            .order_by(ImportJob.id)
        )
        child_jobs = list(result.scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="import_job_detail.html",
        context=_ctx(
            request,
            admin_user=admin_user,
            job=job,
            child_jobs=child_jobs,
        ),
    )
