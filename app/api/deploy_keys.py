"""Deploy key endpoints."""

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response
from sqlalchemy import select

from app.api.deps import AuthUser, CurrentUser, DbSession, get_repo_or_404
from app.api.pagination import paginated_json
from app.api.projects import _get_project_or_404
from app.config import settings
from app.models.deploy_key import DeployKey
from app.models.project import Project
from app.schemas.user import _fmt_dt
from app.services.permissions import MAINTAINER, require_project_access

router = APIRouter(tags=["deploy-keys"])

BASE = settings.BASE_URL


def _gitlab_key_json(key: DeployKey, project: Project, base_url: str) -> dict:
    api = f"{base_url}/api/v4"
    return {
        "id": key.id,
        "title": key.title,
        "key": key.key,
        "created_at": _fmt_dt(key.created_at),
        "expires_at": None,
        "can_push": not key.read_only,
        "fingerprint": None,
        "fingerprint_sha256": None,
        "projects_with_write_access": (
            [
                {
                    "id": project.id,
                    "name": project.name,
                    "path": project.name,
                    "path_with_namespace": project.full_name,
                }
            ]
            if not key.read_only
            else []
        ),
        "_links": {
            "self": f"{api}/projects/{project.id}/deploy_keys/{key.id}",
        },
    }


def _key_json(key: DeployKey, owner: str, repo_name: str, base_url: str) -> dict:
    api = f"{base_url}/api/v4"
    return {
        "id": key.id,
        "key": key.key,
        "url": f"{api}/repos/{owner}/{repo_name}/keys/{key.id}",
        "title": key.title,
        "verified": key.verified,
        "created_at": _fmt_dt(key.created_at),
        "read_only": key.read_only,
    }


@router.get("/projects/{project_ref:path}/deploy_keys")
async def list_project_deploy_keys(
    project_ref: str,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List GitLab-shaped deploy keys for a project."""
    project = await _get_project_or_404(project_ref, db, current_user)
    await require_project_access(project, current_user, db, MAINTAINER)
    all_keys = (
        (
            await db.execute(
                select(DeployKey)
                .where(DeployKey.repo_id == project.id)
                .order_by(DeployKey.id.asc())
            )
        )
        .scalars()
        .all()
    )
    start = (page - 1) * per_page
    items = [
        _gitlab_key_json(key, project, BASE)
        for key in all_keys[start : start + per_page]
    ]
    return paginated_json(
        items,
        total=len(all_keys),
        page=page,
        per_page=per_page,
        request=request,
    )


@router.post("/projects/{project_ref:path}/deploy_keys", status_code=201)
async def create_project_deploy_key(
    project_ref: str,
    user: AuthUser,
    db: DbSession,
    body: dict = Body(default_factory=dict),
):
    """Add a GitLab-shaped deploy key to a project."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, MAINTAINER)
    key_value = str(body.get("key") or "").strip()
    if not key_value:
        raise HTTPException(status_code=400, detail="key is missing")
    title = str(body.get("title") or "").strip() or "Deploy key"
    can_push = bool(body.get("can_push", False))

    deploy_key = DeployKey(
        repo_id=project.id,
        title=title,
        key=key_value,
        read_only=not can_push,
    )
    db.add(deploy_key)
    await db.commit()
    await db.refresh(deploy_key)
    return _gitlab_key_json(deploy_key, project, BASE)


@router.get("/projects/{project_ref:path}/deploy_keys/{key_id}")
async def get_project_deploy_key(
    project_ref: str,
    key_id: int,
    db: DbSession,
    current_user: CurrentUser,
):
    """Get one GitLab-shaped deploy key for a project."""
    project = await _get_project_or_404(project_ref, db, current_user)
    await require_project_access(project, current_user, db, MAINTAINER)
    result = await db.execute(
        select(DeployKey).where(DeployKey.id == key_id, DeployKey.repo_id == project.id)
    )
    deploy_key = result.scalar_one_or_none()
    if deploy_key is None:
        raise HTTPException(status_code=404, detail="404 Deploy Key Not Found")
    return _gitlab_key_json(deploy_key, project, BASE)


@router.delete("/projects/{project_ref:path}/deploy_keys/{key_id}", status_code=204)
async def delete_project_deploy_key(
    project_ref: str,
    key_id: int,
    user: AuthUser,
    db: DbSession,
):
    """Delete one GitLab-shaped deploy key from a project."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, MAINTAINER)
    result = await db.execute(
        select(DeployKey).where(DeployKey.id == key_id, DeployKey.repo_id == project.id)
    )
    deploy_key = result.scalar_one_or_none()
    if deploy_key is None:
        raise HTTPException(status_code=404, detail="404 Deploy Key Not Found")
    await db.delete(deploy_key)
    await db.commit()
    return Response(status_code=204)


@router.get("/repos/{owner}/{repo}/keys")
async def list_deploy_keys(
    owner: str, repo: str, db: DbSession, user: AuthUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List deploy keys."""
    repository = await get_repo_or_404(owner, repo, db)
    query = (
        select(DeployKey)
        .where(DeployKey.repo_id == repository.id)
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    keys = (await db.execute(query)).scalars().all()
    return [_key_json(k, owner, repo, BASE) for k in keys]


@router.post("/repos/{owner}/{repo}/keys", status_code=201)
async def create_deploy_key(
    owner: str, repo: str, body: dict, user: AuthUser, db: DbSession,
):
    """Add a deploy key."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, MAINTAINER)

    title = body.get("title", "")
    key_value = body.get("key", "")
    read_only = body.get("read_only", True)

    if not key_value:
        raise HTTPException(status_code=422, detail="key is required")

    dk = DeployKey(
        repo_id=repository.id,
        title=title,
        key=key_value,
        read_only=read_only,
    )
    db.add(dk)
    await db.commit()
    await db.refresh(dk)
    return _key_json(dk, owner, repo, BASE)


@router.get("/repos/{owner}/{repo}/keys/{key_id}")
async def get_deploy_key(
    owner: str, repo: str, key_id: int, db: DbSession, user: AuthUser,
):
    """Get a deploy key."""
    repository = await get_repo_or_404(owner, repo, db)
    result = await db.execute(
        select(DeployKey).where(DeployKey.id == key_id, DeployKey.repo_id == repository.id)
    )
    dk = result.scalar_one_or_none()
    if dk is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return _key_json(dk, owner, repo, BASE)


@router.delete("/repos/{owner}/{repo}/keys/{key_id}", status_code=204)
async def delete_deploy_key(
    owner: str, repo: str, key_id: int, user: AuthUser, db: DbSession,
):
    """Delete a deploy key."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, MAINTAINER)
    result = await db.execute(
        select(DeployKey).where(DeployKey.id == key_id, DeployKey.repo_id == repository.id)
    )
    dk = result.scalar_one_or_none()
    if dk is None:
        raise HTTPException(status_code=404, detail="Not Found")
    await db.delete(dk)
    await db.commit()
