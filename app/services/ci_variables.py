"""CI/CD variable resolution helpers."""

import fnmatch

from sqlalchemy import select

from app.api.deps import DbSession
from app.models.branch import Branch
from app.models.ci import CiVariable
from app.models.group import Group
from app.models.project import Project


def ci_variable_entry(variable: CiVariable) -> dict:
    masked = variable.visibility in {"masked", "masked_and_hidden"}
    return {
        "value": variable.value,
        "file": variable.variable_type == "file",
        "masked": masked,
        "raw": variable.raw,
        "public": not masked,
    }


def _environment_matches(scope: str, environment: str | None) -> bool:
    if scope == "*":
        return True
    if not environment:
        return False
    return fnmatch.fnmatchcase(environment, scope)


def _scope_specificity(scope: str) -> tuple[int, int]:
    if scope == "*":
        return (0, 0)
    wildcard_count = scope.count("*") + scope.count("?")
    return (1, len(scope) - wildcard_count)


async def project_ref_is_protected(project: Project, ref: str, db: DbSession) -> bool:
    result = await db.execute(
        select(Branch.protected).where(
            Branch.repo_id == project.id,
            Branch.name == ref,
            Branch.protected.is_(True),
        )
    )
    return bool(result.scalar_one_or_none())


def _project_group_paths(project: Project) -> list[str]:
    if project.owner_type != "Organization" or "/" not in project.full_name:
        return []
    namespace_path = project.full_name.rsplit("/", 1)[0]
    parts = namespace_path.split("/")
    return ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]


async def _project_group_ids(project: Project, db: DbSession) -> list[int]:
    paths = _project_group_paths(project)
    if not paths:
        return []
    result = await db.execute(select(Group).where(Group.login.in_(paths)))
    ids_by_path = {group.login: group.id for group in result.scalars().all()}
    return [ids_by_path[path] for path in paths if path in ids_by_path]


async def _variables_for_scope(
    db: DbSession,
    *,
    scope_type: str,
    scope_id: int | None,
    protected_ref: bool,
    environment: str | None,
) -> dict[str, dict]:
    query = select(CiVariable).where(CiVariable.scope_type == scope_type)
    if scope_id is None:
        query = query.where(CiVariable.scope_id.is_(None))
    else:
        query = query.where(CiVariable.scope_id == scope_id)
    result = await db.execute(
        query.order_by(CiVariable.key.asc(), CiVariable.environment_scope.asc())
    )
    selected: dict[str, tuple[tuple[int, int], CiVariable]] = {}
    for variable in result.scalars().all():
        if variable.protected and not protected_ref:
            continue
        if not _environment_matches(variable.environment_scope or "*", environment):
            continue
        specificity = _scope_specificity(variable.environment_scope or "*")
        existing = selected.get(variable.key)
        if existing is None or specificity >= existing[0]:
            selected[variable.key] = (specificity, variable)
    return {
        key: ci_variable_entry(variable)
        for key, (_specificity, variable) in selected.items()
    }


async def project_variable_entries(
    project: Project,
    db: DbSession,
    *,
    ref: str,
    environment: str | None = None,
) -> dict[str, dict]:
    """Return project variables eligible for a job on a ref/environment."""
    protected_ref = await project_ref_is_protected(project, ref, db)
    entries = await _variables_for_scope(
        db,
        scope_type="instance",
        scope_id=None,
        protected_ref=protected_ref,
        environment=environment,
    )
    for group_id in await _project_group_ids(project, db):
        entries.update(
            await _variables_for_scope(
                db,
                scope_type="group",
                scope_id=group_id,
                protected_ref=protected_ref,
                environment=environment,
            )
        )
    entries.update(
        await _variables_for_scope(
            db,
            scope_type="project",
            scope_id=project.id,
            protected_ref=protected_ref,
            environment=environment,
        )
    )
    return entries
