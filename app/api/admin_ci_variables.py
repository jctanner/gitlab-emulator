"""Admin-only instance CI/CD variable endpoints."""

import re

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import AuthUser, DbSession
from app.models.ci import CiVariable

router = APIRouter(tags=["admin-ci-variables"])

VARIABLE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
VARIABLE_TYPES = {"env_var", "file"}


class InstanceVariableCreate(BaseModel):
    key: str
    value: str
    variable_type: str = "env_var"
    protected: bool = False
    masked: bool = False
    hidden: bool = False
    raw: bool = False
    environment_scope: str = "*"
    description: str | None = None


class InstanceVariableUpdate(BaseModel):
    value: str | None = None
    variable_type: str | None = None
    protected: bool | None = None
    masked: bool | None = None
    hidden: bool | None = None
    raw: bool | None = None
    environment_scope: str | None = None
    description: str | None = None


def _require_admin(user) -> None:
    if not user.site_admin:
        raise HTTPException(status_code=403, detail="Forbidden")


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


async def _get_instance_variable_or_404(
    db: DbSession,
    key: str,
    environment_scope: str | None = None,
) -> CiVariable:
    query = select(CiVariable).where(
        CiVariable.scope_type == "instance",
        CiVariable.scope_id.is_(None),
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


async def _ensure_no_instance_variable(
    db: DbSession,
    key: str,
    environment_scope: str,
    exclude_id: int | None = None,
) -> None:
    query = select(CiVariable).where(
        CiVariable.scope_type == "instance",
        CiVariable.scope_id.is_(None),
        CiVariable.key == key,
        CiVariable.environment_scope == environment_scope,
    )
    if exclude_id is not None:
        query = query.where(CiVariable.id != exclude_id)
    existing = (await db.execute(query)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=400, detail="Variable already exists")


@router.get("/admin/ci/variables")
async def list_instance_variables(
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
):
    """List instance CI/CD variables."""
    _require_admin(user)
    query = select(CiVariable).where(
        CiVariable.scope_type == "instance",
        CiVariable.scope_id.is_(None),
    )
    if environment_scope is not None:
        query = query.where(CiVariable.environment_scope == environment_scope)
    query = query.order_by(CiVariable.key, CiVariable.environment_scope)
    variables = (await db.execute(query)).scalars().all()
    return [_variable_json(variable) for variable in variables]


@router.post("/admin/ci/variables", status_code=201)
async def create_instance_variable(
    body: InstanceVariableCreate,
    user: AuthUser,
    db: DbSession,
):
    """Create an instance CI/CD variable."""
    _require_admin(user)
    key = _validate_variable_key(body.key)
    variable_type = _validate_variable_type(body.variable_type)
    environment_scope = body.environment_scope or "*"
    await _ensure_no_instance_variable(db, key, environment_scope)

    variable = CiVariable(
        scope_type="instance",
        scope_id=None,
        key=key,
        value=body.value,
        variable_type=variable_type,
        visibility=_variable_visibility(body.masked, body.hidden),
        protected=body.protected,
        raw=body.raw,
        environment_scope=environment_scope,
        description=body.description,
    )
    db.add(variable)
    await db.commit()
    await db.refresh(variable)
    return _variable_json(variable)


@router.get("/admin/ci/variables/{key}")
async def get_instance_variable(
    key: str,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
):
    """Get an instance CI/CD variable."""
    _require_admin(user)
    variable = await _get_instance_variable_or_404(db, key, environment_scope)
    return _variable_json(variable)


@router.put("/admin/ci/variables/{key}")
async def update_instance_variable(
    key: str,
    body: InstanceVariableUpdate,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
):
    """Update an instance CI/CD variable."""
    _require_admin(user)
    variable = await _get_instance_variable_or_404(db, key, environment_scope)
    updates = body.model_dump(exclude_unset=True)

    if "variable_type" in updates and updates["variable_type"] is not None:
        variable.variable_type = _validate_variable_type(updates["variable_type"])
    if "value" in updates and updates["value"] is not None:
        variable.value = updates["value"]
    if "protected" in updates and updates["protected"] is not None:
        variable.protected = updates["protected"]
    if "raw" in updates and updates["raw"] is not None:
        variable.raw = updates["raw"]
    if "description" in updates:
        variable.description = updates["description"]
    if "environment_scope" in updates and updates["environment_scope"] is not None:
        next_scope = updates["environment_scope"] or "*"
        await _ensure_no_instance_variable(db, variable.key, next_scope, variable.id)
        variable.environment_scope = next_scope
    if "masked" in updates or "hidden" in updates:
        current_masked = variable.visibility in {"masked", "masked_and_hidden"}
        current_hidden = variable.visibility == "masked_and_hidden"
        variable.visibility = _variable_visibility(
            bool(updates.get("masked", current_masked)),
            bool(updates.get("hidden", current_hidden)),
        )

    await db.commit()
    await db.refresh(variable)
    return _variable_json(variable)


@router.delete("/admin/ci/variables/{key}", status_code=204)
async def delete_instance_variable(
    key: str,
    user: AuthUser,
    db: DbSession,
    environment_scope: str | None = Query(None, alias="filter[environment_scope]"),
):
    """Delete an instance CI/CD variable."""
    _require_admin(user)
    variable = await _get_instance_variable_or_404(db, key, environment_scope)
    await db.delete(variable)
    await db.commit()
    return Response(status_code=204)
