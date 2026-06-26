"""CI/CD variable resolution helpers."""

from sqlalchemy import select

from app.api.deps import DbSession
from app.models.ci import CiVariable
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


async def project_variable_entries(project: Project, db: DbSession) -> dict[str, dict]:
    """Return MVP project variables eligible for all project jobs.

    Environment-scope matching is a later slice. Until job environments are
    modeled, only the default `*` scope participates in runner payloads.
    """
    result = await db.execute(
        select(CiVariable)
        .where(
            CiVariable.scope_type == "project",
            CiVariable.scope_id == project.id,
            CiVariable.environment_scope == "*",
        )
        .order_by(CiVariable.key.asc())
    )
    return {
        variable.key: ci_variable_entry(variable)
        for variable in result.scalars().all()
    }
