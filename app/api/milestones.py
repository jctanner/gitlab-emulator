"""Milestone endpoints -- GitLab project and repo milestone CRUD."""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select, func as sa_func

from app.api.deps import AuthUser, CurrentUser, DbSession, get_repo_or_404
from app.api.pagination import paginated_json
from app.api.projects import _get_project_or_404
from app.config import settings
from app.models.milestone import Milestone
from app.models.issue import Issue
from app.schemas.milestone import MilestoneCreate, MilestoneResponse, MilestoneUpdate
from app.services.permissions import MAINTAINER, require_project_access

router = APIRouter(tags=["milestones"])

BASE = settings.BASE_URL


async def _count_issues(db, milestone_id: int, state: str) -> int:
    result = await db.execute(
        select(sa_func.count()).where(
            Issue.milestone_id == milestone_id, Issue.state == state
        )
    )
    return result.scalar() or 0


def _parse_due_on(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt_date(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.date().isoformat()


def _fmt_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _gitlab_milestone_json(
    milestone: Milestone,
    project,
    open_issues: int = 0,
    closed_issues: int = 0,
) -> dict:
    due_date = _fmt_date(milestone.due_on)
    web_url = f"{BASE}/{project.full_name}/-/milestones/{milestone.number}"
    return {
        "id": milestone.id,
        "iid": milestone.number,
        "project_id": project.id,
        "title": milestone.title,
        "description": milestone.description,
        "state": milestone.state,
        "created_at": _fmt_datetime(milestone.created_at),
        "updated_at": _fmt_datetime(milestone.updated_at),
        "due_date": due_date,
        "due_on": due_date,
        "start_date": None,
        "expired": False,
        "web_url": web_url,
        "url": f"{BASE}/api/v4/projects/{project.id}/milestones/{milestone.id}",
        "open_issues": open_issues,
        "closed_issues": closed_issues,
    }


async def _get_project_milestone_or_404(
    project,
    milestone_ref: int,
    db: DbSession,
) -> Milestone:
    result = await db.execute(
        select(Milestone).where(
            Milestone.repo_id == project.id,
            Milestone.id == milestone_ref,
        )
    )
    milestone = result.scalar_one_or_none()
    if milestone is None:
        result = await db.execute(
            select(Milestone).where(
                Milestone.repo_id == project.id,
                Milestone.number == milestone_ref,
            )
        )
        milestone = result.scalar_one_or_none()
    if milestone is None:
        raise HTTPException(status_code=404, detail="404 Milestone Not Found")
    return milestone


@router.get("/projects/{project_ref:path}/milestones")
async def list_project_milestones(
    project_ref: str,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    state: str = Query("active"),
    search: str | None = Query(None),
    title: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List GitLab-shaped project milestones."""
    project = await _get_project_or_404(project_ref, db, current_user)
    query = select(Milestone).where(Milestone.repo_id == project.id)
    count_query = (
        select(sa_func.count())
        .select_from(Milestone)
        .where(Milestone.repo_id == project.id)
    )
    if state not in ("all", ""):
        normalized_state = "open" if state == "active" else state
        query = query.where(Milestone.state == normalized_state)
        count_query = count_query.where(Milestone.state == normalized_state)
    if search:
        pattern = f"%{search}%"
        query = query.where(Milestone.title.ilike(pattern))
        count_query = count_query.where(Milestone.title.ilike(pattern))
    if title:
        query = query.where(Milestone.title == title)
        count_query = count_query.where(Milestone.title == title)

    total = int((await db.execute(count_query)).scalar() or 0)
    milestones = (
        await db.execute(
            query.order_by(Milestone.due_on.asc(), Milestone.number.asc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).scalars().all()

    items = []
    for milestone in milestones:
        open_count = await _count_issues(db, milestone.id, "open")
        closed_count = await _count_issues(db, milestone.id, "closed")
        items.append(
            _gitlab_milestone_json(milestone, project, open_count, closed_count)
        )
    return paginated_json(items, request, page, per_page, total)


@router.post("/projects/{project_ref:path}/milestones", status_code=201)
async def create_project_milestone(
    project_ref: str,
    body: MilestoneCreate,
    user: AuthUser,
    db: DbSession,
):
    """Create a GitLab-shaped project milestone."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, MAINTAINER)

    result = await db.execute(
        select(sa_func.coalesce(sa_func.max(Milestone.number), 0)).where(
            Milestone.repo_id == project.id
        )
    )
    next_number = (result.scalar() or 0) + 1
    milestone = Milestone(
        repo_id=project.id,
        number=next_number,
        title=body.title,
        description=body.description,
        state="open" if body.state == "active" else body.state,
        due_on=_parse_due_on(body.due_on),
    )
    db.add(milestone)
    await db.commit()
    await db.refresh(milestone)
    return _gitlab_milestone_json(milestone, project)


@router.get("/projects/{project_ref:path}/milestones/{milestone_id}")
async def get_project_milestone(
    project_ref: str,
    milestone_id: int,
    db: DbSession,
    current_user: CurrentUser,
):
    """Get a GitLab-shaped project milestone."""
    project = await _get_project_or_404(project_ref, db, current_user)
    milestone = await _get_project_milestone_or_404(project, milestone_id, db)
    open_count = await _count_issues(db, milestone.id, "open")
    closed_count = await _count_issues(db, milestone.id, "closed")
    return _gitlab_milestone_json(milestone, project, open_count, closed_count)


@router.put("/projects/{project_ref:path}/milestones/{milestone_id}")
async def update_project_milestone(
    project_ref: str,
    milestone_id: int,
    body: MilestoneUpdate,
    user: AuthUser,
    db: DbSession,
):
    """Update a GitLab-shaped project milestone."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, MAINTAINER)
    milestone = await _get_project_milestone_or_404(project, milestone_id, db)

    if body.title is not None:
        milestone.title = body.title
    if body.description is not None:
        milestone.description = body.description
    if body.state is not None:
        old_state = milestone.state
        milestone.state = "open" if body.state == "active" else body.state
        if milestone.state == "closed" and old_state != "closed":
            milestone.closed_at = datetime.now(timezone.utc)
        elif milestone.state == "open":
            milestone.closed_at = None
    if body.due_on is not None:
        milestone.due_on = _parse_due_on(body.due_on)

    await db.commit()
    await db.refresh(milestone)
    open_count = await _count_issues(db, milestone.id, "open")
    closed_count = await _count_issues(db, milestone.id, "closed")
    return _gitlab_milestone_json(milestone, project, open_count, closed_count)


@router.delete("/projects/{project_ref:path}/milestones/{milestone_id}", status_code=204)
async def delete_project_milestone(
    project_ref: str,
    milestone_id: int,
    user: AuthUser,
    db: DbSession,
):
    """Delete a GitLab-shaped project milestone."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, MAINTAINER)
    milestone = await _get_project_milestone_or_404(project, milestone_id, db)
    await db.delete(milestone)
    await db.commit()


@router.get("/repos/{owner}/{repo}/milestones")
async def list_milestones(
    owner: str,
    repo: str,
    db: DbSession,
    current_user: CurrentUser,
    state: str = Query("open"),
    sort: str = Query("due_on"),
    direction: str = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List milestones for a repository."""
    repository = await get_repo_or_404(owner, repo, db)
    query = select(Milestone).where(Milestone.repo_id == repository.id)

    if state != "all":
        query = query.where(Milestone.state == state)

    sort_col = getattr(Milestone, sort, Milestone.due_on)
    if direction == "desc":
        query = query.order_by(sort_col.desc())
    else:
        query = query.order_by(sort_col.asc())

    query = query.offset((page - 1) * per_page).limit(per_page)
    milestones = (await db.execute(query)).scalars().all()

    results = []
    for ms in milestones:
        open_count = await _count_issues(db, ms.id, "open")
        closed_count = await _count_issues(db, ms.id, "closed")
        results.append(
            MilestoneResponse.from_db(
                ms, BASE, owner, repo,
                open_issues=open_count,
                closed_issues=closed_count,
            )
        )
    return results


@router.post("/repos/{owner}/{repo}/milestones", status_code=201)
async def create_milestone(
    owner: str, repo: str, body: MilestoneCreate, user: AuthUser, db: DbSession
):
    """Create a milestone."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, MAINTAINER)

    # Get next milestone number
    result = await db.execute(
        select(sa_func.coalesce(sa_func.max(Milestone.number), 0)).where(
            Milestone.repo_id == repository.id
        )
    )
    next_number = (result.scalar() or 0) + 1

    milestone = Milestone(
        repo_id=repository.id,
        number=next_number,
        title=body.title,
        description=body.description,
        state=body.state,
        due_on=_parse_due_on(body.due_on),
    )
    db.add(milestone)
    await db.commit()
    await db.refresh(milestone)
    return MilestoneResponse.from_db(milestone, BASE, owner, repo)


@router.get("/repos/{owner}/{repo}/milestones/{milestone_number}")
async def get_milestone(
    owner: str, repo: str, milestone_number: int, db: DbSession, current_user: CurrentUser
):
    """Get a single milestone."""
    repository = await get_repo_or_404(owner, repo, db)
    result = await db.execute(
        select(Milestone).where(
            Milestone.repo_id == repository.id,
            Milestone.number == milestone_number,
        )
    )
    milestone = result.scalar_one_or_none()
    if milestone is None:
        raise HTTPException(status_code=404, detail="Not Found")

    open_count = await _count_issues(db, milestone.id, "open")
    closed_count = await _count_issues(db, milestone.id, "closed")
    return MilestoneResponse.from_db(
        milestone, BASE, owner, repo,
        open_issues=open_count, closed_issues=closed_count,
    )


@router.patch("/repos/{owner}/{repo}/milestones/{milestone_number}")
async def update_milestone(
    owner: str,
    repo: str,
    milestone_number: int,
    body: MilestoneUpdate,
    user: AuthUser,
    db: DbSession,
):
    """Update a milestone."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, MAINTAINER)
    result = await db.execute(
        select(Milestone).where(
            Milestone.repo_id == repository.id,
            Milestone.number == milestone_number,
        )
    )
    milestone = result.scalar_one_or_none()
    if milestone is None:
        raise HTTPException(status_code=404, detail="Not Found")

    if body.title is not None:
        milestone.title = body.title
    if body.description is not None:
        milestone.description = body.description
    if body.state is not None:
        old_state = milestone.state
        milestone.state = body.state
        if body.state == "closed" and old_state != "closed":
            milestone.closed_at = datetime.now(timezone.utc)
        elif body.state == "open":
            milestone.closed_at = None
    if body.due_on is not None:
        milestone.due_on = _parse_due_on(body.due_on)

    await db.commit()
    await db.refresh(milestone)

    open_count = await _count_issues(db, milestone.id, "open")
    closed_count = await _count_issues(db, milestone.id, "closed")
    return MilestoneResponse.from_db(
        milestone, BASE, owner, repo,
        open_issues=open_count, closed_issues=closed_count,
    )


@router.delete("/repos/{owner}/{repo}/milestones/{milestone_number}", status_code=204)
async def delete_milestone(
    owner: str, repo: str, milestone_number: int, user: AuthUser, db: DbSession
):
    """Delete a milestone."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, MAINTAINER)
    result = await db.execute(
        select(Milestone).where(
            Milestone.repo_id == repository.id,
            Milestone.number == milestone_number,
        )
    )
    milestone = result.scalar_one_or_none()
    if milestone is None:
        raise HTTPException(status_code=404, detail="Not Found")

    await db.delete(milestone)
    await db.commit()
