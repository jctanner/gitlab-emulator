"""GitLab namespace endpoints backed by users and groups."""

from __future__ import annotations

from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.api.pagination import paginated_json
from app.config import settings
from app.models.group import Group
from app.models.organization import OrgMembership
from app.models.user import User
from app.schemas.user import _fmt_dt

router = APIRouter(tags=["namespaces"])


def _user_namespace_json(user: User) -> dict:
    base_url = settings.BASE_URL
    return {
        "id": user.id,
        "name": user.name or user.login,
        "path": user.login,
        "kind": "user",
        "full_path": user.login,
        "parent_id": None,
        "avatar_url": user.avatar_url,
        "web_url": f"{base_url}/{user.login}",
        "members_count_with_descendants": 1,
        "billable_members_count": 1,
        "created_at": _fmt_dt(user.created_at),
        "updated_at": _fmt_dt(user.updated_at),
    }


def _group_namespace_json(group: Group, parent_id: int | None = None) -> dict:
    base_url = settings.BASE_URL
    path = group.login.rsplit("/", 1)[-1]
    return {
        "id": group.id,
        "name": group.name or group.login,
        "path": path,
        "kind": "group",
        "full_path": group.login,
        "parent_id": parent_id,
        "avatar_url": group.avatar_url,
        "web_url": f"{base_url}/groups/{group.login}",
        "members_count_with_descendants": None,
        "billable_members_count": None,
        "created_at": _fmt_dt(group.created_at),
        "updated_at": _fmt_dt(group.updated_at),
    }


async def _visible_group_ids(db: DbSession, current_user: User | None) -> set[int] | None:
    if current_user is None:
        return set()
    if current_user.site_admin:
        return None
    result = await db.execute(
        select(OrgMembership.org_id).where(
            OrgMembership.user_id == current_user.id,
            OrgMembership.state == "active",
        )
    )
    return {row[0] for row in result.all()}


async def _parent_ids_for_groups(groups: list[Group], db: DbSession) -> dict[str, int]:
    parent_paths = {
        group.login.rsplit("/", 1)[0] for group in groups if "/" in group.login
    }
    if not parent_paths:
        return {}
    result = await db.execute(select(Group).where(Group.login.in_(parent_paths)))
    return {parent.login: parent.id for parent in result.scalars().all()}


def _matches_search(namespace: dict, search: str | None) -> bool:
    if not search:
        return True
    needle = search.lower()
    return needle in namespace["full_path"].lower() or needle in namespace["name"].lower()


@router.get("/namespaces")
async def list_namespaces(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    search: str | None = Query(None),
    owned_only: bool = Query(False),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List user and group namespaces in a GitLab-compatible shape."""
    users = (await db.execute(select(User).order_by(User.login.asc()))).scalars().all()
    visible_group_ids = await _visible_group_ids(db, current_user)
    group_query = select(Group).order_by(Group.login.asc())
    if visible_group_ids is not None:
        if visible_group_ids:
            group_query = group_query.where(Group.id.in_(visible_group_ids))
        else:
            group_query = group_query.where(False)
    groups = (await db.execute(group_query)).scalars().all()
    parent_ids = await _parent_ids_for_groups(groups, db)

    namespaces = []
    for user in users:
        if owned_only and (current_user is None or user.id != current_user.id):
            continue
        item = _user_namespace_json(user)
        if _matches_search(item, search):
            namespaces.append(item)
    for group in groups:
        if owned_only and current_user is not None and not current_user.site_admin:
            if visible_group_ids is not None and group.id not in visible_group_ids:
                continue
        parent_path = group.login.rsplit("/", 1)[0] if "/" in group.login else None
        item = _group_namespace_json(
            group,
            parent_ids.get(parent_path) if parent_path else None,
        )
        if _matches_search(item, search):
            namespaces.append(item)

    namespaces.sort(key=lambda item: (item["full_path"], item["kind"]))
    total = len(namespaces)
    start = (page - 1) * per_page
    end = start + per_page
    return paginated_json(namespaces[start:end], request, page, per_page, total)


@router.get("/namespaces/{namespace_ref:path}")
async def get_namespace(
    namespace_ref: str,
    db: DbSession,
    current_user: CurrentUser,
):
    """Get a namespace by numeric ID or full path."""
    decoded_ref = unquote(namespace_ref).strip("/")
    if not decoded_ref:
        raise HTTPException(status_code=404, detail="404 Namespace Not Found")

    if decoded_ref.isdigit():
        namespace_id = int(decoded_ref)
        group = (
            await db.execute(select(Group).where(Group.id == namespace_id))
        ).scalar_one_or_none()
        if group is not None:
            visible_group_ids = await _visible_group_ids(db, current_user)
            if visible_group_ids is None or group.id in visible_group_ids:
                parent_ids = await _parent_ids_for_groups([group], db)
                parent_path = (
                    group.login.rsplit("/", 1)[0] if "/" in group.login else None
                )
                return _group_namespace_json(
                    group,
                    parent_ids.get(parent_path) if parent_path else None,
                )
        user = (
            await db.execute(select(User).where(User.id == namespace_id))
        ).scalar_one_or_none()
        if user is not None:
            return _user_namespace_json(user)
        group = None
    else:
        user = (
            await db.execute(select(User).where(User.login == decoded_ref))
        ).scalar_one_or_none()
        if user is not None:
            return _user_namespace_json(user)
        group = (
            await db.execute(select(Group).where(Group.login == decoded_ref))
        ).scalar_one_or_none()

    if group is None:
        raise HTTPException(status_code=404, detail="404 Namespace Not Found")
    visible_group_ids = await _visible_group_ids(db, current_user)
    if visible_group_ids is not None and group.id not in visible_group_ids:
        raise HTTPException(status_code=404, detail="404 Namespace Not Found")
    parent_ids = await _parent_ids_for_groups([group], db)
    parent_path = group.login.rsplit("/", 1)[0] if "/" in group.login else None
    return _group_namespace_json(
        group,
        parent_ids.get(parent_path) if parent_path else None,
    )
