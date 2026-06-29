"""Protected branch authorization helpers."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.branch import Branch
from app.services.permissions import project_access_level


def minimum_push_access_level(branch: Branch) -> int:
    restrictions = branch.protection.restrictions if branch.protection else {}
    entries = (restrictions or {}).get("push_access_levels") or [{"access_level": 40}]
    levels = [
        int(entry.get("access_level", 40))
        for entry in entries
        if isinstance(entry, dict) and int(entry.get("access_level", 40)) > 0
    ]
    return min(levels, default=40)


async def require_branch_push_access(
    project: Any,
    branch_name: str,
    user: Any,
    db: Any,
) -> None:
    result = await db.execute(
        select(Branch)
        .options(selectinload(Branch.protection))
        .where(
            Branch.repo_id == project.id,
            Branch.name == branch_name,
            Branch.protected.is_(True),
        )
    )
    branch = result.scalar_one_or_none()
    if branch is None:
        return

    if await project_access_level(project, user, db) < minimum_push_access_level(branch):
        raise HTTPException(
            status_code=403,
            detail=f"You are not allowed to push to protected branch '{branch_name}'",
        )
