"""Label endpoints -- GitLab project labels, repo labels, and issue labels."""

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func as sa_func
from sqlalchemy import select, delete as sa_delete

from app.api.deps import AuthUser, CurrentUser, DbSession, get_repo_or_404
from app.api.pagination import paginated_json
from app.api.projects import _get_project_or_404
from app.config import settings
from app.models.label import Label
from app.models.issue import Issue, IssueLabel
from app.schemas.label import LabelCreate, LabelResponse, LabelUpdate
from app.services.permissions import DEVELOPER, MAINTAINER, require_project_access

router = APIRouter(tags=["labels"])

BASE = settings.BASE_URL


# ---------------------------------------------------------------------------
# GitLab project-level label CRUD
# ---------------------------------------------------------------------------

def _label_color(color: str | None) -> str:
    raw = (color or "ededed").strip()
    return raw[1:] if raw.startswith("#") else raw


def _gitlab_label_json(
    label: Label,
    project,
    open_issues_count: int = 0,
    closed_issues_count: int = 0,
) -> dict:
    color = label.color if str(label.color).startswith("#") else f"#{label.color}"
    return {
        "id": label.id,
        "name": label.name,
        "color": color,
        "text_color": "#FFFFFF",
        "description": label.description,
        "description_html": label.description or "",
        "open_issues_count": open_issues_count,
        "closed_issues_count": closed_issues_count,
        "open_merge_requests_count": 0,
        "subscribed": False,
        "priority": None,
        "is_project_label": True,
        "url": f"{BASE}/api/v4/projects/{project.id}/labels/{label.name}",
        "web_url": f"{BASE}/{project.full_name}/-/labels/{label.name}",
    }


async def _label_issue_counts(db: DbSession, label_id: int) -> tuple[int, int]:
    query = (
        select(Issue.state, sa_func.count(Issue.id))
        .join(IssueLabel, IssueLabel.issue_id == Issue.id)
        .where(IssueLabel.label_id == label_id)
        .group_by(Issue.state)
    )
    rows = (await db.execute(query)).all()
    counts = {state: count for state, count in rows}
    return int(counts.get("open", 0)), int(counts.get("closed", 0))


async def _get_project_label_or_404(project, name: str, db: DbSession) -> Label:
    result = await db.execute(
        select(Label).where(Label.repo_id == project.id, Label.name == name)
    )
    label = result.scalar_one_or_none()
    if label is None:
        raise HTTPException(status_code=404, detail="404 Label Not Found")
    return label


async def _get_project_label_by_identifier_or_404(
    project, identifier: str, db: DbSession
) -> Label:
    """Resolve GitLab label routes that accept a label name or numeric label id."""
    if identifier.isdigit():
        result = await db.execute(
            select(Label).where(
                Label.repo_id == project.id,
                Label.id == int(identifier),
            )
        )
        label = result.scalar_one_or_none()
        if label is not None:
            return label
    return await _get_project_label_or_404(project, identifier, db)


@router.get("/projects/{project_ref:path}/labels")
async def list_project_labels(
    project_ref: str,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    search: str | None = Query(None),
    with_counts: bool = Query(False),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List GitLab-shaped project labels."""
    project = await _get_project_or_404(project_ref, db, current_user)
    query = select(Label).where(Label.repo_id == project.id)
    count_query = (
        select(sa_func.count())
        .select_from(Label)
        .where(Label.repo_id == project.id)
    )
    if search:
        pattern = f"%{search}%"
        query = query.where(Label.name.ilike(pattern))
        count_query = count_query.where(Label.name.ilike(pattern))

    total = int((await db.execute(count_query)).scalar() or 0)
    labels = (
        await db.execute(
            query.order_by(Label.name)
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).scalars().all()

    items = []
    for label in labels:
        open_count = closed_count = 0
        if with_counts:
            open_count, closed_count = await _label_issue_counts(db, label.id)
        items.append(_gitlab_label_json(label, project, open_count, closed_count))
    return paginated_json(items, request, page, per_page, total)


@router.post("/projects/{project_ref:path}/labels", status_code=201)
async def create_project_label(
    project_ref: str,
    body: LabelCreate,
    user: AuthUser,
    db: DbSession,
):
    """Create a GitLab-shaped project label."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, MAINTAINER)

    existing = await db.execute(
        select(Label).where(Label.repo_id == project.id, Label.name == body.name)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Label already exists")

    label = Label(
        repo_id=project.id,
        name=body.name,
        color=_label_color(body.color),
        description=body.description,
    )
    db.add(label)
    await db.commit()
    await db.refresh(label)
    return _gitlab_label_json(label, project)


@router.get("/projects/{project_ref:path}/labels/{name}")
async def get_project_label(
    project_ref: str,
    name: str,
    db: DbSession,
    current_user: CurrentUser,
    with_counts: bool = Query(False),
):
    """Get a GitLab-shaped project label by name or numeric label id."""
    project = await _get_project_or_404(project_ref, db, current_user)
    label = await _get_project_label_by_identifier_or_404(project, name, db)
    open_count = closed_count = 0
    if with_counts:
        open_count, closed_count = await _label_issue_counts(db, label.id)
    return _gitlab_label_json(label, project, open_count, closed_count)


@router.put("/projects/{project_ref:path}/labels/{name}")
async def update_project_label(
    project_ref: str,
    name: str,
    body: LabelUpdate,
    user: AuthUser,
    db: DbSession,
):
    """Update a GitLab-shaped project label by name or numeric label id."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, MAINTAINER)
    label = await _get_project_label_by_identifier_or_404(project, name, db)

    new_name = body.new_name if body.new_name is not None else body.name
    if new_name is not None:
        label.name = new_name
    if body.color is not None:
        label.color = _label_color(body.color)
    if body.description is not None:
        label.description = body.description

    await db.commit()
    await db.refresh(label)
    return _gitlab_label_json(label, project)


@router.delete("/projects/{project_ref:path}/labels/{name}", status_code=204)
async def delete_project_label(
    project_ref: str,
    name: str,
    user: AuthUser,
    db: DbSession,
):
    """Delete a GitLab-shaped project label by name or numeric label id."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, MAINTAINER)
    label = await _get_project_label_by_identifier_or_404(project, name, db)
    await db.delete(label)
    await db.commit()


# ---------------------------------------------------------------------------
# Repo-level label CRUD
# ---------------------------------------------------------------------------

@router.get("/repos/{owner}/{repo}/labels")
async def list_labels(
    owner: str,
    repo: str,
    db: DbSession,
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List all labels for a repository."""
    repository = await get_repo_or_404(owner, repo, db)
    query = (
        select(Label)
        .where(Label.repo_id == repository.id)
        .order_by(Label.name)
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    labels = (await db.execute(query)).scalars().all()
    return [LabelResponse.from_db(l, BASE, owner, repo) for l in labels]


@router.post("/repos/{owner}/{repo}/labels", status_code=201)
async def create_label(
    owner: str, repo: str, body: LabelCreate, user: AuthUser, db: DbSession
):
    """Create a label."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, MAINTAINER)

    existing = await db.execute(
        select(Label).where(
            Label.repo_id == repository.id, Label.name == body.name
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=422, detail="Label already exists")

    label = Label(
        repo_id=repository.id,
        name=body.name,
        color=body.color.lstrip("#"),
        description=body.description,
    )
    db.add(label)
    await db.commit()
    await db.refresh(label)
    return LabelResponse.from_db(label, BASE, owner, repo)


@router.get("/repos/{owner}/{repo}/labels/{name}")
async def get_label(
    owner: str, repo: str, name: str, db: DbSession, current_user: CurrentUser
):
    """Get a single label."""
    repository = await get_repo_or_404(owner, repo, db)

    result = await db.execute(
        select(Label).where(
            Label.repo_id == repository.id, Label.name == name
        )
    )
    label = result.scalar_one_or_none()
    if label is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return LabelResponse.from_db(label, BASE, owner, repo)


@router.patch("/repos/{owner}/{repo}/labels/{name}")
async def update_label(
    owner: str, repo: str, name: str, body: LabelUpdate, user: AuthUser, db: DbSession
):
    """Update a label."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, MAINTAINER)

    result = await db.execute(
        select(Label).where(
            Label.repo_id == repository.id, Label.name == name
        )
    )
    label = result.scalar_one_or_none()
    if label is None:
        raise HTTPException(status_code=404, detail="Not Found")

    new_name = body.new_name if body.new_name is not None else body.name
    if new_name is not None:
        label.name = new_name
    if body.color is not None:
        label.color = body.color.lstrip("#")
    if body.description is not None:
        label.description = body.description

    await db.commit()
    await db.refresh(label)
    return LabelResponse.from_db(label, BASE, owner, repo)


@router.delete("/repos/{owner}/{repo}/labels/{name}", status_code=204)
async def delete_label(
    owner: str, repo: str, name: str, user: AuthUser, db: DbSession
):
    """Delete a label."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, MAINTAINER)

    result = await db.execute(
        select(Label).where(
            Label.repo_id == repository.id, Label.name == name
        )
    )
    label = result.scalar_one_or_none()
    if label is None:
        raise HTTPException(status_code=404, detail="Not Found")

    await db.delete(label)
    await db.commit()


# ---------------------------------------------------------------------------
# Issue-level label management
# ---------------------------------------------------------------------------

@router.get("/repos/{owner}/{repo}/issues/{issue_number}/labels")
async def list_issue_labels(
    owner: str, repo: str, issue_number: int, db: DbSession, current_user: CurrentUser
):
    """List labels on an issue."""
    repository = await get_repo_or_404(owner, repo, db)
    result = await db.execute(
        select(Issue).where(
            Issue.repo_id == repository.id, Issue.number == issue_number
        )
    )
    issue = result.scalar_one_or_none()
    if issue is None:
        raise HTTPException(status_code=404, detail="Not Found")

    return [LabelResponse.from_db(l, BASE, owner, repo) for l in issue.labels]


@router.post("/repos/{owner}/{repo}/issues/{issue_number}/labels", status_code=200)
async def add_issue_labels(
    owner: str,
    repo: str,
    issue_number: int,
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Add labels to an issue."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, DEVELOPER)
    result = await db.execute(
        select(Issue).where(
            Issue.repo_id == repository.id, Issue.number == issue_number
        )
    )
    issue = result.scalar_one_or_none()
    if issue is None:
        raise HTTPException(status_code=404, detail="Not Found")

    label_names = body.get("labels", [])
    for lname in label_names:
        lbl_result = await db.execute(
            select(Label).where(
                Label.repo_id == repository.id, Label.name == lname
            )
        )
        label = lbl_result.scalar_one_or_none()
        if label:
            # Check if already assigned
            existing = await db.execute(
                select(IssueLabel).where(
                    IssueLabel.issue_id == issue.id,
                    IssueLabel.label_id == label.id,
                )
            )
            if existing.scalar_one_or_none() is None:
                db.add(IssueLabel(issue_id=issue.id, label_id=label.id))

    await db.commit()
    await db.refresh(issue)
    return [LabelResponse.from_db(l, BASE, owner, repo) for l in issue.labels]


@router.put("/repos/{owner}/{repo}/issues/{issue_number}/labels")
async def set_issue_labels(
    owner: str,
    repo: str,
    issue_number: int,
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Replace all labels on an issue."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, DEVELOPER)
    result = await db.execute(
        select(Issue).where(
            Issue.repo_id == repository.id, Issue.number == issue_number
        )
    )
    issue = result.scalar_one_or_none()
    if issue is None:
        raise HTTPException(status_code=404, detail="Not Found")

    # Remove existing
    await db.execute(
        sa_delete(IssueLabel).where(IssueLabel.issue_id == issue.id)
    )

    label_names = body.get("labels", [])
    for lname in label_names:
        lbl_result = await db.execute(
            select(Label).where(
                Label.repo_id == repository.id, Label.name == lname
            )
        )
        label = lbl_result.scalar_one_or_none()
        if label:
            db.add(IssueLabel(issue_id=issue.id, label_id=label.id))

    await db.commit()
    await db.refresh(issue)
    return [LabelResponse.from_db(l, BASE, owner, repo) for l in issue.labels]


@router.delete("/repos/{owner}/{repo}/issues/{issue_number}/labels/{name}", status_code=200)
async def remove_issue_label(
    owner: str,
    repo: str,
    issue_number: int,
    name: str,
    user: AuthUser,
    db: DbSession,
):
    """Remove a label from an issue."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, DEVELOPER)
    result = await db.execute(
        select(Issue).where(
            Issue.repo_id == repository.id, Issue.number == issue_number
        )
    )
    issue = result.scalar_one_or_none()
    if issue is None:
        raise HTTPException(status_code=404, detail="Not Found")

    lbl_result = await db.execute(
        select(Label).where(
            Label.repo_id == repository.id, Label.name == name
        )
    )
    label = lbl_result.scalar_one_or_none()
    if label is None:
        raise HTTPException(status_code=404, detail="Label not found")

    await db.execute(
        sa_delete(IssueLabel).where(
            IssueLabel.issue_id == issue.id, IssueLabel.label_id == label.id
        )
    )
    await db.commit()
    await db.refresh(issue)
    return [LabelResponse.from_db(l, BASE, owner, repo) for l in issue.labels]
