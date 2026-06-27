"""GitLab access level helpers over the emulator's existing membership tables."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from app.models.organization import OrgMembership, Organization
from app.models.repository import Collaborator

GUEST = 10
REPORTER = 20
DEVELOPER = 30
MAINTAINER = 40
OWNER = 50

ROLE_TO_ACCESS_LEVEL = {
    "guest": GUEST,
    "reporter": REPORTER,
    "member": DEVELOPER,
    "developer": DEVELOPER,
    "maintainer": MAINTAINER,
    "admin": OWNER,
    "owner": OWNER,
}

ACCESS_LEVEL_TO_GROUP_ROLE = {
    GUEST: "guest",
    REPORTER: "reporter",
    DEVELOPER: "developer",
    MAINTAINER: "maintainer",
    OWNER: "admin",
}

PERMISSION_TO_ACCESS_LEVEL = {
    "pull": REPORTER,
    "triage": REPORTER,
    "push": DEVELOPER,
    "maintain": MAINTAINER,
    "admin": OWNER,
}

ACCESS_LEVEL_TO_PERMISSION = {
    GUEST: "pull",
    REPORTER: "pull",
    DEVELOPER: "push",
    MAINTAINER: "maintain",
    OWNER: "admin",
}

PIPELINE_VARIABLE_POLICY_LEVELS = {
    "developer": DEVELOPER,
    "maintainer": MAINTAINER,
    "owner": OWNER,
}


def access_level_for_role(role: str | None) -> int:
    return ROLE_TO_ACCESS_LEVEL.get(str(role or "").lower(), GUEST)


def group_role_for_access_level(access_level: int) -> str:
    if access_level >= OWNER:
        return ACCESS_LEVEL_TO_GROUP_ROLE[OWNER]
    if access_level >= MAINTAINER:
        return ACCESS_LEVEL_TO_GROUP_ROLE[MAINTAINER]
    if access_level >= DEVELOPER:
        return ACCESS_LEVEL_TO_GROUP_ROLE[DEVELOPER]
    if access_level >= REPORTER:
        return ACCESS_LEVEL_TO_GROUP_ROLE[REPORTER]
    return ACCESS_LEVEL_TO_GROUP_ROLE[GUEST]


def permission_for_access_level(access_level: int) -> str:
    if access_level >= OWNER:
        return ACCESS_LEVEL_TO_PERMISSION[OWNER]
    if access_level >= MAINTAINER:
        return ACCESS_LEVEL_TO_PERMISSION[MAINTAINER]
    if access_level >= DEVELOPER:
        return ACCESS_LEVEL_TO_PERMISSION[DEVELOPER]
    return ACCESS_LEVEL_TO_PERMISSION[REPORTER]


def access_level_for_permission(permission: str | None) -> int:
    return PERMISSION_TO_ACCESS_LEVEL.get(str(permission or "").lower(), REPORTER)


def pipeline_variables_allowed_for_access_level(
    *,
    policy: str,
    access_level: int,
) -> bool:
    if policy == "no_one_allowed":
        return False
    required = PIPELINE_VARIABLE_POLICY_LEVELS.get(policy, DEVELOPER)
    return access_level >= required


async def group_access_level(group: Any, user: Any | None, db: Any) -> int:
    if user is None:
        return GUEST
    if getattr(user, "site_admin", False):
        return OWNER
    result = await db.execute(
        select(OrgMembership).where(
            OrgMembership.org_id == group.id,
            OrgMembership.user_id == user.id,
            OrgMembership.state == "active",
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        return GUEST
    return access_level_for_role(membership.role)


async def _group_namespace_access_level(namespace: str, user: Any, db: Any) -> int:
    parts = [part for part in namespace.split("/") if part]
    paths = [
        "/".join(parts[:index])
        for index in range(1, len(parts) + 1)
    ]
    if not paths:
        return GUEST
    result = await db.execute(
        select(OrgMembership)
        .join(Organization, Organization.id == OrgMembership.org_id)
        .where(
            Organization.login.in_(paths),
            OrgMembership.user_id == user.id,
            OrgMembership.state == "active",
        )
    )
    return max(
        (access_level_for_role(membership.role) for membership in result.scalars().all()),
        default=GUEST,
    )


async def project_access_level(project: Any, user: Any | None, db: Any) -> int:
    if user is None:
        return GUEST
    if getattr(user, "site_admin", False):
        return OWNER
    if getattr(project, "owner_id", None) == getattr(user, "id", None):
        return OWNER

    result = await db.execute(
        select(Collaborator).where(
            Collaborator.repo_id == project.id,
            Collaborator.user_id == user.id,
        )
    )
    collaborator = result.scalar_one_or_none()
    levels = [
        access_level_for_permission(collaborator.permission)
        if collaborator is not None
        else GUEST
    ]
    if getattr(project, "owner_type", None) == "Organization":
        namespace = str(getattr(project, "full_name", "")).rsplit("/", 1)[0]
        levels.append(await _group_namespace_access_level(namespace, user, db))
    return max(levels, default=GUEST)


async def require_project_access(
    project: Any,
    user: Any,
    db: Any,
    minimum_access_level: int,
) -> None:
    if await project_access_level(project, user, db) < minimum_access_level:
        raise HTTPException(status_code=403, detail="Forbidden")


async def require_group_access(
    group: Any,
    user: Any,
    db: Any,
    minimum_access_level: int,
) -> None:
    if await group_access_level(group, user, db) < minimum_access_level:
        raise HTTPException(status_code=403, detail="Forbidden")
