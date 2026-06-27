"""GitLab group endpoints backed by the existing Group model."""

import re
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql import Select

from app.api.deps import AuthUser, CurrentUser, DbSession
from app.api.pagination import paginated_json
from app.config import settings
from app.models.ci import CiSecret, CiVariable
from app.models.group import Group
from app.models.organization import OrgMembership
from app.models.project import Project
from app.models.user import User
from app.schemas.user import SimpleUser
from app.schemas.user import _fmt_dt
from app.services.permissions import (
    MAINTAINER,
    OWNER,
    access_level_for_role,
    group_role_for_access_level,
    require_group_access,
)

router = APIRouter(tags=["groups"])
VARIABLE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
VARIABLE_TYPES = {"env_var", "file"}


class GroupVariableCreate(BaseModel):
    key: str
    value: str
    variable_type: str = "env_var"
    protected: bool = False
    masked: bool = False
    hidden: bool = False
    raw: bool = False
    environment_scope: str = "*"
    description: str | None = None


class GroupVariableUpdate(BaseModel):
    value: str | None = None
    variable_type: str | None = None
    protected: bool | None = None
    masked: bool | None = None
    hidden: bool | None = None
    raw: bool | None = None
    environment_scope: str | None = None
    description: str | None = None


class GroupSecretCreate(BaseModel):
    name: str
    value: str
    description: str | None = None
    environment_scope: str = "*"
    branch_scope: str = "*"
    protected: bool = False
    rotation_reminder_days: int | None = None
    status: str = "healthy"


class GroupSecretUpdate(BaseModel):
    value: str | None = None
    description: str | None = None
    environment_scope: str | None = None
    branch_scope: str | None = None
    protected: bool | None = None
    rotation_reminder_days: int | None = None
    status: str | None = None


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


async def _require_group_owner(group: Group, user: User, db: DbSession) -> None:
    await require_group_access(group, user, db, OWNER)


async def _require_group_maintainer(group: Group, user: User, db: DbSession) -> None:
    await require_group_access(group, user, db, MAINTAINER)


def _validate_variable_key(key: str) -> str:
    normalized = str(key or "").strip()
    if not VARIABLE_KEY_RE.match(normalized):
        raise HTTPException(status_code=400, detail="Invalid variable key")
    return normalized


def _validate_variable_type(variable_type: str) -> str:
    normalized = str(variable_type or "env_var")
    if normalized not in VARIABLE_TYPES:
        raise HTTPException(status_code=400, detail="Invalid variable_type")
    return normalized


def _validate_secret_name(name: str) -> str:
    normalized = str(name or "").strip()
    if not VARIABLE_KEY_RE.match(normalized):
        raise HTTPException(status_code=400, detail="Invalid secret name")
    return normalized


def _variable_visibility(masked: bool, hidden: bool) -> str:
    if hidden:
        return "masked_and_hidden"
    if masked:
        return "masked"
    return "visible"


def _variable_json(variable: CiVariable) -> dict:
    hidden = variable.visibility == "masked_and_hidden"
    masked = variable.visibility in {"masked", "masked_and_hidden"}
    return {
        "key": variable.key,
        "variable_type": variable.variable_type,
        "value": None if hidden else variable.value,
        "protected": variable.protected,
        "masked": masked,
        "hidden": hidden,
        "raw": variable.raw,
        "environment_scope": variable.environment_scope,
        "description": variable.description,
    }


def _secret_json(secret: CiSecret) -> dict:
    return {
        "name": secret.name,
        "value": None,
        "description": secret.description,
        "environment_scope": secret.environment_scope,
        "branch_scope": secret.branch_scope,
        "protected": secret.protected,
        "rotation_reminder_days": secret.rotation_reminder_days,
        "status": secret.status,
        "last_accessed_at": _fmt_dt(secret.last_accessed_at),
        "last_accessed_by_job_id": secret.last_accessed_by_job_id,
        "created_at": _fmt_dt(secret.created_at),
        "updated_at": _fmt_dt(secret.updated_at),
    }


async def _get_group_secret_or_404(
    group: Group,
    db: DbSession,
    name: str,
    environment_scope: str | None = None,
    branch_scope: str | None = None,
) -> CiSecret:
    query = select(CiSecret).where(
        CiSecret.scope_type == "group",
        CiSecret.scope_id == group.id,
        CiSecret.name == _validate_secret_name(name),
        CiSecret.environment_scope == (environment_scope if environment_scope is not None else "*"),
        CiSecret.branch_scope == (branch_scope if branch_scope is not None else "*"),
    )
    secret = (await db.execute(query)).scalar_one_or_none()
    if secret is None:
        raise HTTPException(status_code=404, detail="404 Secret Not Found")
    return secret


async def _get_group_variable_or_404(
    group: Group,
    db: DbSession,
    key: str,
    environment_scope: str | None = None,
) -> CiVariable:
    query = select(CiVariable).where(
        CiVariable.scope_type == "group",
        CiVariable.scope_id == group.id,
        CiVariable.key == _validate_variable_key(key),
    )
    if environment_scope is not None:
        query = query.where(CiVariable.environment_scope == environment_scope)
    else:
        query = query.where(CiVariable.environment_scope == "*")
    variable = (await db.execute(query)).scalar_one_or_none()
    if variable is None:
        raise HTTPException(status_code=404, detail="404 Variable Not Found")
    return variable


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
    return access_level_for_role(role)


def _group_role(access_level: int) -> str:
    return group_role_for_access_level(access_level)


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
                eligible_roles = [
                    role
                    for role in {
                        "guest",
                        "reporter",
                        "member",
                        "developer",
                        "maintainer",
                        "admin",
                        "owner",
                    }
                    if access_level_for_role(role) >= min_access_level
                ]
                query = query.where(OrgMembership.role.in_(eligible_roles))
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


@router.get("/groups/{group_ref:path}/variables")
async def list_group_variables(
    group_ref: str,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
):
    """List group CI/CD variables."""
    group = await _get_group_or_404(group_ref, db)
    await _require_group_maintainer(group, user, db)
    query = select(CiVariable).where(
        CiVariable.scope_type == "group",
        CiVariable.scope_id == group.id,
    )
    if environment_scope is not None:
        query = query.where(CiVariable.environment_scope == environment_scope)
    query = query.order_by(CiVariable.key, CiVariable.environment_scope)
    variables = (await db.execute(query)).scalars().all()
    return [_variable_json(variable) for variable in variables]


@router.post("/groups/{group_ref:path}/variables", status_code=201)
async def create_group_variable(
    group_ref: str,
    body: GroupVariableCreate,
    user: AuthUser,
    db: DbSession,
):
    """Create a group CI/CD variable."""
    group = await _get_group_or_404(group_ref, db)
    await _require_group_maintainer(group, user, db)
    variable = CiVariable(
        scope_type="group",
        scope_id=group.id,
        key=_validate_variable_key(body.key),
        value=body.value,
        variable_type=_validate_variable_type(body.variable_type),
        visibility=_variable_visibility(body.masked, body.hidden),
        protected=body.protected,
        raw=body.raw,
        environment_scope=body.environment_scope or "*",
        description=body.description,
    )
    db.add(variable)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Variable already exists") from exc
    await db.refresh(variable)
    return _variable_json(variable)


@router.get("/groups/{group_ref:path}/variables/{key}")
async def get_group_variable(
    group_ref: str,
    key: str,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
):
    """Get a group CI/CD variable by key."""
    group = await _get_group_or_404(group_ref, db)
    await _require_group_maintainer(group, user, db)
    variable = await _get_group_variable_or_404(group, db, key, environment_scope)
    return _variable_json(variable)


@router.put("/groups/{group_ref:path}/variables/{key}")
async def update_group_variable(
    group_ref: str,
    key: str,
    body: GroupVariableUpdate,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
):
    """Update a group CI/CD variable."""
    group = await _get_group_or_404(group_ref, db)
    await _require_group_maintainer(group, user, db)
    variable = await _get_group_variable_or_404(group, db, key, environment_scope)
    if body.value is not None:
        variable.value = body.value
    if body.variable_type is not None:
        variable.variable_type = _validate_variable_type(body.variable_type)
    if body.protected is not None:
        variable.protected = body.protected
    if body.raw is not None:
        variable.raw = body.raw
    if body.environment_scope is not None:
        variable.environment_scope = body.environment_scope or "*"
    if body.description is not None:
        variable.description = body.description
    if body.masked is not None or body.hidden is not None:
        current_hidden = variable.visibility == "masked_and_hidden"
        current_masked = variable.visibility in {"masked", "masked_and_hidden"}
        variable.visibility = _variable_visibility(
            body.masked if body.masked is not None else current_masked,
            body.hidden if body.hidden is not None else current_hidden,
        )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Variable already exists") from exc
    await db.refresh(variable)
    return _variable_json(variable)


@router.delete("/groups/{group_ref:path}/variables/{key}", status_code=204)
async def delete_group_variable(
    group_ref: str,
    key: str,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
):
    """Delete a group CI/CD variable."""
    group = await _get_group_or_404(group_ref, db)
    await _require_group_maintainer(group, user, db)
    variable = await _get_group_variable_or_404(group, db, key, environment_scope)
    await db.delete(variable)
    await db.commit()
    return Response(status_code=204)


@router.get("/groups/{group_ref:path}/secrets")
async def list_group_secrets(
    group_ref: str,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
    branch_scope: str | None = Query(None, alias="filter[branch_scope]"),
):
    """List group CI/CD secrets."""
    group = await _get_group_or_404(group_ref, db)
    await _require_group_maintainer(group, user, db)
    query = select(CiSecret).where(
        CiSecret.scope_type == "group",
        CiSecret.scope_id == group.id,
    )
    if environment_scope is not None:
        query = query.where(CiSecret.environment_scope == environment_scope)
    if branch_scope is not None:
        query = query.where(CiSecret.branch_scope == branch_scope)
    query = query.order_by(CiSecret.name, CiSecret.environment_scope, CiSecret.branch_scope)
    secrets = (await db.execute(query)).scalars().all()
    return [_secret_json(secret) for secret in secrets]


@router.post("/groups/{group_ref:path}/secrets", status_code=201)
async def create_group_secret(
    group_ref: str,
    body: GroupSecretCreate,
    user: AuthUser,
    db: DbSession,
):
    """Create a group CI/CD secret."""
    group = await _get_group_or_404(group_ref, db)
    await _require_group_maintainer(group, user, db)
    secret = CiSecret(
        scope_type="group",
        scope_id=group.id,
        name=_validate_secret_name(body.name),
        value=body.value,
        description=body.description,
        environment_scope=body.environment_scope or "*",
        branch_scope=body.branch_scope or "*",
        protected=body.protected,
        rotation_reminder_days=body.rotation_reminder_days,
        status=body.status or "healthy",
    )
    db.add(secret)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Secret already exists") from exc
    await db.refresh(secret)
    return _secret_json(secret)


@router.get("/groups/{group_ref:path}/secrets/{name}")
async def get_group_secret(
    group_ref: str,
    name: str,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
    branch_scope: str | None = Query(None, alias="filter[branch_scope]"),
):
    """Get a group CI/CD secret by name."""
    group = await _get_group_or_404(group_ref, db)
    await _require_group_maintainer(group, user, db)
    secret = await _get_group_secret_or_404(
        group,
        db,
        name,
        environment_scope,
        branch_scope,
    )
    return _secret_json(secret)


@router.put("/groups/{group_ref:path}/secrets/{name}")
async def update_group_secret(
    group_ref: str,
    name: str,
    body: GroupSecretUpdate,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
    branch_scope: str | None = Query(None, alias="filter[branch_scope]"),
):
    """Update a group CI/CD secret."""
    group = await _get_group_or_404(group_ref, db)
    await _require_group_maintainer(group, user, db)
    secret = await _get_group_secret_or_404(
        group,
        db,
        name,
        environment_scope,
        branch_scope,
    )
    if body.value is not None:
        secret.value = body.value
    if body.description is not None:
        secret.description = body.description
    if body.environment_scope is not None:
        secret.environment_scope = body.environment_scope or "*"
    if body.branch_scope is not None:
        secret.branch_scope = body.branch_scope or "*"
    if body.protected is not None:
        secret.protected = body.protected
    if body.rotation_reminder_days is not None:
        secret.rotation_reminder_days = body.rotation_reminder_days
    if body.status is not None:
        secret.status = body.status or "healthy"
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Secret already exists") from exc
    await db.refresh(secret)
    return _secret_json(secret)


@router.delete("/groups/{group_ref:path}/secrets/{name}", status_code=204)
async def delete_group_secret(
    group_ref: str,
    name: str,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
    branch_scope: str | None = Query(None, alias="filter[branch_scope]"),
):
    """Delete a group CI/CD secret."""
    group = await _get_group_or_404(group_ref, db)
    await _require_group_maintainer(group, user, db)
    secret = await _get_group_secret_or_404(
        group,
        db,
        name,
        environment_scope,
        branch_scope,
    )
    await db.delete(secret)
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
