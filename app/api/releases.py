"""Release endpoints -- CRUD and asset management."""

from datetime import datetime, timezone

from urllib.parse import unquote

from fastapi import APIRouter, Body, HTTPException, Query, Request
from sqlalchemy import func as sa_func
from sqlalchemy import select

from app.api.deps import AuthUser, CurrentUser, DbSession, get_repo_or_404
from app.api.pagination import paginated_json
from app.api.projects import _get_project_or_404, _git_text, _resolve_commit_ref
from app.config import settings
from app.models.release import Release, ReleaseAsset
from app.models.project import Project
from app.schemas.user import SimpleUser, _fmt_dt, _make_node_id

router = APIRouter(tags=["releases"])

BASE = settings.BASE_URL


def _release_json(release: Release, owner: str, repo_name: str, base_url: str) -> dict:
    api = f"{base_url}/api/v4"
    author = SimpleUser.from_db(release.author, base_url).model_dump() if release.author else None

    assets = []
    if release.assets:
        for a in release.assets:
            uploader = SimpleUser.from_db(a.uploader, base_url).model_dump() if a.uploader else None
            assets.append({
                "url": f"{api}/repos/{owner}/{repo_name}/releases/assets/{a.id}",
                "id": a.id,
                "node_id": _make_node_id("ReleaseAsset", a.id),
                "name": a.name,
                "label": a.label,
                "uploader": uploader,
                "content_type": a.content_type,
                "state": a.state,
                "size": a.size,
                "download_count": a.download_count,
                "created_at": _fmt_dt(a.created_at),
                "updated_at": _fmt_dt(a.updated_at),
                "browser_download_url": a.browser_download_url,
            })

    return {
        "url": f"{api}/repos/{owner}/{repo_name}/releases/{release.id}",
        "assets_url": f"{api}/repos/{owner}/{repo_name}/releases/{release.id}/assets",
        "upload_url": f"{api}/repos/{owner}/{repo_name}/releases/{release.id}/assets{{?name,label}}",
        "html_url": f"{base_url}/{owner}/{repo_name}/releases/tag/{release.tag_name}",
        "id": release.id,
        "node_id": _make_node_id("Release", release.id),
        "tag_name": release.tag_name,
        "target_commitish": release.target_commitish,
        "name": release.name,
        "draft": release.draft,
        "prerelease": release.prerelease,
        "created_at": _fmt_dt(release.created_at),
        "published_at": _fmt_dt(release.published_at),
        "author": author,
        "assets": assets,
        "tarball_url": f"{api}/repos/{owner}/{repo_name}/tarball/{release.tag_name}",
        "zipball_url": f"{api}/repos/{owner}/{repo_name}/zipball/{release.tag_name}",
        "body": release.body,
    }


def _gitlab_release_json(release: Release, project: Project, base_url: str) -> dict:
    author = (
        SimpleUser.from_db(release.author, base_url).model_dump()
        if release.author
        else None
    )
    sources_url = f"{base_url}/{project.full_name}/-/archive/{release.tag_name}"
    links = []
    if release.assets:
        for asset in release.assets:
            links.append(
                {
                    "id": asset.id,
                    "name": asset.name,
                    "url": asset.browser_download_url,
                    "direct_asset_url": asset.browser_download_url,
                    "link_type": "other",
                    "external": False,
                }
            )

    return {
        "tag_name": release.tag_name,
        "tag_path": f"/{project.full_name}/-/tags/{release.tag_name}",
        "name": release.name or release.tag_name,
        "description": release.body or "",
        "created_at": _fmt_dt(release.created_at),
        "released_at": _fmt_dt(release.published_at or release.created_at),
        "upcoming_release": release.draft,
        "author": author,
        "commit": None,
        "milestones": [],
        "commit_path": None,
        "tag_message": None,
        "evidences": [],
        "assets": {
            "count": len(links) + 2,
            "sources": [
                {
                    "format": "zip",
                    "url": f"{sources_url}.zip",
                },
                {
                    "format": "tar.gz",
                    "url": f"{sources_url}.tar.gz",
                },
            ],
            "links": links,
        },
        "_links": {
            "self": f"{base_url}/api/v4/projects/{project.id}/releases/{release.tag_name}",
            "edit_url": f"{base_url}/{project.full_name}/-/releases/{release.tag_name}/edit",
            "closed_issues_url": f"{base_url}/{project.full_name}/-/releases/{release.tag_name}/downloads",
            "opened_issues_url": f"{base_url}/{project.full_name}/-/releases/{release.tag_name}/downloads",
        },
    }


async def _get_project_release_or_404(
    project: Project,
    tag_name: str,
    db: DbSession,
) -> Release:
    result = await db.execute(
        select(Release).where(
            Release.repo_id == project.id,
            Release.tag_name == unquote(tag_name),
        )
    )
    release = result.scalar_one_or_none()
    if release is None:
        raise HTTPException(status_code=404, detail="404 Release Not Found")
    return release


async def _ensure_release_tag(
    project: Project,
    tag_name: str,
    ref: str | None,
) -> None:
    if not project.disk_path:
        return
    try:
        await _git_text(project.disk_path, "rev-parse", f"refs/tags/{tag_name}^{{commit}}")
        return
    except RuntimeError:
        pass
    if not ref:
        return
    sha = await _resolve_commit_ref(project, ref)
    try:
        await _git_text(project.disk_path, "update-ref", f"refs/tags/{tag_name}", sha)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail="Could not create tag") from exc


@router.get("/projects/{project_ref:path}/releases")
async def list_project_releases(
    project_ref: str,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List GitLab-shaped project releases."""
    project = await _get_project_or_404(project_ref, db, current_user)
    query = (
        select(Release)
        .where(Release.repo_id == project.id)
        .order_by(Release.created_at.desc())
    )
    total = (await db.execute(select(sa_func.count()).select_from(query.subquery()))).scalar() or 0
    result = await db.execute(query.offset((page - 1) * per_page).limit(per_page))
    return paginated_json(
        [_gitlab_release_json(release, project, BASE) for release in result.scalars().all()],
        request,
        page,
        per_page,
        total,
    )


@router.post("/projects/{project_ref:path}/releases", status_code=201)
async def create_project_release(
    project_ref: str,
    user: AuthUser,
    db: DbSession,
    body: dict = Body(...),
):
    """Create a GitLab-shaped project release."""
    project = await _get_project_or_404(project_ref, db, user)
    tag_name = str(body.get("tag_name") or "").strip()
    if not tag_name:
        raise HTTPException(status_code=400, detail="tag_name is required")

    existing = await db.execute(
        select(Release).where(Release.repo_id == project.id, Release.tag_name == tag_name)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Release already exists")

    await _ensure_release_tag(project, tag_name, body.get("ref") or project.default_branch)
    now = datetime.now(timezone.utc)
    release = Release(
        repo_id=project.id,
        tag_name=tag_name,
        target_commitish=body.get("ref") or body.get("target") or project.default_branch,
        name=body.get("name") or tag_name,
        body=body.get("description") or body.get("body") or "",
        draft=bool(body.get("upcoming_release", False)),
        prerelease=False,
        author_id=user.id,
        published_at=now,
    )
    db.add(release)
    await db.commit()
    release = await _get_project_release_or_404(project, tag_name, db)
    return _gitlab_release_json(release, project, BASE)


@router.get("/projects/{project_ref:path}/releases/{tag_name:path}")
async def get_project_release(
    project_ref: str,
    tag_name: str,
    db: DbSession,
    current_user: CurrentUser,
):
    """Get one GitLab-shaped project release by tag name."""
    project = await _get_project_or_404(project_ref, db, current_user)
    release = await _get_project_release_or_404(project, tag_name, db)
    return _gitlab_release_json(release, project, BASE)


@router.put("/projects/{project_ref:path}/releases/{tag_name:path}")
async def update_project_release(
    project_ref: str,
    tag_name: str,
    user: AuthUser,
    db: DbSession,
    body: dict = Body(...),
):
    """Update a GitLab-shaped project release."""
    project = await _get_project_or_404(project_ref, db, user)
    release = await _get_project_release_or_404(project, tag_name, db)
    if "name" in body:
        release.name = body["name"]
    if "description" in body:
        release.body = body["description"]
    if "released_at" in body:
        # Preserve the current timestamp parser surface for now; clients mostly
        # need the field reflected as a published release rather than scheduled.
        release.published_at = release.published_at or datetime.now(timezone.utc)
    if "upcoming_release" in body:
        release.draft = bool(body["upcoming_release"])
    await db.commit()
    release = await _get_project_release_or_404(project, release.tag_name, db)
    return _gitlab_release_json(release, project, BASE)


@router.delete("/projects/{project_ref:path}/releases/{tag_name:path}")
async def delete_project_release(
    project_ref: str,
    tag_name: str,
    user: AuthUser,
    db: DbSession,
):
    """Delete a GitLab-shaped project release without deleting its git tag."""
    project = await _get_project_or_404(project_ref, db, user)
    release = await _get_project_release_or_404(project, tag_name, db)
    data = _gitlab_release_json(release, project, BASE)
    await db.delete(release)
    await db.commit()
    return data


@router.get("/repos/{owner}/{repo}/releases")
async def list_releases(
    owner: str, repo: str, request: Request, db: DbSession, current_user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List releases."""
    repository = await get_repo_or_404(owner, repo, db)
    query = (
        select(Release)
        .where(Release.repo_id == repository.id)
        .order_by(Release.created_at.desc())
    )
    total = (await db.execute(select(sa_func.count()).select_from(query.subquery()))).scalar() or 0
    releases = (await db.execute(query.offset((page - 1) * per_page).limit(per_page))).scalars().all()
    return paginated_json(
        [_release_json(r, owner, repo, BASE) for r in releases],
        request,
        page,
        per_page,
        total,
    )


@router.post("/repos/{owner}/{repo}/releases", status_code=201)
async def create_release(
    owner: str, repo: str, body: dict, user: AuthUser, db: DbSession
):
    """Create a release."""
    repository = await get_repo_or_404(owner, repo, db)

    tag_name = body.get("tag_name")
    if not tag_name:
        raise HTTPException(status_code=422, detail="tag_name is required")

    now = datetime.now(timezone.utc)
    release = Release(
        repo_id=repository.id,
        tag_name=tag_name,
        target_commitish=body.get("target_commitish", repository.default_branch),
        name=body.get("name"),
        body=body.get("body"),
        draft=body.get("draft", False),
        prerelease=body.get("prerelease", False),
        author_id=user.id,
        published_at=None if body.get("draft") else now,
    )
    db.add(release)
    await db.commit()
    await db.refresh(release)
    return _release_json(release, owner, repo, BASE)


@router.get("/repos/{owner}/{repo}/releases/{release_id}")
async def get_release(
    owner: str, repo: str, release_id: int, db: DbSession, current_user: CurrentUser
):
    """Get a release."""
    repository = await get_repo_or_404(owner, repo, db)
    result = await db.execute(
        select(Release).where(
            Release.id == release_id, Release.repo_id == repository.id
        )
    )
    release = result.scalar_one_or_none()
    if release is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return _release_json(release, owner, repo, BASE)


@router.get("/repos/{owner}/{repo}/releases/latest")
async def get_latest_release(
    owner: str, repo: str, db: DbSession, current_user: CurrentUser
):
    """Get the latest release."""
    repository = await get_repo_or_404(owner, repo, db)
    result = await db.execute(
        select(Release)
        .where(Release.repo_id == repository.id, Release.draft == False)
        .order_by(Release.created_at.desc())
        .limit(1)
    )
    release = result.scalar_one_or_none()
    if release is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return _release_json(release, owner, repo, BASE)


@router.get("/repos/{owner}/{repo}/releases/tags/{tag}")
async def get_release_by_tag(
    owner: str, repo: str, tag: str, db: DbSession, current_user: CurrentUser
):
    """Get a release by tag name."""
    repository = await get_repo_or_404(owner, repo, db)
    result = await db.execute(
        select(Release).where(
            Release.repo_id == repository.id, Release.tag_name == tag
        )
    )
    release = result.scalar_one_or_none()
    if release is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return _release_json(release, owner, repo, BASE)


@router.patch("/repos/{owner}/{repo}/releases/{release_id}")
async def update_release(
    owner: str, repo: str, release_id: int, body: dict, user: AuthUser, db: DbSession
):
    """Update a release."""
    repository = await get_repo_or_404(owner, repo, db)
    result = await db.execute(
        select(Release).where(
            Release.id == release_id, Release.repo_id == repository.id
        )
    )
    release = result.scalar_one_or_none()
    if release is None:
        raise HTTPException(status_code=404, detail="Not Found")

    for key in ("tag_name", "target_commitish", "name", "body", "draft", "prerelease"):
        if key in body:
            setattr(release, key, body[key])

    await db.commit()
    await db.refresh(release)
    return _release_json(release, owner, repo, BASE)


@router.delete("/repos/{owner}/{repo}/releases/{release_id}", status_code=204)
async def delete_release(
    owner: str, repo: str, release_id: int, user: AuthUser, db: DbSession
):
    """Delete a release."""
    repository = await get_repo_or_404(owner, repo, db)
    result = await db.execute(
        select(Release).where(
            Release.id == release_id, Release.repo_id == repository.id
        )
    )
    release = result.scalar_one_or_none()
    if release is None:
        raise HTTPException(status_code=404, detail="Not Found")
    await db.delete(release)
    await db.commit()
