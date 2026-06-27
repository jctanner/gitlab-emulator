"""CI/CD secret resolution helpers."""

from dataclasses import dataclass
import fnmatch

from sqlalchemy import select

from app.api.deps import DbSession
from app.models.ci import CiSecret
from app.models.group import Group
from app.models.project import Project
from app.services.ci_variables import project_ref_is_protected


@dataclass(frozen=True)
class ResolvedJobSecret:
    variable_key: str
    secret: CiSecret
    file: bool


def _scope_matches(scope: str, value: str | None) -> bool:
    if scope == "*":
        return True
    if not value:
        return False
    return fnmatch.fnmatchcase(value, scope)


def _scope_specificity(scope: str) -> tuple[int, int]:
    if scope == "*":
        return (0, 0)
    wildcard_count = scope.count("*") + scope.count("?")
    return (1, len(scope) - wildcard_count)


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


async def _best_secret_for_scope(
    db: DbSession,
    *,
    scope_type: str,
    scope_id: int,
    name: str,
    protected_ref: bool,
    environment: str | None,
    ref: str,
) -> CiSecret | None:
    result = await db.execute(
        select(CiSecret)
        .where(
            CiSecret.scope_type == scope_type,
            CiSecret.scope_id == scope_id,
            CiSecret.name == name,
        )
        .order_by(CiSecret.environment_scope.asc(), CiSecret.branch_scope.asc())
    )
    selected: tuple[tuple[int, int, int, int], CiSecret] | None = None
    for secret in result.scalars().all():
        if secret.protected and not protected_ref:
            continue
        if not _scope_matches(secret.environment_scope or "*", environment):
            continue
        if not _scope_matches(secret.branch_scope or "*", ref):
            continue
        specificity = (
            *_scope_specificity(secret.environment_scope or "*"),
            *_scope_specificity(secret.branch_scope or "*"),
        )
        if selected is None or specificity >= selected[0]:
            selected = (specificity, secret)
    return selected[1] if selected else None


def ci_secret_variable_entry(secret: CiSecret, *, file: bool) -> dict:
    return {
        "value": secret.value,
        "file": file,
        "masked": True,
        "raw": True,
        "public": False,
    }


def ci_secret_metadata_entry(resolved: ResolvedJobSecret) -> dict:
    """Return non-sensitive metadata for UI/API job inspection."""
    return {
        "key": resolved.variable_key,
        "name": resolved.secret.name,
        "mode": "file" if resolved.file else "env",
        "file": resolved.file,
        "scope_type": resolved.secret.scope_type,
        "scope_id": resolved.secret.scope_id,
        "environment_scope": resolved.secret.environment_scope or "*",
        "branch_scope": resolved.secret.branch_scope or "*",
        "protected": bool(resolved.secret.protected),
    }


async def project_secret_entries(
    project: Project,
    db: DbSession,
    *,
    ref: str,
    environment: str | None,
    secrets: dict[str, dict],
) -> tuple[dict[str, dict], list[ResolvedJobSecret]]:
    """Return job secret variables eligible for a project/ref/environment."""
    if not secrets:
        return {}, []

    protected_ref = await project_ref_is_protected(project, ref, db)
    scopes = [
        *[("group", group_id) for group_id in await _project_group_ids(project, db)],
        ("project", project.id),
    ]
    entries: dict[str, dict] = {}
    resolved: list[ResolvedJobSecret] = []
    for variable_key, request in secrets.items():
        name = str(request.get("name") or variable_key)
        file = bool(request.get("file", True))
        match_environment = request.get("environment_scope", environment)
        match_ref = request.get("branch_scope", ref)
        secret: CiSecret | None = None
        for scope_type, scope_id in scopes:
            candidate = await _best_secret_for_scope(
                db,
                scope_type=scope_type,
                scope_id=scope_id,
                name=name,
                protected_ref=protected_ref,
                environment=match_environment,
                ref=match_ref,
            )
            if candidate is not None:
                secret = candidate
        if secret is None:
            raise ValueError(f"Secret {name} is missing or not eligible for this job")
        entries[variable_key] = ci_secret_variable_entry(secret, file=file)
        resolved.append(
            ResolvedJobSecret(
                variable_key=variable_key,
                secret=secret,
                file=file,
            )
        )
    return entries, resolved
