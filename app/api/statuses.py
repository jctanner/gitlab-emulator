"""Commit status endpoints -- create, list, and combined status."""

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.api.deps import AuthUser, CurrentUser, DbSession, get_repo_or_404
from app.api.projects import _get_project_or_404
from app.config import settings
from app.models.commit_status import CommitStatus
from app.models.project import Project
from app.schemas.user import SimpleUser, _fmt_dt, _make_node_id
from app.services.permissions import DEVELOPER, require_project_access

router = APIRouter(tags=["statuses"])

BASE = settings.BASE_URL
GITLAB_STATUS_STATES = {"canceled", "failed", "pending", "running", "skipped", "success"}


def _status_json(status: CommitStatus, owner: str, repo_name: str, base_url: str) -> dict:
    api = f"{base_url}/api/v4"
    creator = SimpleUser.from_db(status.creator, base_url).model_dump() if status.creator else None
    return {
        "url": f"{api}/repos/{owner}/{repo_name}/statuses/{status.id}",
        "avatar_url": creator.get("avatar_url", "") if creator else "",
        "id": status.id,
        "node_id": _make_node_id("CommitStatus", status.id),
        "state": status.state,
        "description": status.description,
        "target_url": status.target_url,
        "context": status.context,
        "created_at": _fmt_dt(status.created_at),
        "updated_at": _fmt_dt(status.updated_at),
        "creator": creator,
    }


def _gitlab_status_json(status: CommitStatus, project: Project, base_url: str) -> dict:
    creator = (
        SimpleUser.from_db(status.creator, base_url).model_dump()
        if status.creator
        else None
    )
    return {
        "id": status.id,
        "sha": status.sha,
        "ref": None,
        "status": status.state,
        "name": status.context,
        "context": status.context,
        "target_url": status.target_url,
        "description": status.description,
        "coverage": None,
        "allow_failure": False,
        "created_at": _fmt_dt(status.created_at),
        "started_at": None,
        "finished_at": _fmt_dt(status.updated_at)
        if status.state in {"canceled", "failed", "skipped", "success"}
        else None,
        "author": creator,
        "project_id": project.id,
    }


@router.post("/repos/{owner}/{repo}/statuses/{sha}", status_code=201)
async def create_status(
    owner: str, repo: str, sha: str, body: dict, user: AuthUser, db: DbSession
):
    """Create a commit status."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, DEVELOPER)

    state = body.get("state")
    if state not in ("error", "failure", "pending", "success"):
        raise HTTPException(status_code=422, detail="Invalid state")

    status = CommitStatus(
        repo_id=repository.id,
        sha=sha,
        state=state,
        target_url=body.get("target_url"),
        description=body.get("description"),
        context=body.get("context", "default"),
        creator_id=user.id,
    )
    db.add(status)
    await db.commit()
    await db.refresh(status)
    return _status_json(status, owner, repo, BASE)


@router.post("/projects/{project_ref:path}/statuses/{sha}", status_code=201)
async def create_project_commit_status(
    project_ref: str,
    sha: str,
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Create a GitLab-shaped commit status for a project commit."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, DEVELOPER)

    state = body.get("state") or body.get("status")
    if state not in GITLAB_STATUS_STATES:
        raise HTTPException(status_code=400, detail="Invalid status state")

    status = CommitStatus(
        repo_id=project.id,
        sha=sha,
        state=state,
        target_url=body.get("target_url"),
        description=body.get("description"),
        context=body.get("name") or body.get("context") or "default",
        creator_id=user.id,
    )
    db.add(status)
    await db.commit()
    await db.refresh(status)
    return _gitlab_status_json(status, project, BASE)


@router.get("/repos/{owner}/{repo}/commits/{sha}/statuses")
async def list_statuses(
    owner: str, repo: str, sha: str, db: DbSession, current_user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List statuses for a commit ref."""
    repository = await get_repo_or_404(owner, repo, db)

    query = (
        select(CommitStatus)
        .where(CommitStatus.repo_id == repository.id, CommitStatus.sha == sha)
        .order_by(CommitStatus.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    statuses = (await db.execute(query)).scalars().all()
    return [_status_json(s, owner, repo, BASE) for s in statuses]


@router.get("/projects/{project_ref:path}/repository/commits/{sha}/statuses")
async def list_project_commit_statuses(
    project_ref: str,
    sha: str,
    db: DbSession,
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List GitLab-shaped commit statuses for a project commit."""
    project = await _get_project_or_404(project_ref, db, current_user)
    query = (
        select(CommitStatus)
        .where(CommitStatus.repo_id == project.id, CommitStatus.sha == sha)
        .order_by(CommitStatus.created_at.desc(), CommitStatus.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    statuses = (await db.execute(query)).scalars().all()
    return [_gitlab_status_json(status, project, BASE) for status in statuses]


@router.get("/repos/{owner}/{repo}/commits/{sha}/status")
async def get_combined_status(
    owner: str, repo: str, sha: str, db: DbSession, current_user: CurrentUser
):
    """Get the combined status for a commit ref."""
    repository = await get_repo_or_404(owner, repo, db)

    query = (
        select(CommitStatus)
        .where(CommitStatus.repo_id == repository.id, CommitStatus.sha == sha)
        .order_by(CommitStatus.created_at.desc())
    )
    statuses = (await db.execute(query)).scalars().all()

    # Deduplicate by context (latest wins)
    by_context: dict[str, CommitStatus] = {}
    for s in statuses:
        if s.context not in by_context:
            by_context[s.context] = s

    # Compute combined state
    unique_statuses = list(by_context.values())
    if not unique_statuses:
        combined_state = "pending"
    elif any(s.state in ("error", "failure", "failed") for s in unique_statuses):
        combined_state = "failure"
    elif all(s.state == "success" for s in unique_statuses):
        combined_state = "success"
    else:
        combined_state = "pending"

    api = f"{BASE}/api/v4"
    return {
        "state": combined_state,
        "statuses": [_status_json(s, owner, repo, BASE) for s in unique_statuses],
        "sha": sha,
        "total_count": len(unique_statuses),
        "repository": {
            "id": repository.id,
            "full_name": repository.full_name,
            "url": f"{api}/repos/{repository.full_name}",
        },
        "commit_url": f"{api}/repos/{owner}/{repo}/commits/{sha}",
        "url": f"{api}/repos/{owner}/{repo}/commits/{sha}/status",
    }
