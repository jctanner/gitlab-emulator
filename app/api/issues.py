"""Issue endpoints -- list, create, get, update issues."""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, func as sa_func
from urllib.parse import unquote

from app.api.deps import AuthUser, CurrentUser, DbSession, get_repo_or_404
from app.api.pagination import paginated_json
from app.config import settings
from app.models.issue import Issue, IssueLabel, IssueAssignee
from app.models.label import Label
from app.models.milestone import Milestone
from app.models.project import Project
from app.models.user import User
from app.schemas.user import SimpleUser, _fmt_dt, _make_node_id
from app.schemas.label import LabelResponse
from app.services.permissions import (
    REPORTER,
    project_access_level,
    require_project_access,
)

router = APIRouter(tags=["issues"])

BASE = settings.BASE_URL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _issue_json(issue: Issue, base_url: str) -> dict:
    """Build a GitHub-compatible issue JSON object."""
    api = f"{base_url}/api/v4"
    repo = issue.repository
    owner_login = repo.owner.login if repo and repo.owner else "unknown"
    repo_name = repo.name if repo else "unknown"
    repo_full = f"{owner_login}/{repo_name}"
    issue_url = f"{api}/repos/{repo_full}/issues/{issue.number}"

    user_simple = (
        SimpleUser.from_db(issue.user, base_url).model_dump() if issue.user else None
    )

    labels = []
    if issue.labels:
        labels = [
            LabelResponse.from_db(lbl, base_url, owner_login, repo_name).model_dump()
            for lbl in issue.labels
        ]

    assignees = []
    if issue.assignees:
        assignees = [
            SimpleUser.from_db(u, base_url).model_dump() for u in issue.assignees
        ]

    milestone = None
    if issue.milestone:
        from app.schemas.milestone import MilestoneResponse

        milestone = MilestoneResponse.from_db(
            issue.milestone, base_url, owner_login, repo_name
        ).model_dump()

    pull_request_info = None
    if issue.pull_request is not None:
        pr = issue.pull_request
        pr_url = f"{api}/repos/{repo_full}/pulls/{issue.number}"
        pull_request_info = {
            "url": pr_url,
            "html_url": f"{base_url}/{repo_full}/pull/{issue.number}",
            "diff_url": f"{base_url}/{repo_full}/pull/{issue.number}.diff",
            "patch_url": f"{base_url}/{repo_full}/pull/{issue.number}.patch",
            "merged_at": _fmt_dt(pr.merged_at) if pr.merged_at else None,
        }

    closed_by = None
    if issue.closed_by:
        closed_by = SimpleUser.from_db(issue.closed_by, base_url).model_dump()

    return {
        "url": issue_url,
        "repository_url": f"{api}/repos/{repo_full}",
        "labels_url": f"{issue_url}/labels{{/name}}",
        "comments_url": f"{issue_url}/comments",
        "events_url": f"{issue_url}/events",
        "html_url": f"{base_url}/{repo_full}/issues/{issue.number}",
        "id": issue.id,
        "node_id": _make_node_id("Issue", issue.id),
        "number": issue.number,
        "title": issue.title,
        "user": user_simple,
        "labels": labels,
        "state": issue.state,
        "state_reason": issue.state_reason,
        "locked": issue.locked,
        "assignee": assignees[0] if assignees else None,
        "assignees": assignees,
        "milestone": milestone,
        "comments": 0,
        "created_at": _fmt_dt(issue.created_at),
        "updated_at": _fmt_dt(issue.updated_at),
        "closed_at": _fmt_dt(issue.closed_at),
        "closed_by": closed_by,
        "author_association": "OWNER",
        "active_lock_reason": issue.lock_reason,
        "body": issue.body,
        "reactions": {
            "url": f"{issue_url}/reactions",
            "total_count": 0,
            "+1": 0,
            "-1": 0,
            "laugh": 0,
            "hooray": 0,
            "confused": 0,
            "heart": 0,
            "rocket": 0,
            "eyes": 0,
        },
        "timeline_url": f"{issue_url}/timeline",
        "performed_via_gitlab_app": None,
        "pull_request": pull_request_info,
    }


async def _get_project_or_404(
    project_ref: str,
    db: DbSession,
    current_user: User | None,
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
        raise HTTPException(status_code=404, detail="Project Not Found")
    if project.private and (
        current_user is None
        or await project_access_level(project, current_user, db) < REPORTER
    ):
        raise HTTPException(status_code=404, detail="Project Not Found")
    return project


def _gitlab_issue_json(issue: Issue, base_url: str) -> dict:
    project = issue.repository
    project_path = project.full_name if project else "unknown/unknown"
    author = (
        SimpleUser.from_db(issue.user, base_url).model_dump() if issue.user else None
    )
    assignees = [
        SimpleUser.from_db(user, base_url).model_dump()
        for user in (issue.assignees or [])
    ]
    labels = [label.name for label in (issue.labels or [])]
    milestone = None
    if issue.milestone:
        from app.schemas.milestone import MilestoneResponse

        owner_login, _, repo_name = project_path.partition("/")
        milestone = MilestoneResponse.from_db(
            issue.milestone,
            base_url,
            owner_login,
            repo_name,
        ).model_dump()
    return {
        "id": issue.id,
        "iid": issue.number,
        "project_id": project.id if project else None,
        "title": issue.title,
        "description": issue.body,
        "state": "closed" if issue.state == "closed" else "opened",
        "created_at": _fmt_dt(issue.created_at),
        "updated_at": _fmt_dt(issue.updated_at),
        "closed_at": _fmt_dt(issue.closed_at),
        "closed_by": SimpleUser.from_db(issue.closed_by, base_url).model_dump()
        if issue.closed_by
        else None,
        "labels": labels,
        "milestone": milestone,
        "assignees": assignees,
        "assignee": assignees[0] if assignees else None,
        "author": author,
        "type": "ISSUE",
        "user_notes_count": 0,
        "merge_requests_count": 0,
        "upvotes": 0,
        "downvotes": 0,
        "due_date": None,
        "confidential": False,
        "discussion_locked": issue.locked,
        "issue_type": "issue",
        "web_url": f"{base_url}/{project_path}/-/issues/{issue.number}",
        "references": {
            "short": f"#{issue.number}",
            "relative": f"#{issue.number}",
            "full": f"{project_path}#{issue.number}",
        },
        "time_stats": {
            "time_estimate": 0,
            "total_time_spent": 0,
            "human_time_estimate": None,
            "human_total_time_spent": None,
        },
        "task_completion_status": {"count": 0, "completed_count": 0},
        "has_tasks": False,
        "severity": "UNKNOWN",
    }


def _label_names(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


async def _apply_issue_labels(
    issue: Issue, project: Project, labels: list[str], db: DbSession
) -> None:
    for label_name in labels:
        result = await db.execute(
            select(Label).where(Label.repo_id == project.id, Label.name == label_name)
        )
        label = result.scalar_one_or_none()
        if label is None:
            label = Label(repo_id=project.id, name=label_name, color="428BCA")
            db.add(label)
            await db.flush()
        db.add(IssueLabel(issue_id=issue.id, label_id=label.id))


async def _apply_issue_assignees(
    issue: Issue, assignee_ids: list[int], db: DbSession
) -> None:
    for user_id in assignee_ids:
        result = await db.execute(select(User).where(User.id == user_id))
        assignee = result.scalar_one_or_none()
        if assignee:
            db.add(IssueAssignee(issue_id=issue.id, user_id=assignee.id))


async def _apply_project_issue_milestone(
    issue: Issue,
    project: Project,
    body: dict,
    db: DbSession,
) -> None:
    if "milestone_id" in body:
        if body["milestone_id"] in (None, ""):
            issue.milestone_id = None
            return
        if not str(body["milestone_id"]).isdigit():
            return
        result = await db.execute(
            select(Milestone).where(
                Milestone.repo_id == project.id,
                Milestone.id == int(body["milestone_id"]),
            )
        )
        milestone = result.scalar_one_or_none()
        if milestone:
            issue.milestone_id = milestone.id
        return

    if "milestone" in body:
        if body["milestone"] in (None, ""):
            issue.milestone_id = None
            return
        if not str(body["milestone"]).isdigit():
            return
        result = await db.execute(
            select(Milestone).where(
                Milestone.repo_id == project.id,
                Milestone.number == int(body["milestone"]),
            )
        )
        milestone = result.scalar_one_or_none()
        if milestone:
            issue.milestone_id = milestone.id


async def _project_issue_or_404(
    project: Project, issue_iid: int, db: DbSession
) -> Issue:
    result = await db.execute(
        select(Issue).where(Issue.repo_id == project.id, Issue.number == issue_iid)
    )
    issue = result.scalar_one_or_none()
    if issue is None:
        raise HTTPException(status_code=404, detail="Issue Not Found")
    return issue


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/projects/{project_ref:path}/issues")
async def list_project_issues(
    project_ref: str,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    state: str = Query("opened"),
    labels: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List GitLab-shaped project issues."""
    project = await _get_project_or_404(project_ref, db, current_user)
    query = select(Issue).where(Issue.repo_id == project.id)
    if state not in {"all", "opened"}:
        query = query.where(Issue.state == ("closed" if state == "closed" else state))
    elif state == "opened":
        query = query.where(Issue.state == "open")
    if labels:
        for label_name in _label_names(labels):
            label_subq = (
                select(IssueLabel.issue_id)
                .join(Label, Label.id == IssueLabel.label_id)
                .where(Label.name == label_name, Label.repo_id == project.id)
            )
            query = query.where(Issue.id.in_(label_subq))
    query = query.order_by(Issue.created_at.desc())
    total = (
        await db.execute(select(sa_func.count()).select_from(query.subquery()))
    ).scalar() or 0
    issues = (
        (await db.execute(query.offset((page - 1) * per_page).limit(per_page)))
        .scalars()
        .all()
    )
    return paginated_json(
        [_gitlab_issue_json(issue, BASE) for issue in issues],
        request,
        page,
        per_page,
        total,
    )


@router.post("/projects/{project_ref:path}/issues", status_code=201)
async def create_project_issue(
    project_ref: str,
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Create a GitLab-shaped project issue."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, REPORTER)
    title = body.get("title")
    if not title:
        raise HTTPException(status_code=422, detail="title is required")
    number = project.next_issue_number
    project.next_issue_number = number + 1
    project.open_issues_count += 1
    issue = Issue(
        repo_id=project.id,
        number=number,
        user_id=user.id,
        title=title,
        body=body.get("description", body.get("body")),
    )
    db.add(issue)
    await db.flush()
    await _apply_issue_labels(issue, project, _label_names(body.get("labels")), db)
    await _apply_issue_assignees(
        issue,
        [int(value) for value in body.get("assignee_ids", []) if str(value).isdigit()],
        db,
    )
    await _apply_project_issue_milestone(issue, project, body, db)
    await db.commit()
    await db.refresh(issue)
    return _gitlab_issue_json(issue, BASE)


@router.get("/projects/{project_ref:path}/issues/{issue_iid}")
async def get_project_issue(
    project_ref: str,
    issue_iid: int,
    db: DbSession,
    current_user: CurrentUser,
):
    """Get a GitLab-shaped project issue by IID."""
    project = await _get_project_or_404(project_ref, db, current_user)
    issue = await _project_issue_or_404(project, issue_iid, db)
    return _gitlab_issue_json(issue, BASE)


@router.put("/projects/{project_ref:path}/issues/{issue_iid}")
@router.patch("/projects/{project_ref:path}/issues/{issue_iid}")
async def update_project_issue(
    project_ref: str,
    issue_iid: int,
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Update a GitLab-shaped project issue by IID."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, REPORTER)
    issue = await _project_issue_or_404(project, issue_iid, db)
    if "title" in body:
        issue.title = body["title"]
    if "description" in body:
        issue.body = body["description"]
    if "body" in body:
        issue.body = body["body"]
    if "state_event" in body:
        body["state"] = "closed" if body["state_event"] == "close" else "open"
    if "state" in body:
        old_state = issue.state
        new_state = "closed" if body["state"] in {"closed", "close"} else "open"
        issue.state = new_state
        if new_state == "closed" and old_state != "closed":
            issue.closed_at = datetime.now(timezone.utc)
            issue.closed_by_id = user.id
            project.open_issues_count = max(0, project.open_issues_count - 1)
        elif new_state == "open" and old_state != "open":
            issue.closed_at = None
            issue.closed_by_id = None
            project.open_issues_count += 1
    if "labels" in body:
        from sqlalchemy import delete

        await db.execute(delete(IssueLabel).where(IssueLabel.issue_id == issue.id))
        await _apply_issue_labels(issue, project, _label_names(body.get("labels")), db)
    if "assignee_ids" in body:
        from sqlalchemy import delete

        await db.execute(
            delete(IssueAssignee).where(IssueAssignee.issue_id == issue.id)
        )
        await _apply_issue_assignees(
            issue,
            [
                int(value)
                for value in body.get("assignee_ids", [])
                if str(value).isdigit()
            ],
            db,
        )
    await _apply_project_issue_milestone(issue, project, body, db)
    await db.commit()
    await db.refresh(issue)
    return _gitlab_issue_json(issue, BASE)


@router.get("/repos/{owner}/{repo}/issues")
async def list_issues(
    owner: str,
    repo: str,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    state: str = Query("open"),
    labels: Optional[str] = Query(None),
    sort: str = Query("created"),
    direction: str = Query("desc"),
    since: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List issues for a repository."""
    repository = await get_repo_or_404(owner, repo, db)

    query = select(Issue).where(Issue.repo_id == repository.id)

    if state != "all":
        query = query.where(Issue.state == state)

    # Filter by labels (comma-separated)
    if labels:
        label_names = [l.strip() for l in labels.split(",")]
        for label_name in label_names:
            label_subq = (
                select(IssueLabel.issue_id)
                .join(Label, Label.id == IssueLabel.label_id)
                .where(Label.name == label_name, Label.repo_id == repository.id)
            )
            query = query.where(Issue.id.in_(label_subq))

    # Sorting
    if sort == "updated":
        sort_col = Issue.updated_at
    elif sort == "comments":
        sort_col = Issue.created_at  # approximate
    else:
        sort_col = Issue.created_at

    if direction == "asc":
        query = query.order_by(sort_col.asc())
    else:
        query = query.order_by(sort_col.desc())

    # Total count
    count_q = select(sa_func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.offset((page - 1) * per_page).limit(per_page)
    issues = (await db.execute(query)).scalars().all()

    return paginated_json(
        [_issue_json(i, BASE) for i in issues],
        request,
        page,
        per_page,
        total,
    )


@router.post("/repos/{owner}/{repo}/issues", status_code=201)
async def create_issue(
    owner: str, repo: str, body: dict, user: AuthUser, db: DbSession
):
    """Create a new issue."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, REPORTER)

    title = body.get("title")
    if not title:
        raise HTTPException(status_code=422, detail="title is required")

    number = repository.next_issue_number
    repository.next_issue_number = number + 1
    repository.open_issues_count += 1

    issue = Issue(
        repo_id=repository.id,
        number=number,
        user_id=user.id,
        title=title,
        body=body.get("body"),
    )
    db.add(issue)
    await db.flush()

    # Labels
    label_names = body.get("labels", [])
    if label_names:
        for lname in label_names:
            result = await db.execute(
                select(Label).where(Label.repo_id == repository.id, Label.name == lname)
            )
            label = result.scalar_one_or_none()
            if label:
                db.add(IssueLabel(issue_id=issue.id, label_id=label.id))

    # Assignees
    assignee_logins = body.get("assignees", [])
    if assignee_logins:
        for login in assignee_logins:
            result = await db.execute(select(User).where(User.login == login))
            assignee = result.scalar_one_or_none()
            if assignee:
                db.add(IssueAssignee(issue_id=issue.id, user_id=assignee.id))

    # Milestone
    milestone_number = body.get("milestone")
    if milestone_number:
        from app.models.milestone import Milestone

        result = await db.execute(
            select(Milestone).where(
                Milestone.repo_id == repository.id,
                Milestone.number == milestone_number,
            )
        )
        ms = result.scalar_one_or_none()
        if ms:
            issue.milestone_id = ms.id

    await db.commit()
    await db.refresh(issue)
    return _issue_json(issue, BASE)


@router.get("/repos/{owner}/{repo}/issues/{issue_number}")
async def get_issue(
    owner: str, repo: str, issue_number: int, db: DbSession, current_user: CurrentUser
):
    """Get a single issue."""
    repository = await get_repo_or_404(owner, repo, db)

    result = await db.execute(
        select(Issue).where(
            Issue.repo_id == repository.id, Issue.number == issue_number
        )
    )
    issue = result.scalar_one_or_none()
    if issue is None:
        raise HTTPException(status_code=404, detail="Not Found")

    return _issue_json(issue, BASE)


@router.patch("/repos/{owner}/{repo}/issues/{issue_number}")
async def update_issue(
    owner: str,
    repo: str,
    issue_number: int,
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Update an issue."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, REPORTER)

    result = await db.execute(
        select(Issue).where(
            Issue.repo_id == repository.id, Issue.number == issue_number
        )
    )
    issue = result.scalar_one_or_none()
    if issue is None:
        raise HTTPException(status_code=404, detail="Not Found")

    if "title" in body:
        issue.title = body["title"]
    if "body" in body:
        issue.body = body["body"]
    if "state" in body:
        old_state = issue.state
        new_state = body["state"]
        issue.state = new_state
        if new_state == "closed" and old_state != "closed":
            issue.closed_at = datetime.now(timezone.utc)
            issue.closed_by_id = user.id
            repository.open_issues_count = max(0, repository.open_issues_count - 1)
        elif new_state == "open" and old_state != "open":
            issue.closed_at = None
            issue.closed_by_id = None
            repository.open_issues_count += 1
    if "state_reason" in body:
        issue.state_reason = body["state_reason"]
    if "milestone" in body:
        if body["milestone"] is None:
            issue.milestone_id = None
        else:
            from app.models.milestone import Milestone

            ms_result = await db.execute(
                select(Milestone).where(
                    Milestone.repo_id == repository.id,
                    Milestone.number == body["milestone"],
                )
            )
            ms = ms_result.scalar_one_or_none()
            if ms:
                issue.milestone_id = ms.id

    # Labels
    if "labels" in body:
        # Remove existing labels
        await db.execute(select(IssueLabel).where(IssueLabel.issue_id == issue.id))
        from sqlalchemy import delete

        await db.execute(delete(IssueLabel).where(IssueLabel.issue_id == issue.id))
        for lname in body["labels"]:
            lbl_result = await db.execute(
                select(Label).where(Label.repo_id == repository.id, Label.name == lname)
            )
            label = lbl_result.scalar_one_or_none()
            if label:
                db.add(IssueLabel(issue_id=issue.id, label_id=label.id))

    # Assignees
    if "assignees" in body:
        from sqlalchemy import delete

        await db.execute(
            delete(IssueAssignee).where(IssueAssignee.issue_id == issue.id)
        )
        for login in body["assignees"]:
            u_result = await db.execute(select(User).where(User.login == login))
            assignee = u_result.scalar_one_or_none()
            if assignee:
                db.add(IssueAssignee(issue_id=issue.id, user_id=assignee.id))

    await db.commit()
    await db.refresh(issue)
    return _issue_json(issue, BASE)
