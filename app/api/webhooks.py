"""Webhook endpoints -- CRUD and delivery listing."""

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func as sa_func
from sqlalchemy import select

from app.api.deps import AuthUser, CurrentUser, DbSession, get_repo_or_404
from app.api.groups import _get_group_or_404
from app.api.pagination import paginated_json
from app.api.projects import _get_project_or_404
from app.config import settings
from app.models.group import Group
from app.models.project import Project
from app.models.webhook import Webhook, WebhookDelivery
from app.schemas.user import _fmt_dt, _make_node_id
from app.services.permissions import MAINTAINER, OWNER, require_group_access, require_project_access

router = APIRouter(tags=["webhooks"])

BASE = settings.BASE_URL


def _hook_json(hook: Webhook, owner: str, repo_name: str, base_url: str) -> dict:
    api = f"{base_url}/api/v4"
    return {
        "type": "Project",
        "id": hook.id,
        "name": "web",
        "active": hook.active,
        "events": hook.events or ["push"],
        "config": {
            "content_type": hook.content_type,
            "insecure_ssl": "1" if hook.insecure_ssl else "0",
            "url": hook.url,
        },
        "updated_at": _fmt_dt(hook.updated_at),
        "created_at": _fmt_dt(hook.created_at),
        "url": f"{api}/repos/{owner}/{repo_name}/hooks/{hook.id}",
        "test_url": f"{api}/repos/{owner}/{repo_name}/hooks/{hook.id}/tests",
        "ping_url": f"{api}/repos/{owner}/{repo_name}/hooks/{hook.id}/pings",
        "deliveries_url": f"{api}/repos/{owner}/{repo_name}/hooks/{hook.id}/deliveries",
        "last_response": {"code": None, "status": "unused", "message": None},
    }


GITLAB_EVENT_FIELDS = {
    "push_events": "push_events",
    "issues_events": "issues_events",
    "confidential_issues_events": "confidential_issues_events",
    "merge_requests_events": "merge_requests_events",
    "tag_push_events": "tag_push_events",
    "note_events": "note_events",
    "job_events": "job_events",
    "pipeline_events": "pipeline_events",
    "wiki_page_events": "wiki_page_events",
    "deployment_events": "deployment_events",
    "releases_events": "releases_events",
}


def _gitlab_events_from_body(body: dict, current: list | None = None) -> list:
    events = set(current or [])
    if not current and not any(field in body for field in GITLAB_EVENT_FIELDS):
        events.add("push_events")
    for field in GITLAB_EVENT_FIELDS:
        if field in body:
            if body[field]:
                events.add(field)
            else:
                events.discard(field)
    return sorted(events)


def _gitlab_hook_json(
    hook: Webhook,
    base_url: str,
    project: Project | None = None,
    group: Group | None = None,
) -> dict:
    target_path = project.full_name if project else group.login
    target_type = "projects" if project else "groups"
    api_id = project.id if project else group.id
    events = set(hook.events or [])
    data = {
        "id": hook.id,
        "url": hook.url,
        "project_id": project.id if project else None,
        "group_id": group.id if group else None,
        "created_at": _fmt_dt(hook.created_at),
        "enable_ssl_verification": not hook.insecure_ssl,
        "alert_status": "executable",
        "disabled_until": None,
        "url_variables": [],
        "token": hook.secret,
        "description": "",
        "owner": None,
        "web_url": f"{base_url}/{target_path}/-/hooks/{hook.id}",
        "_links": {
            "self": f"{base_url}/api/v4/{target_type}/{api_id}/hooks/{hook.id}",
        },
    }
    for field in GITLAB_EVENT_FIELDS:
        data[field] = field in events
    return data


async def _get_project_hook_or_404(
    project: Project,
    hook_id: int,
    db: DbSession,
) -> Webhook:
    result = await db.execute(
        select(Webhook).where(Webhook.id == hook_id, Webhook.repo_id == project.id)
    )
    hook = result.scalar_one_or_none()
    if hook is None:
        raise HTTPException(status_code=404, detail="404 Hook Not Found")
    return hook


async def _get_group_hook_or_404(
    group: Group,
    hook_id: int,
    db: DbSession,
) -> Webhook:
    result = await db.execute(
        select(Webhook).where(Webhook.id == hook_id, Webhook.org_id == group.id)
    )
    hook = result.scalar_one_or_none()
    if hook is None:
        raise HTTPException(status_code=404, detail="404 Hook Not Found")
    return hook


@router.get("/projects/{project_ref:path}/hooks")
async def list_project_hooks(
    project_ref: str,
    request: Request,
    db: DbSession,
    user: AuthUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List GitLab-shaped project hooks."""
    project = await _get_project_or_404(project_ref, db, user)
    query = (
        select(Webhook)
        .where(Webhook.repo_id == project.id)
        .order_by(Webhook.id)
    )
    total = (await db.execute(select(sa_func.count()).select_from(query.subquery()))).scalar() or 0
    result = await db.execute(query.offset((page - 1) * per_page).limit(per_page))
    return paginated_json(
        [_gitlab_hook_json(hook, BASE, project=project) for hook in result.scalars().all()],
        request,
        page,
        per_page,
        total,
    )


@router.post("/projects/{project_ref:path}/hooks", status_code=201)
async def create_project_hook(
    project_ref: str,
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Create a GitLab-shaped project hook."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, MAINTAINER)
    url = body.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    hook = Webhook(
        repo_id=project.id,
        url=url,
        secret=body.get("token"),
        content_type="json",
        insecure_ssl=not bool(body.get("enable_ssl_verification", True)),
        events=_gitlab_events_from_body(body),
        active=True,
    )
    db.add(hook)
    await db.commit()
    await db.refresh(hook)
    return _gitlab_hook_json(hook, BASE, project=project)


@router.get("/projects/{project_ref:path}/hooks/{hook_id}")
async def get_project_hook(
    project_ref: str,
    hook_id: int,
    db: DbSession,
    user: AuthUser,
):
    """Get one GitLab-shaped project hook."""
    project = await _get_project_or_404(project_ref, db, user)
    hook = await _get_project_hook_or_404(project, hook_id, db)
    return _gitlab_hook_json(hook, BASE, project=project)


@router.put("/projects/{project_ref:path}/hooks/{hook_id}")
async def update_project_hook(
    project_ref: str,
    hook_id: int,
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Update a GitLab-shaped project hook."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, MAINTAINER)
    hook = await _get_project_hook_or_404(project, hook_id, db)
    if "url" in body:
        hook.url = body["url"]
    if "token" in body:
        hook.secret = body["token"]
    if "enable_ssl_verification" in body:
        hook.insecure_ssl = not bool(body["enable_ssl_verification"])
    hook.events = _gitlab_events_from_body(body, hook.events)
    await db.commit()
    await db.refresh(hook)
    return _gitlab_hook_json(hook, BASE, project=project)


@router.delete("/projects/{project_ref:path}/hooks/{hook_id}", status_code=204)
async def delete_project_hook(
    project_ref: str,
    hook_id: int,
    user: AuthUser,
    db: DbSession,
):
    """Delete a GitLab-shaped project hook."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, MAINTAINER)
    hook = await _get_project_hook_or_404(project, hook_id, db)
    await db.delete(hook)
    await db.commit()


@router.get("/groups/{group_ref:path}/hooks")
async def list_group_hooks(
    group_ref: str,
    request: Request,
    db: DbSession,
    user: AuthUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List GitLab-shaped group hooks."""
    group = await _get_group_or_404(group_ref, db)
    query = (
        select(Webhook)
        .where(Webhook.org_id == group.id)
        .order_by(Webhook.id)
    )
    total = (await db.execute(select(sa_func.count()).select_from(query.subquery()))).scalar() or 0
    result = await db.execute(query.offset((page - 1) * per_page).limit(per_page))
    return paginated_json(
        [_gitlab_hook_json(hook, BASE, group=group) for hook in result.scalars().all()],
        request,
        page,
        per_page,
        total,
    )


@router.post("/groups/{group_ref:path}/hooks", status_code=201)
async def create_group_hook(
    group_ref: str,
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Create a GitLab-shaped group hook."""
    group = await _get_group_or_404(group_ref, db)
    await require_group_access(group, user, db, OWNER)
    url = body.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    hook = Webhook(
        org_id=group.id,
        url=url,
        secret=body.get("token"),
        content_type="json",
        insecure_ssl=not bool(body.get("enable_ssl_verification", True)),
        events=_gitlab_events_from_body(body),
        active=True,
    )
    db.add(hook)
    await db.commit()
    await db.refresh(hook)
    return _gitlab_hook_json(hook, BASE, group=group)


@router.get("/groups/{group_ref:path}/hooks/{hook_id}")
async def get_group_hook(
    group_ref: str,
    hook_id: int,
    db: DbSession,
    user: AuthUser,
):
    """Get one GitLab-shaped group hook."""
    group = await _get_group_or_404(group_ref, db)
    hook = await _get_group_hook_or_404(group, hook_id, db)
    return _gitlab_hook_json(hook, BASE, group=group)


@router.put("/groups/{group_ref:path}/hooks/{hook_id}")
async def update_group_hook(
    group_ref: str,
    hook_id: int,
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Update a GitLab-shaped group hook."""
    group = await _get_group_or_404(group_ref, db)
    await require_group_access(group, user, db, OWNER)
    hook = await _get_group_hook_or_404(group, hook_id, db)
    if "url" in body:
        hook.url = body["url"]
    if "token" in body:
        hook.secret = body["token"]
    if "enable_ssl_verification" in body:
        hook.insecure_ssl = not bool(body["enable_ssl_verification"])
    hook.events = _gitlab_events_from_body(body, hook.events)
    await db.commit()
    await db.refresh(hook)
    return _gitlab_hook_json(hook, BASE, group=group)


@router.delete("/groups/{group_ref:path}/hooks/{hook_id}", status_code=204)
async def delete_group_hook(
    group_ref: str,
    hook_id: int,
    user: AuthUser,
    db: DbSession,
):
    """Delete a GitLab-shaped group hook."""
    group = await _get_group_or_404(group_ref, db)
    await require_group_access(group, user, db, OWNER)
    hook = await _get_group_hook_or_404(group, hook_id, db)
    await db.delete(hook)
    await db.commit()


@router.get("/repos/{owner}/{repo}/hooks")
async def list_hooks(
    owner: str, repo: str, request: Request, db: DbSession, user: AuthUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List repository webhooks."""
    repository = await get_repo_or_404(owner, repo, db)
    query = (
        select(Webhook)
        .where(Webhook.repo_id == repository.id)
    )
    total = (await db.execute(select(sa_func.count()).select_from(query.subquery()))).scalar() or 0
    hooks = (await db.execute(query.offset((page - 1) * per_page).limit(per_page))).scalars().all()
    return paginated_json(
        [_hook_json(h, owner, repo, BASE) for h in hooks],
        request,
        page,
        per_page,
        total,
    )


@router.post("/repos/{owner}/{repo}/hooks", status_code=201)
async def create_hook(
    owner: str, repo: str, body: dict, user: AuthUser, db: DbSession
):
    """Create a webhook."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, MAINTAINER)

    config = body.get("config", {})
    url = config.get("url")
    if not url:
        raise HTTPException(status_code=422, detail="config.url is required")

    hook = Webhook(
        repo_id=repository.id,
        url=url,
        secret=config.get("secret"),
        content_type=config.get("content_type", "json"),
        insecure_ssl=config.get("insecure_ssl", "0") == "1",
        events=body.get("events", ["push"]),
        active=body.get("active", True),
    )
    db.add(hook)
    await db.commit()
    await db.refresh(hook)
    return _hook_json(hook, owner, repo, BASE)


@router.get("/repos/{owner}/{repo}/hooks/{hook_id}")
async def get_hook(
    owner: str, repo: str, hook_id: int, db: DbSession, user: AuthUser
):
    """Get a single webhook."""
    repository = await get_repo_or_404(owner, repo, db)
    result = await db.execute(
        select(Webhook).where(Webhook.id == hook_id, Webhook.repo_id == repository.id)
    )
    hook = result.scalar_one_or_none()
    if hook is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return _hook_json(hook, owner, repo, BASE)


@router.patch("/repos/{owner}/{repo}/hooks/{hook_id}")
async def update_hook(
    owner: str, repo: str, hook_id: int, body: dict, user: AuthUser, db: DbSession
):
    """Update a webhook."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, MAINTAINER)
    result = await db.execute(
        select(Webhook).where(Webhook.id == hook_id, Webhook.repo_id == repository.id)
    )
    hook = result.scalar_one_or_none()
    if hook is None:
        raise HTTPException(status_code=404, detail="Not Found")

    config = body.get("config", {})
    if "url" in config:
        hook.url = config["url"]
    if "secret" in config:
        hook.secret = config["secret"]
    if "content_type" in config:
        hook.content_type = config["content_type"]
    if "insecure_ssl" in config:
        hook.insecure_ssl = config["insecure_ssl"] == "1"
    if "events" in body:
        hook.events = body["events"]
    if "active" in body:
        hook.active = body["active"]

    await db.commit()
    await db.refresh(hook)
    return _hook_json(hook, owner, repo, BASE)


@router.delete("/repos/{owner}/{repo}/hooks/{hook_id}", status_code=204)
async def delete_hook(
    owner: str, repo: str, hook_id: int, user: AuthUser, db: DbSession
):
    """Delete a webhook."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, MAINTAINER)
    result = await db.execute(
        select(Webhook).where(Webhook.id == hook_id, Webhook.repo_id == repository.id)
    )
    hook = result.scalar_one_or_none()
    if hook is None:
        raise HTTPException(status_code=404, detail="Not Found")
    await db.delete(hook)
    await db.commit()


@router.get("/repos/{owner}/{repo}/hooks/{hook_id}/deliveries")
async def list_deliveries(
    owner: str, repo: str, hook_id: int, request: Request, db: DbSession, user: AuthUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List deliveries for a webhook."""
    repository = await get_repo_or_404(owner, repo, db)
    result = await db.execute(
        select(Webhook).where(Webhook.id == hook_id, Webhook.repo_id == repository.id)
    )
    hook = result.scalar_one_or_none()
    if hook is None:
        raise HTTPException(status_code=404, detail="Not Found")

    query = (
        select(WebhookDelivery)
        .where(WebhookDelivery.webhook_id == hook.id)
        .order_by(WebhookDelivery.delivered_at.desc())
    )
    total = (await db.execute(select(sa_func.count()).select_from(query.subquery()))).scalar() or 0
    deliveries = (await db.execute(query.offset((page - 1) * per_page).limit(per_page))).scalars().all()

    return paginated_json(
        [
            {
                "id": d.id,
                "event": d.event,
                "action": d.action,
                "status_code": d.status_code,
                "delivered_at": _fmt_dt(d.delivered_at),
                "duration": d.duration,
                "success": d.success,
            }
            for d in deliveries
        ],
        request,
        page,
        per_page,
        total,
    )
