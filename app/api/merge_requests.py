"""GitLab merge request API.

This is a GitLab-shaped facade over the existing merge request and issue
storage. The backing table name remains legacy-compatible with the original
GitHub emulator, but this module uses GitLab-facing model names.
"""

import asyncio
import os
from datetime import datetime, timezone
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import AuthUser, CurrentUser, DbSession
from app.api.gitlab_commits import _commit_json as _gitlab_commit_json
from app.api.gitlab_commits import _COMMIT_FORMAT
from app.api.pagination import paginated_json
from app.api.pipelines import (
    CreatePipelineRequest,
    PipelineVariable,
    _create_pipeline,
    _pipeline_json,
)
from app.api.projects import _get_project_or_404
from app.api.pulls import _perform_git_merge
from app.config import settings
from app.models.issue import Issue
from app.models.merge_request import MergeRequest
from app.models.ci import Pipeline
from app.models.project import Project
from app.schemas.user import _fmt_dt
from app.services.permissions import DEVELOPER, require_project_access

router = APIRouter(tags=["merge-requests"])


def _mr_query():
    return (
        select(MergeRequest)
        .options(
            selectinload(MergeRequest.issue).selectinload(Issue.user),
            selectinload(MergeRequest.issue).selectinload(Issue.closed_by),
            selectinload(MergeRequest.repository).selectinload(Project.owner),
            selectinload(MergeRequest.head_repository),
            selectinload(MergeRequest.merged_by),
        )
    )


async def _git_text(repo_path: str, *args: str) -> str:
    env = {**os.environ, "GIT_DIR": repo_path}
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode())
    return stdout.decode()


async def _resolve_branch_sha(project: Project, branch: str) -> str:
    if not project.disk_path or not os.path.isdir(project.disk_path):
        raise HTTPException(status_code=404, detail="404 Project Not Found")
    try:
        return (
            await _git_text(
                project.disk_path,
                "rev-parse",
                f"refs/heads/{unquote(branch)}^{{commit}}",
            )
        ).strip()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=f"Branch not found: {branch}") from exc


def _user_json(user, base_url: str) -> dict | None:
    if user is None:
        return None
    return {
        "id": user.id,
        "username": user.login,
        "name": user.name or user.login,
        "state": "active",
        "avatar_url": user.avatar_url,
        "web_url": f"{base_url}/{user.login}",
    }


def _mr_state(merge_request: MergeRequest) -> str:
    if merge_request.merged:
        return "merged"
    return "opened" if merge_request.issue.state == "open" else "closed"


def _merge_status(merge_request: MergeRequest) -> tuple[str, str]:
    state = _mr_state(merge_request)
    if state == "merged":
        return "unchecked", "merged"
    if state == "closed":
        return "cannot_be_merged", "not_open"
    if merge_request.mergeable is False:
        return "cannot_be_merged", "not_mergeable"
    return "can_be_merged", "mergeable"


def _mr_json(
    merge_request: MergeRequest,
    base_url: str,
    *,
    changes_count: int | str | None = None,
    head_pipeline: Pipeline | None = None,
) -> dict:
    issue = merge_request.issue
    project = merge_request.repository
    author = issue.user if issue else None
    iid = issue.number
    state = _mr_state(merge_request)
    merge_status, detailed_merge_status = _merge_status(merge_request)
    web_url = f"{base_url}/{project.full_name}/-/merge_requests/{iid}"
    api_url = f"{base_url}/api/v4/projects/{project.id}/merge_requests/{iid}"
    closed_by = issue.closed_by if issue and issue.closed_by else merge_request.merged_by

    pipeline_json = _pipeline_json(head_pipeline) if head_pipeline else None
    return {
        "id": merge_request.id,
        "iid": iid,
        "project_id": project.id,
        "title": issue.title,
        "description": issue.body,
        "state": state,
        "created_at": _fmt_dt(issue.created_at),
        "updated_at": _fmt_dt(issue.updated_at),
        "merged_by": _user_json(merge_request.merged_by, base_url),
        "merge_user": _user_json(merge_request.merged_by, base_url),
        "merged_at": _fmt_dt(merge_request.merged_at),
        "closed_by": _user_json(closed_by, base_url),
        "closed_at": _fmt_dt(issue.closed_at),
        "target_branch": merge_request.base_ref,
        "source_branch": merge_request.head_ref,
        "source_branch_exists": True,
        "target_branch_exists": True,
        "user_notes_count": 0,
        "upvotes": 0,
        "downvotes": 0,
        "author": _user_json(author, base_url),
        "assignees": [],
        "assignee": None,
        "reviewers": [],
        "source_project_id": merge_request.head_repo_id or project.id,
        "target_project_id": project.id,
        "labels": [],
        "draft": merge_request.draft,
        "work_in_progress": merge_request.draft,
        "milestone": None,
        "merge_when_pipeline_succeeds": False,
        "merge_status": merge_status,
        "detailed_merge_status": detailed_merge_status,
        "sha": merge_request.head_sha,
        "merge_commit_sha": merge_request.merge_commit_sha,
        "squash_commit_sha": None,
        "discussion_locked": None,
        "should_remove_source_branch": None,
        "remove_source_branch": False,
        "force_remove_source_branch": False,
        "reference": f"!{iid}",
        "references": {
            "short": f"!{iid}",
            "relative": f"!{iid}",
            "full": f"{project.full_name}!{iid}",
        },
        "web_url": web_url,
        "time_stats": {
            "time_estimate": 0,
            "total_time_spent": 0,
            "human_time_estimate": None,
            "human_total_time_spent": None,
        },
        "squash": False,
        "subscribed": False,
        "changes_count": None if changes_count is None else str(changes_count),
        "latest_build_started_at": None,
        "latest_build_finished_at": None,
        "first_deployed_to_production_at": None,
        "pipeline": pipeline_json,
        "head_pipeline": pipeline_json,
        "diff_refs": {
            "base_sha": merge_request.base_sha,
            "head_sha": merge_request.head_sha,
            "start_sha": merge_request.base_sha,
        },
        "merge_error": None,
        "first_contribution": False,
        "task_completion_status": {"count": 0, "completed_count": 0},
        "has_conflicts": merge_request.mergeable is False,
        "blocking_discussions_resolved": True,
        "approvals_before_merge": None,
        "url": api_url,
        "user": {"can_merge": True},
    }


def _merge_request_pipeline_variables(
    project: Project,
    merge_request: MergeRequest,
) -> list[PipelineVariable]:
    issue = merge_request.issue
    iid = str(issue.number)
    return [
        PipelineVariable(key="CI_MERGE_REQUEST_ID", value=str(merge_request.id)),
        PipelineVariable(key="CI_MERGE_REQUEST_IID", value=iid),
        PipelineVariable(key="CI_MERGE_REQUEST_PROJECT_ID", value=str(project.id)),
        PipelineVariable(key="CI_MERGE_REQUEST_SOURCE_BRANCH_NAME", value=merge_request.head_ref),
        PipelineVariable(key="CI_MERGE_REQUEST_TARGET_BRANCH_NAME", value=merge_request.base_ref),
        PipelineVariable(key="CI_MERGE_REQUEST_SOURCE_BRANCH_SHA", value=merge_request.head_sha),
        PipelineVariable(key="CI_MERGE_REQUEST_TARGET_BRANCH_SHA", value=merge_request.base_sha),
        PipelineVariable(key="CI_MERGE_REQUEST_SOURCE_PROJECT_PATH", value=project.full_name),
        PipelineVariable(key="CI_MERGE_REQUEST_TARGET_PROJECT_PATH", value=project.full_name),
    ]


async def _latest_merge_request_pipeline(
    project: Project,
    merge_request: MergeRequest,
    db: DbSession,
) -> Pipeline | None:
    result = await db.execute(
        select(Pipeline)
        .options(selectinload(Pipeline.project))
        .where(
            Pipeline.project_id == project.id,
            Pipeline.source == "merge_request_event",
            Pipeline.ref == merge_request.head_ref,
            Pipeline.sha == merge_request.head_sha,
        )
        .order_by(Pipeline.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _mr_json_with_pipeline(
    merge_request: MergeRequest,
    project: Project,
    db: DbSession,
    *,
    changes_count: int | str | None = None,
) -> dict:
    head_pipeline = await _latest_merge_request_pipeline(project, merge_request, db)
    return _mr_json(
        merge_request,
        settings.BASE_URL,
        changes_count=changes_count,
        head_pipeline=head_pipeline,
    )


async def _create_merge_request_event_pipeline(
    project: Project,
    merge_request: MergeRequest,
    db: DbSession,
    *,
    actor,
    allow_skip: bool,
) -> Pipeline | None:
    try:
        return await _create_pipeline(
            project.id,
            CreatePipelineRequest(
                ref=merge_request.head_ref,
                sha=merge_request.head_sha,
                variables=_merge_request_pipeline_variables(project, merge_request),
            ),
            db,
            source="merge_request_event",
            actor=actor,
        )
    except HTTPException as exc:
        if not allow_skip:
            raise
        await db.rollback()
        await db.refresh(project)
        await db.refresh(merge_request)
        detail = str(exc.detail)
        if exc.status_code == 400 and (
            ".gitlab-ci.yml not found" in detail
            or "workflow rules skipped pipeline" in detail
        ):
            return None
        raise


async def _validate_source_target(
    project: Project,
    source_branch: str,
    target_branch: str,
) -> tuple[str, str]:
    if source_branch == target_branch:
        raise HTTPException(
            status_code=400,
            detail="source_branch and target_branch must be different",
        )
    head_sha = await _resolve_branch_sha(project, source_branch)
    base_sha = await _resolve_branch_sha(project, target_branch)
    return head_sha, base_sha


async def _refresh_branch_shas(project: Project, merge_request: MergeRequest) -> None:
    merge_request.head_sha, merge_request.base_sha = await _validate_source_target(
        project,
        merge_request.head_ref,
        merge_request.base_ref,
    )


async def _change_entries(
    project: Project,
    merge_request: MergeRequest,
    *,
    include_diff: bool,
) -> list[dict]:
    try:
        output = await _git_text(
            project.disk_path,
            "diff",
            "--name-status",
            "-M",
            merge_request.base_sha,
            merge_request.head_sha,
        )
    except RuntimeError:
        output = ""

    changes = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:
            old_path = parts[1]
            new_path = parts[2]
            renamed = True
        else:
            old_path = parts[1]
            new_path = old_path
            renamed = False

        diff = ""
        if include_diff:
            try:
                diff = await _git_text(
                    project.disk_path,
                    "diff",
                    "--src-prefix=a/",
                    "--dst-prefix=b/",
                    merge_request.base_sha,
                    merge_request.head_sha,
                    "--",
                    new_path,
                )
            except RuntimeError:
                diff = ""

        changes.append(
            {
                "old_path": old_path,
                "new_path": new_path,
                "a_mode": "100644",
                "b_mode": "100644",
                "diff": diff,
                "new_file": status == "A",
                "renamed_file": renamed,
                "deleted_file": status == "D",
                "too_large": False,
                "collapsed": False,
                "generated_file": False,
            }
        )
    return changes


async def _get_mr_or_404(project: Project, iid: int, db: DbSession) -> MergeRequest:
    result = await db.execute(
        _mr_query()
        .join(Issue, MergeRequest.issue_id == Issue.id)
        .where(MergeRequest.repo_id == project.id, Issue.number == iid)
    )
    merge_request = result.scalar_one_or_none()
    if merge_request is None:
        raise HTTPException(status_code=404, detail="404 Merge Request Not Found")
    return merge_request


@router.get("/projects/{project_ref:path}/merge_requests")
async def list_merge_requests(
    project_ref: str,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    state: str = Query("opened"),
    source_branch: str | None = Query(None),
    target_branch: str | None = Query(None),
    order_by: str = Query("created_at"),
    sort: str = Query("desc"),
    view: str | None = Query(None),
    with_merge_status_recheck: bool | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List merge requests for a GitLab project."""
    project = await _get_project_or_404(project_ref, db, current_user)
    query = (
        _mr_query()
        .join(Issue, MergeRequest.issue_id == Issue.id)
        .where(MergeRequest.repo_id == project.id)
    )

    if state in {"opened", "open"}:
        query = query.where(Issue.state == "open", MergeRequest.merged == False)
    elif state == "closed":
        query = query.where(Issue.state == "closed", MergeRequest.merged == False)
    elif state == "merged":
        query = query.where(MergeRequest.merged == True)
    elif state != "all":
        raise HTTPException(status_code=400, detail="Invalid state")

    if source_branch:
        query = query.where(MergeRequest.head_ref == source_branch)
    if target_branch:
        query = query.where(MergeRequest.base_ref == target_branch)

    sort_col = Issue.updated_at if order_by == "updated_at" else Issue.created_at
    query = query.order_by(sort_col.asc() if sort == "asc" else sort_col.desc())
    total = (
        await db.execute(select(sa_func.count()).select_from(query.subquery()))
    ).scalar() or 0
    query = query.offset((page - 1) * per_page).limit(per_page)
    merge_requests = (await db.execute(query)).scalars().all()
    items = [
        await _mr_json_with_pipeline(merge_request, project, db)
        for merge_request in merge_requests
    ]
    return paginated_json(items, request, page, per_page, total)


@router.post("/projects/{project_ref:path}/merge_requests", status_code=201)
async def create_merge_request(
    project_ref: str,
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Create a GitLab-shaped merge request."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, DEVELOPER)
    title = body.get("title")
    source_branch = body.get("source_branch")
    target_branch = body.get("target_branch")
    if not title or not source_branch or not target_branch:
        raise HTTPException(
            status_code=400,
            detail="title, source_branch, and target_branch are required",
        )

    head_sha, base_sha = await _validate_source_target(
        project,
        source_branch,
        target_branch,
    )

    existing = await db.execute(
        select(sa_func.count())
        .select_from(MergeRequest)
        .join(Issue, MergeRequest.issue_id == Issue.id)
        .where(
            MergeRequest.repo_id == project.id,
            MergeRequest.head_ref == source_branch,
            MergeRequest.base_ref == target_branch,
            Issue.state == "open",
            MergeRequest.merged == False,
        )
    )
    if (existing.scalar() or 0) > 0:
        raise HTTPException(status_code=409, detail="Merge request already exists")

    number = project.next_issue_number
    project.next_issue_number = number + 1
    project.open_issues_count += 1
    issue = Issue(
        repo_id=project.id,
        number=number,
        user_id=user.id,
        title=title,
        body=body.get("description") or body.get("body"),
        state="open",
    )
    db.add(issue)
    await db.flush()

    merge_request = MergeRequest(
        issue_id=issue.id,
        repo_id=project.id,
        head_ref=source_branch,
        head_sha=head_sha,
        head_repo_id=project.id,
        base_ref=target_branch,
        base_sha=base_sha,
        draft=bool(body.get("draft", False)),
        mergeable=True,
    )
    db.add(merge_request)
    await db.commit()

    merge_request = await _get_mr_or_404(project, number, db)
    await _create_merge_request_event_pipeline(
        project,
        merge_request,
        db,
        actor=user,
        allow_skip=True,
    )
    merge_request = await _get_mr_or_404(project, number, db)
    return await _mr_json_with_pipeline(merge_request, project, db)


@router.get("/projects/{project_ref:path}/merge_requests/{iid}")
async def get_merge_request(
    project_ref: str,
    iid: int,
    db: DbSession,
    current_user: CurrentUser,
):
    """Get one merge request for a GitLab project."""
    project = await _get_project_or_404(project_ref, db, current_user)
    merge_request = await _get_mr_or_404(project, iid, db)
    return await _mr_json_with_pipeline(merge_request, project, db)


@router.get("/projects/{project_ref:path}/merge_requests/{iid}/commits")
async def get_merge_request_commits(
    project_ref: str,
    iid: int,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List commits in a GitLab-shaped merge request."""
    project = await _get_project_or_404(project_ref, db, current_user)
    merge_request = await _get_mr_or_404(project, iid, db)
    try:
        total = int(
            (
                await _git_text(
                    project.disk_path,
                    "rev-list",
                    "--count",
                    f"{merge_request.base_sha}..{merge_request.head_sha}",
                )
            ).strip()
            or "0"
        )
        output = await _git_text(
            project.disk_path,
            "log",
            f"--format={_COMMIT_FORMAT}",
            f"--skip={(page - 1) * per_page}",
            f"-{per_page}",
            f"{merge_request.base_sha}..{merge_request.head_sha}",
        )
    except RuntimeError:
        return paginated_json([], request, page, per_page, 0)
    commits = []
    for record in output.split("\x1e"):
        if not record.strip():
            continue
        try:
            commits.append(_gitlab_commit_json(project, record))
        except ValueError:
            continue
    return paginated_json(commits, request, page, per_page, total)


@router.post("/projects/{project_ref:path}/merge_requests/{iid}/pipelines", status_code=201)
async def create_merge_request_pipeline(
    project_ref: str,
    iid: int,
    user: AuthUser,
    db: DbSession,
):
    """Create a GitLab-shaped merge request event pipeline."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, DEVELOPER)
    merge_request = await _get_mr_or_404(project, iid, db)
    await _refresh_branch_shas(project, merge_request)
    pipeline = await _create_merge_request_event_pipeline(
        project,
        merge_request,
        db,
        actor=user,
        allow_skip=False,
    )
    return _pipeline_json(pipeline)


@router.get("/projects/{project_ref:path}/merge_requests/{iid}/changes")
async def get_merge_request_changes(
    project_ref: str,
    iid: int,
    db: DbSession,
    current_user: CurrentUser,
):
    """Get a GitLab-shaped merge request with changed files."""
    project = await _get_project_or_404(project_ref, db, current_user)
    merge_request = await _get_mr_or_404(project, iid, db)
    changes = await _change_entries(project, merge_request, include_diff=True)
    data = await _mr_json_with_pipeline(
        merge_request,
        project,
        db,
        changes_count=len(changes),
    )
    data["changes"] = changes
    data["overflow"] = False
    return data


@router.get("/projects/{project_ref:path}/merge_requests/{iid}/diffs")
async def get_merge_request_diffs(
    project_ref: str,
    iid: int,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List changed file diffs for a GitLab-shaped merge request."""
    project = await _get_project_or_404(project_ref, db, current_user)
    merge_request = await _get_mr_or_404(project, iid, db)
    changes = await _change_entries(project, merge_request, include_diff=True)
    start = (page - 1) * per_page
    return paginated_json(
        changes[start:start + per_page],
        request,
        page,
        per_page,
        len(changes),
    )


@router.put("/projects/{project_ref:path}/merge_requests/{iid}")
async def update_merge_request(
    project_ref: str,
    iid: int,
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Update a GitLab-shaped merge request."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, DEVELOPER)
    merge_request = await _get_mr_or_404(project, iid, db)
    issue = merge_request.issue
    pipeline_relevant_update = False

    if "title" in body:
        issue.title = body["title"]
    if "description" in body:
        issue.body = body["description"]
    if "target_branch" in body:
        if merge_request.merged:
            raise HTTPException(
                status_code=400,
                detail="Cannot update a merged merge request",
            )
        merge_request.base_ref = body["target_branch"]
        merge_request.head_sha, merge_request.base_sha = await _validate_source_target(
            project,
            merge_request.head_ref,
            merge_request.base_ref,
        )
        pipeline_relevant_update = True
    if "source_branch" in body:
        if merge_request.merged:
            raise HTTPException(
                status_code=400,
                detail="Cannot update a merged merge request",
            )
        merge_request.head_ref = body["source_branch"]
        merge_request.head_sha, merge_request.base_sha = await _validate_source_target(
            project,
            merge_request.head_ref,
            merge_request.base_ref,
        )
        pipeline_relevant_update = True
    if "draft" in body:
        merge_request.draft = bool(body["draft"])

    state_event = body.get("state_event")
    if state_event == "close":
        if issue.state != "closed":
            issue.state = "closed"
            issue.closed_at = datetime.now(timezone.utc)
            issue.closed_by_id = user.id
            issue.closed_by = user
            project.open_issues_count = max(0, project.open_issues_count - 1)
    elif state_event == "reopen":
        if merge_request.merged:
            raise HTTPException(
                status_code=400,
                detail="Cannot reopen a merged merge request",
            )
        if issue.state != "open":
            issue.state = "open"
            issue.closed_at = None
            issue.closed_by_id = None
            issue.closed_by = None
            if not merge_request.merged:
                project.open_issues_count += 1
            pipeline_relevant_update = True
    elif state_event is not None:
        raise HTTPException(status_code=400, detail="Invalid state_event")

    await db.commit()
    merge_request = await _get_mr_or_404(project, iid, db)
    if issue.state == "open" and pipeline_relevant_update:
        await _create_merge_request_event_pipeline(
            project,
            merge_request,
            db,
            actor=user,
            allow_skip=True,
        )
        merge_request = await _get_mr_or_404(project, iid, db)
    return await _mr_json_with_pipeline(merge_request, project, db)


@router.put("/projects/{project_ref:path}/merge_requests/{iid}/merge")
async def merge_merge_request(
    project_ref: str,
    iid: int,
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Merge a GitLab-shaped merge request."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, DEVELOPER)
    merge_request = await _get_mr_or_404(project, iid, db)
    if merge_request.merged:
        raise HTTPException(status_code=405, detail="Merge request already merged")
    if merge_request.issue.state == "closed":
        raise HTTPException(status_code=422, detail="Merge request is closed")
    await _refresh_branch_shas(project, merge_request)
    if body.get("sha") and body["sha"] != merge_request.head_sha:
        raise HTTPException(
            status_code=409,
            detail="SHA does not match HEAD of source branch",
        )

    commit_title = body.get("merge_commit_message") or body.get(
        "commit_message",
        f"Merge branch '{merge_request.head_ref}' into '{merge_request.base_ref}'",
    )
    merge_method = body.get("merge_method", "merge")
    if merge_method not in {"merge", "squash", "rebase"}:
        raise HTTPException(status_code=400, detail="Invalid merge_method")
    git_sha = await _perform_git_merge(
        disk_path=project.disk_path,
        head_ref=merge_request.head_ref,
        base_ref=merge_request.base_ref,
        merge_method=merge_method,
        commit_message=commit_title,
    )
    if not git_sha:
        merge_request.mergeable = False
        await db.commit()
        raise HTTPException(status_code=405, detail="Branch cannot be merged")

    now = datetime.now(timezone.utc)
    merge_request.merged = True
    merge_request.merged_at = now
    merge_request.merged_by_id = user.id
    merge_request.merged_by = user
    merge_request.merge_commit_sha = git_sha
    merge_request.mergeable = True
    merge_request.issue.state = "closed"
    merge_request.issue.closed_at = now
    merge_request.issue.closed_by_id = user.id
    merge_request.issue.closed_by = user
    project.open_issues_count = max(0, project.open_issues_count - 1)
    await db.commit()

    return await _mr_json_with_pipeline(
        await _get_mr_or_404(project, iid, db),
        project,
        db,
    )
