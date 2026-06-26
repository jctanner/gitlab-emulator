"""GitLab group endpoints backed by the existing Group model."""

from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.sql import Select

from app.api.deps import AuthUser, CurrentUser, DbSession
from app.api.pagination import paginated_json
from app.config import settings
from app.models.group import Group
from app.models.organization import OrgMembership
from app.models.project import Project
from app.models.user import User
from app.schemas.user import SimpleUser
from app.schemas.user import _fmt_dt

router = APIRouter(tags=["groups"])


async def _get_group_or_404(group_ref: str, db: DbSession) -> Group:
    decoded_ref = unquote(group_ref).strip("/")
    if decoded_ref.isdigit():
        condition = Group.id == int(decoded_ref)
    else:
        condition = Group.login == decoded_ref
    result = await db.execute(select(Group).where(condition))
    group = result.scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="404 Group Not Found")
    return group


def _group_json(
    group: Group,
    base_url: str,
    parent_id: int | None = None,
) -> dict:
    api = f"{base_url}/api/v4"
    parent_path = group.login.rsplit("/", 1)[0] if "/" in group.login else None
    path = group.login.rsplit("/", 1)[-1]
    parent_name = None
    return {
        "id": group.id,
        "web_url": f"{base_url}/groups/{group.login}",
        "name": group.name or group.login,
        "path": path,
        "description": group.description or "",
        "visibility": "private",
        "share_with_group_lock": False,
        "require_two_factor_authentication": False,
        "two_factor_grace_period": 48,
        "project_creation_level": "developer",
        "auto_devops_enabled": None,
        "subgroup_creation_level": "maintainer",
        "emails_disabled": None,
        "mentions_disabled": None,
        "lfs_enabled": True,
        "default_branch_protection": 2,
        "avatar_url": group.avatar_url,
        "request_access_enabled": False,
        "full_name": group.name or group.login,
        "full_path": group.login,
        "created_at": _fmt_dt(group.created_at),
        "updated_at": _fmt_dt(group.updated_at),
        "parent_id": parent_id,
        "parent_full_path": parent_path,
        "parent_name": parent_name,
        "organization_id": group.id,
        "projects": [],
        "shared_projects": [],
        "runners_token": None,
        "shared_runners_setting": "enabled",
        "wiki_access_level": "enabled",
        "duo_features_enabled": False,
        "lock_duo_features_enabled": False,
        "membership_lock": False,
        "prevent_forking_outside_group": False,
        "marked_for_deletion_on": None,
        "repository_storage": "default",
        "enabled_git_access_protocol": None,
        "emails_enabled": True,
        "default_branch": None,
        "default_branch_protection_defaults": {
            "allowed_to_push": [{"access_level": 40}],
            "allow_force_push": False,
            "allowed_to_merge": [{"access_level": 40}],
        },
        "_links": {
            "self": f"{api}/groups/{group.id}",
            "projects": f"{api}/groups/{group.id}/projects",
            "hooks": f"{api}/groups/{group.id}/hooks",
        },
    }


async def _parent_ids_for_groups(groups: list[Group], db: DbSession) -> dict[str, int]:
    parent_paths = {
        group.login.rsplit("/", 1)[0]
        for group in groups
        if "/" in group.login
    }
    if not parent_paths:
        return {}
    result = await db.execute(select(Group).where(Group.login.in_(parent_paths)))
    return {parent.login: parent.id for parent in result.scalars().all()}


def _group_json_with_parent(
    group: Group,
    base_url: str,
    parent_ids: dict[str, int],
) -> dict:
    parent_path = group.login.rsplit("/", 1)[0] if "/" in group.login else None
    return _group_json(
        group,
        base_url,
        parent_ids.get(parent_path) if parent_path else None,
    )


def _ordered_groups_query(query: Select, order_by: str, sort: str) -> Select:
    order_columns = {
        "id": Group.id,
        "name": Group.name,
        "path": Group.login,
        "created_at": Group.created_at,
        "updated_at": Group.updated_at,
    }
    column = order_columns.get(order_by, Group.login)
    direction = column.desc() if sort == "desc" else column.asc()
    return query.order_by(direction, Group.id.asc())


def _group_access_level(role: str) -> int:
    return 50 if role == "admin" else 30


def _group_role(access_level: int) -> str:
    return "admin" if access_level >= 50 else "member"


def _member_json(
    user: User,
    access_level: int,
    base_url: str,
    *,
    created_at=None,
) -> dict:
    simple = SimpleUser.from_db(user, base_url).model_dump()
    return {
        "id": user.id,
        "username": user.login,
        "name": user.name or user.login,
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


@router.post("/groups", status_code=201)
async def create_group(body: dict, user: AuthUser, db: DbSession):
    """Create a GitLab group as an organization-backed namespace."""
    path = str(body.get("path") or "").strip("/")
    name = body.get("name") or path
    if not path:
        raise HTTPException(status_code=422, detail="path is required")
    parent_id = body.get("parent_id")
    resolved_parent_id = None
    full_path = path
    if parent_id is not None:
        parent = await _get_group_or_404(str(parent_id), db)
        resolved_parent_id = parent.id
        full_path = f"{parent.login}/{path}"

    existing = await db.execute(select(Group).where(Group.login == full_path))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=400, detail="Group already exists")

    group = Group(
        login=full_path,
        name=name,
        description=body.get("description"),
        avatar_url=body.get("avatar_url"),
    )
    db.add(group)
    await db.flush()
    db.add(
        OrgMembership(
            org_id=group.id,
            user_id=user.id,
            role="admin",
            state="active",
        )
    )
    await db.commit()
    await db.refresh(group)
    return _group_json(group, settings.BASE_URL, resolved_parent_id)


@router.get("/groups")
async def list_groups(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    search: str | None = Query(None),
    top_level_only: bool = Query(False),
    skip_groups: list[int] | None = Query(None),
    owned: bool = Query(False),
    min_access_level: int | None = Query(None),
    all_available: bool = Query(False),
    order_by: str = Query("path"),
    sort: str = Query("asc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List GitLab groups."""
    query = select(Group)
    if search:
        query = query.where(
            Group.login.ilike(f"%{search}%")
            | Group.name.ilike(f"%{search}%")
        )
    if top_level_only:
        query = query.where(~Group.login.contains("/"))
    if skip_groups:
        query = query.where(Group.id.not_in(skip_groups))
    if owned or min_access_level is not None:
        if current_user is None:
            query = query.where(False)
        else:
            query = query.join(OrgMembership, OrgMembership.org_id == Group.id).where(
                OrgMembership.user_id == current_user.id,
                OrgMembership.state == "active",
            )
            if owned:
                query = query.where(OrgMembership.role == "admin")
            if min_access_level is not None:
                if min_access_level >= 50:
                    query = query.where(OrgMembership.role == "admin")
                elif min_access_level > 30:
                    query = query.where(False)
    # ``all_available`` is accepted for GitLab client compatibility. This
    # emulator does not model private group visibility beyond membership yet.
    _ = all_available
    query = _ordered_groups_query(query, order_by, sort)
    total = (
        await db.execute(select(sa_func.count()).select_from(query.subquery()))
    ).scalar() or 0
    query = query.offset((page - 1) * per_page).limit(per_page)
    groups = (await db.execute(query)).scalars().all()
    parent_ids = await _parent_ids_for_groups(groups, db)
    return paginated_json(
        [
            _group_json_with_parent(group, settings.BASE_URL, parent_ids)
            for group in groups
        ],
        request,
        page,
        per_page,
        total,
    )


@router.get("/groups/{group_ref:path}/projects")
async def list_group_projects(
    group_ref: str,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List projects in a GitLab group namespace."""
    group = await _get_group_or_404(group_ref, db)
    query = (
        select(Project)
        .where(
            Project.owner_type == "Organization",
            Project.full_name.like(f"{group.login}/%"),
        )
        .order_by(Project.id)
    )
    total = (
        await db.execute(select(sa_func.count()).select_from(query.subquery()))
    ).scalar() or 0
    query = query.offset((page - 1) * per_page).limit(per_page)
    projects = (await db.execute(query)).scalars().all()

    from app.api.projects import _project_json

    return paginated_json(
        [await _project_json(project, settings.BASE_URL, db) for project in projects],
        request,
        page,
        per_page,
        total,
    )


@router.get("/groups/{group_ref:path}/members")
async def list_group_members(
    group_ref: str,
    request: Request,
    db: DbSession,
    user: AuthUser,
    query: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List GitLab-shaped group members."""
    group = await _get_group_or_404(group_ref, db)
    result = await db.execute(
        select(OrgMembership)
        .where(OrgMembership.org_id == group.id)
        .order_by(OrgMembership.user_id)
    )
    members = [
        _member_json(
            member.user,
            _group_access_level(member.role),
            settings.BASE_URL,
            created_at=member.created_at,
        )
        for member in result.scalars().all()
        if member.user
    ]
    if query:
        lowered = query.lower()
        members = [
            member
            for member in members
            if lowered in member["username"].lower() or lowered in member["name"].lower()
        ]
    total = len(members)
    start = (page - 1) * per_page
    return paginated_json(
        members[start:start + per_page],
        request,
        page,
        per_page,
        total,
    )


@router.get("/groups/{group_ref:path}/members/all")
async def list_all_group_members(
    group_ref: str,
    request: Request,
    db: DbSession,
    user: AuthUser,
    query: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List all GitLab-shaped group members.

    The emulator has no inherited group membership model, so this currently
    matches direct members while preserving the GitLab route common clients use.
    """
    return await list_group_members(
        group_ref,
        request,
        db,
        user,
        query=query,
        page=page,
        per_page=per_page,
    )


@router.get("/groups/{group_ref:path}/members/{user_id}")
async def get_group_member(
    group_ref: str,
    user_id: int,
    db: DbSession,
    user: AuthUser,
):
    """Get one GitLab-shaped group member."""
    group = await _get_group_or_404(group_ref, db)
    result = await db.execute(
        select(OrgMembership).where(
            OrgMembership.org_id == group.id,
            OrgMembership.user_id == user_id,
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None or membership.user is None:
        raise HTTPException(status_code=404, detail="Member Not Found")
    return _member_json(
        membership.user,
        _group_access_level(membership.role),
        settings.BASE_URL,
        created_at=membership.created_at,
    )


@router.post("/groups/{group_ref:path}/members", status_code=201)
async def add_group_member(
    group_ref: str,
    body: dict,
    db: DbSession,
    user: AuthUser,
):
    """Add or update a GitLab-shaped group member."""
    group = await _get_group_or_404(group_ref, db)
    user_id = body.get("user_id")
    if user_id is None:
        raise HTTPException(status_code=422, detail="user_id is required")
    result = await db.execute(select(User).where(User.id == int(user_id)))
    target_user = result.scalar_one_or_none()
    if target_user is None:
        raise HTTPException(status_code=404, detail="User Not Found")
    access_level = int(body.get("access_level") or 30)
    role = _group_role(access_level)
    existing = await db.execute(
        select(OrgMembership).where(
            OrgMembership.org_id == group.id,
            OrgMembership.user_id == target_user.id,
        )
    )
    membership = existing.scalar_one_or_none()
    if membership:
        if membership.state == "active" and membership.role == role:
            raise HTTPException(status_code=409, detail="Member already exists")
        membership.role = role
        membership.state = "active"
    else:
        db.add(
            OrgMembership(
                org_id=group.id,
                user_id=target_user.id,
                role=role,
                state="active",
            )
        )
    await db.commit()
    return _member_json(target_user, _group_access_level(role), settings.BASE_URL)


@router.delete("/groups/{group_ref:path}/members/{user_id}", status_code=204)
async def delete_group_member(
    group_ref: str,
    user_id: int,
    db: DbSession,
    user: AuthUser,
):
    """Remove a GitLab-shaped group member."""
    group = await _get_group_or_404(group_ref, db)
    result = await db.execute(
        select(OrgMembership).where(
            OrgMembership.org_id == group.id,
            OrgMembership.user_id == user_id,
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=404, detail="Member Not Found")
    await db.delete(membership)
    await db.commit()
    return Response(status_code=204)


@router.get("/groups/{group_ref:path}")
async def get_group(group_ref: str, db: DbSession, current_user: CurrentUser):
    """Get a GitLab group by numeric ID or URL-encoded full path."""
    group = await _get_group_or_404(group_ref, db)
    parent_id = None
    if "/" in group.login:
        parent_path = group.login.rsplit("/", 1)[0]
        parent_result = await db.execute(
            select(Group).where(Group.login == parent_path)
        )
        parent = parent_result.scalar_one_or_none()
        parent_id = parent.id if parent else None
    return _group_json(group, settings.BASE_URL, parent_id)
