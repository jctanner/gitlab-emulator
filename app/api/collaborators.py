"""Collaborator endpoints -- list, check, add, remove, permissions."""

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import select
from urllib.parse import unquote

from app.api.deps import AuthUser, CurrentUser, DbSession, get_repo_or_404
from app.api.pagination import paginated_json
from app.config import settings
from app.models.project import Project
from app.models.repository import Collaborator
from app.models.user import User
from app.schemas.user import SimpleUser, _fmt_dt, _make_node_id
from app.services.permissions import (
    MAINTAINER,
    REPORTER,
    access_level_for_permission,
    collaborator_access_level,
    project_access_level,
    require_project_access,
)

router = APIRouter(tags=["collaborators"])

BASE = settings.BASE_URL

ACCESS_TO_PERMISSION = {
    10: "pull",
    20: "pull",
    30: "push",
    40: "maintain",
    50: "admin",
}
PERMISSION_TO_ACCESS = {
    "pull": 20,
    "triage": 20,
    "push": 30,
    "maintain": 40,
    "admin": 50,
}


def _collab_json(user_obj: User, permission: str, base_url: str) -> dict:
    simple = SimpleUser.from_db(user_obj, base_url).model_dump()
    perm_map = {
        "admin": {
            "admin": True,
            "maintain": True,
            "push": True,
            "triage": True,
            "pull": True,
        },
        "maintain": {
            "admin": False,
            "maintain": True,
            "push": True,
            "triage": True,
            "pull": True,
        },
        "push": {
            "admin": False,
            "maintain": False,
            "push": True,
            "triage": True,
            "pull": True,
        },
        "triage": {
            "admin": False,
            "maintain": False,
            "push": False,
            "triage": True,
            "pull": True,
        },
        "pull": {
            "admin": False,
            "maintain": False,
            "push": False,
            "triage": False,
            "pull": True,
        },
    }
    simple["permissions"] = perm_map.get(permission, perm_map["pull"])
    simple["role_name"] = permission
    return simple


async def _get_project_or_404(
    project_ref: str,
    db: DbSession,
    user: User | None = None,
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
        user is None or await project_access_level(project, user, db) < REPORTER
    ):
        raise HTTPException(status_code=404, detail="Project Not Found")
    return project


def _member_json(
    user_obj: User,
    access_level: int,
    base_url: str,
    *,
    created_at=None,
) -> dict:
    simple = SimpleUser.from_db(user_obj, base_url).model_dump()
    return {
        "id": user_obj.id,
        "username": user_obj.login,
        "name": user_obj.name or user_obj.login,
        "state": "active",
        "locked": False,
        "avatar_url": simple["avatar_url"],
        "web_url": simple["web_url"],
        "access_level": access_level,
        "expires_at": None,
        "membership_state": "active",
        "created_at": _fmt_dt(created_at),
        "created_by": None,
        "invite_email": None,
        "group_saml_identity": None,
        "saml_provider_id": None,
        "group_scim_identity": None,
    }


async def _project_member(
    project: Project, user_id: int, db: DbSession
) -> tuple[User, int] | None:
    if project.owner and project.owner.id == user_id:
        return project.owner, 50
    result = await db.execute(
        select(Collaborator).where(
            Collaborator.repo_id == project.id,
            Collaborator.user_id == user_id,
        )
    )
    collaborator = result.scalar_one_or_none()
    if collaborator is None or collaborator.user is None:
        return None
    return collaborator.user, collaborator_access_level(collaborator)


@router.get("/projects/{project_ref:path}/members")
async def list_project_members(
    project_ref: str,
    request: Request,
    db: DbSession,
    user: AuthUser,
    query: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List GitLab-shaped project members."""
    project = await _get_project_or_404(project_ref, db, user)
    members: list[dict] = []
    seen_user_ids: set[int] = set()
    if project.owner:
        members.append(_member_json(project.owner, 50, BASE))
        seen_user_ids.add(project.owner.id)
    result = await db.execute(
        select(Collaborator).where(Collaborator.repo_id == project.id)
    )
    for collaborator in result.scalars().all():
        if collaborator.user and collaborator.user.id not in seen_user_ids:
            access = collaborator_access_level(collaborator)
            members.append(_member_json(collaborator.user, access, BASE))
            seen_user_ids.add(collaborator.user.id)
    if query:
        lowered = query.lower()
        members = [
            member
            for member in members
            if lowered in member["username"].lower()
            or lowered in member["name"].lower()
        ]
    members.sort(key=lambda member: member["id"])
    total = len(members)
    start = (page - 1) * per_page
    return paginated_json(
        members[start : start + per_page],
        request,
        page,
        per_page,
        total,
    )


@router.get("/projects/{project_ref:path}/members/{user_id}")
async def get_project_member(
    project_ref: str,
    user_id: int,
    db: DbSession,
    user: AuthUser,
):
    """Get one GitLab-shaped project member."""
    project = await _get_project_or_404(project_ref, db, user)
    member = await _project_member(project, user_id, db)
    if member is None:
        raise HTTPException(status_code=404, detail="Member Not Found")
    member_user, access_level = member
    return _member_json(member_user, access_level, BASE)


@router.post("/projects/{project_ref:path}/members", status_code=201)
async def add_project_member(
    project_ref: str,
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Add or update a GitLab-shaped project member."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, MAINTAINER)
    user_id = body.get("user_id")
    if user_id is None:
        raise HTTPException(status_code=422, detail="user_id is required")
    result = await db.execute(select(User).where(User.id == int(user_id)))
    target_user = result.scalar_one_or_none()
    if target_user is None:
        raise HTTPException(status_code=404, detail="User Not Found")
    if project.owner and project.owner.id == target_user.id:
        return _member_json(target_user, 50, BASE)
    access_level = int(body.get("access_level") or 30)
    permission = ACCESS_TO_PERMISSION.get(access_level, "push")
    existing = await db.execute(
        select(Collaborator).where(
            Collaborator.repo_id == project.id,
            Collaborator.user_id == target_user.id,
        )
    )
    collaborator = existing.scalar_one_or_none()
    if collaborator:
        collaborator.permission = permission
        collaborator.access_level = access_level
    else:
        db.add(
            Collaborator(
                repo_id=project.id,
                user_id=target_user.id,
                permission=permission,
                access_level=access_level,
            )
        )
    await db.commit()
    return _member_json(target_user, access_level, BASE)


@router.delete("/projects/{project_ref:path}/members/{user_id}", status_code=204)
async def delete_project_member(
    project_ref: str,
    user_id: int,
    user: AuthUser,
    db: DbSession,
):
    """Remove a GitLab-shaped project member."""
    project = await _get_project_or_404(project_ref, db, user)
    await require_project_access(project, user, db, MAINTAINER)
    result = await db.execute(
        select(Collaborator).where(
            Collaborator.repo_id == project.id,
            Collaborator.user_id == user_id,
        )
    )
    collaborator = result.scalar_one_or_none()
    if collaborator is None:
        raise HTTPException(status_code=404, detail="Member Not Found")
    await db.delete(collaborator)
    await db.commit()
    return Response(status_code=204)


@router.get("/repos/{owner}/{repo}/collaborators")
async def list_collaborators(
    owner: str,
    repo: str,
    db: DbSession,
    user: AuthUser,
):
    """List collaborators for a repository."""
    repository = await get_repo_or_404(owner, repo, db)
    result = await db.execute(
        select(Collaborator).where(Collaborator.repo_id == repository.id)
    )
    collabs = result.scalars().all()

    # Include the owner
    owner_user = repository.owner
    items = []
    if owner_user:
        items.append(_collab_json(owner_user, "admin", BASE))
    for c in collabs:
        if c.user:
            items.append(_collab_json(c.user, c.permission, BASE))
    return items


@router.get("/repos/{owner}/{repo}/collaborators/{username}")
async def check_collaborator(
    owner: str,
    repo: str,
    username: str,
    db: DbSession,
    user: AuthUser,
):
    """Check if a user is a collaborator (204 = yes, 404 = no)."""
    repository = await get_repo_or_404(owner, repo, db)

    # Owner is always a collaborator
    if repository.owner and repository.owner.login == username:
        return Response(status_code=204)

    result = await db.execute(
        select(Collaborator)
        .join(User, Collaborator.user_id == User.id)
        .where(Collaborator.repo_id == repository.id, User.login == username)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return Response(status_code=204)


@router.put("/repos/{owner}/{repo}/collaborators/{username}", status_code=201)
async def add_collaborator(
    owner: str,
    repo: str,
    username: str,
    body: dict,
    user: AuthUser,
    db: DbSession,
):
    """Add a collaborator to a repository."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, MAINTAINER)
    permission = body.get("permission", "push")

    result = await db.execute(select(User).where(User.login == username))
    target_user = result.scalar_one_or_none()
    if target_user is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Check if already a collaborator
    existing = await db.execute(
        select(Collaborator).where(
            Collaborator.repo_id == repository.id,
            Collaborator.user_id == target_user.id,
        )
    )
    collab = existing.scalar_one_or_none()
    if collab:
        collab.permission = permission
        collab.access_level = access_level_for_permission(permission)
    else:
        collab = Collaborator(
            repo_id=repository.id,
            user_id=target_user.id,
            permission=permission,
            access_level=access_level_for_permission(permission),
        )
        db.add(collab)

    await db.commit()
    return {"message": "Invitation created"}


@router.delete("/repos/{owner}/{repo}/collaborators/{username}", status_code=204)
async def remove_collaborator(
    owner: str,
    repo: str,
    username: str,
    user: AuthUser,
    db: DbSession,
):
    """Remove a collaborator."""
    repository = await get_repo_or_404(owner, repo, db)
    await require_project_access(repository, user, db, MAINTAINER)

    result = await db.execute(
        select(Collaborator)
        .join(User, Collaborator.user_id == User.id)
        .where(Collaborator.repo_id == repository.id, User.login == username)
    )
    collab = result.scalar_one_or_none()
    if collab is None:
        raise HTTPException(status_code=404, detail="Not Found")

    await db.delete(collab)
    await db.commit()


@router.get("/repos/{owner}/{repo}/collaborators/{username}/permission")
async def get_collaborator_permission(
    owner: str,
    repo: str,
    username: str,
    db: DbSession,
    user: AuthUser,
):
    """Get a collaborator's permission level."""
    repository = await get_repo_or_404(owner, repo, db)

    # Check owner
    if repository.owner and repository.owner.login == username:
        return {
            "permission": "admin",
            "role_name": "admin",
            "user": SimpleUser.from_db(repository.owner, BASE).model_dump(),
        }

    result = await db.execute(
        select(Collaborator)
        .join(User, Collaborator.user_id == User.id)
        .where(Collaborator.repo_id == repository.id, User.login == username)
    )
    collab = result.scalar_one_or_none()
    if collab is None:
        raise HTTPException(status_code=404, detail="Not Found")

    return {
        "permission": collab.permission,
        "role_name": collab.permission,
        "user": SimpleUser.from_db(collab.user, BASE).model_dump(),
    }
