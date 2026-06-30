"""GitLab snippet endpoints."""

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import select

from app.api.deps import AuthUser, CurrentUser, DbSession
from app.api.pagination import paginated_json
from app.api.projects import _get_project_or_404
from app.config import settings
from app.models.project import Project
from app.models.snippet import Snippet
from app.schemas.user import _fmt_dt
from app.services.permissions import DEVELOPER, REPORTER, require_project_access

router = APIRouter(tags=["snippets"])

VISIBILITIES = {"private", "internal", "public"}


def _snippet_json(snippet: Snippet, base_url: str) -> dict:
    project = snippet.project
    namespace = project.full_name if project else snippet.user.login
    web_url = (
        f"{base_url}/{project.full_name}/-/snippets/{snippet.id}"
        if project
        else f"{base_url}/-/snippets/{snippet.id}"
    )
    api = f"{base_url}/api/v4"
    return {
        "id": snippet.id,
        "title": snippet.title,
        "description": snippet.description,
        "file_name": snippet.file_name,
        "files": [{"path": snippet.file_name, "raw_url": f"{web_url}/raw"}],
        "visibility": snippet.visibility,
        "author": {
            "id": snippet.user.id,
            "username": snippet.user.login,
            "name": snippet.user.name or snippet.user.login,
            "state": "active",
            "avatar_url": snippet.user.avatar_url,
            "web_url": f"{base_url}/{snippet.user.login}",
        }
        if snippet.user
        else None,
        "project_id": project.id if project else None,
        "web_url": web_url,
        "raw_url": f"{api}/snippets/{snippet.id}/raw",
        "ssh_url_to_repo": None,
        "http_url_to_repo": None,
        "created_at": _fmt_dt(snippet.created_at),
        "updated_at": _fmt_dt(snippet.updated_at),
        "imported": False,
        "imported_from": "none",
        "namespace": namespace,
    }


async def _snippet_payload(request: Request) -> dict:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
        return dict(data) if isinstance(data, dict) else {}
    if "form" in content_type or "multipart" in content_type:
        form = await request.form()
        return dict(form)
    return {}


def _snippet_file_values(payload: dict) -> tuple[str | None, str | None]:
    files = payload.get("files")
    if isinstance(files, list) and files:
        first = files[0]
        if isinstance(first, dict):
            file_name = (
                first.get("file_path")
                or first.get("path")
                or first.get("filename")
                or first.get("file_name")
            )
            return (
                str(file_name).strip() if file_name else None,
                str(first.get("content")) if first.get("content") is not None else None,
            )
    return None, None


def _snippet_values(payload: dict) -> tuple[str, str | None, str, str, str]:
    title = str(payload.get("title") or "").strip()
    file_from_files, content_from_files = _snippet_file_values(payload)
    file_name = str(
        payload.get("file_name")
        or payload.get("filename")
        or file_from_files
        or "snippet.txt"
    ).strip()
    content = str(payload.get("content") or content_from_files or "").strip("\n")
    description = payload.get("description")
    visibility = str(payload.get("visibility") or "private").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is missing")
    if not file_name:
        raise HTTPException(status_code=400, detail="file_name is missing")
    if not content:
        raise HTTPException(status_code=400, detail="content is missing")
    if visibility not in VISIBILITIES:
        raise HTTPException(status_code=400, detail="visibility is invalid")
    return title, str(description) if description is not None else None, file_name, content, visibility


async def _create_snippet(
    user: AuthUser,
    db: DbSession,
    payload: dict,
    project: Project | None = None,
) -> dict:
    title, description, file_name, content, visibility = _snippet_values(payload)
    snippet = Snippet(
        user_id=user.id,
        project_id=project.id if project else None,
        title=title,
        description=description,
        file_name=file_name,
        content=content,
        visibility=visibility,
    )
    db.add(snippet)
    await db.commit()
    await db.refresh(snippet)
    return _snippet_json(snippet, settings.BASE_URL)


@router.get("/snippets")
async def list_personal_snippets(
    request: Request,
    user: AuthUser,
    db: DbSession,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    result = await db.execute(
        select(Snippet)
        .where(Snippet.user_id == user.id, Snippet.project_id.is_(None))
        .order_by(Snippet.id.desc())
    )
    snippets = result.scalars().all()
    start = (page - 1) * per_page
    return paginated_json(
        [_snippet_json(snippet, settings.BASE_URL) for snippet in snippets[start : start + per_page]],
        total=len(snippets),
        page=page,
        per_page=per_page,
        request=request,
    )


@router.post("/snippets", status_code=201)
async def create_personal_snippet(
    request: Request,
    user: AuthUser,
    db: DbSession,
):
    return await _create_snippet(user, db, await _snippet_payload(request))


@router.get("/snippets/{snippet_id}")
async def get_personal_snippet(
    snippet_id: int,
    user: AuthUser,
    db: DbSession,
):
    result = await db.execute(
        select(Snippet).where(Snippet.id == snippet_id, Snippet.user_id == user.id)
    )
    snippet = result.scalar_one_or_none()
    if snippet is None:
        raise HTTPException(status_code=404, detail="404 Snippet Not Found")
    return _snippet_json(snippet, settings.BASE_URL)


@router.delete("/snippets/{snippet_id}", status_code=204)
async def delete_personal_snippet(
    snippet_id: int,
    user: AuthUser,
    db: DbSession,
):
    result = await db.execute(
        select(Snippet).where(Snippet.id == snippet_id, Snippet.user_id == user.id)
    )
    snippet = result.scalar_one_or_none()
    if snippet is None:
        raise HTTPException(status_code=404, detail="404 Snippet Not Found")
    await db.delete(snippet)
    await db.commit()
    return Response(status_code=204)


@router.get("/projects/{project_ref:path}/snippets")
async def list_project_snippets(
    project_ref: str,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    project = await _get_project_or_404(project_ref, db, current_user)
    await require_project_access(project, current_user, db, REPORTER)
    result = await db.execute(
        select(Snippet)
        .where(Snippet.project_id == project.id)
        .order_by(Snippet.id.desc())
    )
    snippets = result.scalars().all()
    start = (page - 1) * per_page
    return paginated_json(
        [_snippet_json(snippet, settings.BASE_URL) for snippet in snippets[start : start + per_page]],
        total=len(snippets),
        page=page,
        per_page=per_page,
        request=request,
    )


@router.post("/projects/{project_ref:path}/snippets", status_code=201)
async def create_project_snippet(
    project_ref: str,
    request: Request,
    user: AuthUser,
    db: DbSession,
):
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, DEVELOPER)
    return await _create_snippet(
        user,
        db,
        await _snippet_payload(request),
        project=project,
    )
