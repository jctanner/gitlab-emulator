"""Minimal GitLab generic package registry endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from app.api.deps import AuthUser, CurrentUser, DbSession
from app.api.projects import _get_project_or_404
from app.config import settings
from app.services.permissions import DEVELOPER, REPORTER, require_project_access

router = APIRouter(tags=["packages"])


def _safe_package_path(
    project_id: int,
    package_name: str,
    package_version: str,
    file_name: str,
) -> Path:
    parts = [package_name, package_version, *file_name.split("/")]
    if any(not part or part in {".", ".."} for part in parts):
        raise HTTPException(status_code=400, detail="Invalid package path")
    root = Path(settings.DATA_DIR) / "packages" / "generic" / str(project_id)
    candidate = root.joinpath(*parts).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise HTTPException(status_code=400, detail="Invalid package path")
    return candidate


@router.put(
    "/projects/{project_ref:path}/packages/generic/{package_name}/{package_version}/{file_name:path}",
    status_code=201,
)
async def upload_generic_package_file(
    project_ref: str,
    package_name: str,
    package_version: str,
    file_name: str,
    request: Request,
    user: AuthUser,
    db: DbSession,
):
    """Upload one generic package file."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, DEVELOPER)
    path = _safe_package_path(project.id, package_name, package_version, file_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(await request.body())
    return {
        "message": "201 Created",
        "package_name": package_name,
        "package_version": package_version,
        "file_name": file_name,
        "size": path.stat().st_size,
    }


@router.get(
    "/projects/{project_ref:path}/packages/generic/{package_name}/{package_version}/{file_name:path}"
)
async def download_generic_package_file(
    project_ref: str,
    package_name: str,
    package_version: str,
    file_name: str,
    db: DbSession,
    current_user: CurrentUser,
):
    """Download one generic package file."""
    project = await _get_project_or_404(project_ref, db, current_user)
    if current_user is not None:
        await require_project_access(project, current_user, db, REPORTER)
    path = _safe_package_path(project.id, package_name, package_version, file_name)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Package file not found")
    return FileResponse(path, filename=Path(file_name).name)


@router.head(
    "/projects/{project_ref:path}/packages/generic/{package_name}/{package_version}/{file_name:path}"
)
async def head_generic_package_file(
    project_ref: str,
    package_name: str,
    package_version: str,
    file_name: str,
    db: DbSession,
    current_user: CurrentUser,
):
    """Return metadata for one generic package file."""
    project = await _get_project_or_404(project_ref, db, current_user)
    if current_user is not None:
        await require_project_access(project, current_user, db, REPORTER)
    path = _safe_package_path(project.id, package_name, package_version, file_name)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Package file not found")
    return Response(headers={"Content-Length": str(path.stat().st_size)})
