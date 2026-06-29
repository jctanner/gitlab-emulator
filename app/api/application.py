"""GitLab application-level settings endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select

from app.api.deps import AuthUser, DbSession
from app.config import settings
from app.models.ci import CiRunner, CiSecret, CiVariable, Pipeline, PipelineJob
from app.models.group import Group
from app.models.issue import Issue
from app.models.project import Project
from app.models.pull_request import PullRequest
from app.models.user import User

router = APIRouter(tags=["application"])


def _require_admin(user) -> None:
    if not user.site_admin:
        raise HTTPException(status_code=403, detail="Forbidden")


def _application_settings_json() -> dict:
    return {
        "id": 1,
        "default_branch_name": "main",
        "default_project_visibility": "private",
        "default_group_visibility": "private",
        "default_snippet_visibility": "private",
        "restricted_visibility_levels": [],
        "signup_enabled": False,
        "signin_enabled": True,
        "password_authentication_enabled_for_web": True,
        "password_authentication_enabled_for_git": True,
        "gravatar_enabled": False,
        "repository_checks_enabled": False,
        "shared_runners_enabled": True,
        "shared_runners_text": "Instance runners are available in the emulator.",
        "max_attachment_size": 10,
        "repository_size_limit": 0,
        "session_expire_delay": 10080,
        "import_sources": ["git", "gitlab_project"],
        "enabled_git_access_protocol": "",
        "git_two_factor_session_expiry": 15,
        "container_registry_token_expire_delay": 5,
        "auto_devops_enabled": False,
        "housekeeping_enabled": False,
        "elasticsearch_indexing": False,
        "elasticsearch_search": False,
        "email_author_in_body": False,
        "plantuml_enabled": False,
        "version_check_enabled": False,
        "terms": "",
        "external_auth_client_cert": None,
        "domain_allowlist": [],
        "domain_denylist_enabled": False,
        "domain_denylist": [],
        "home_page_url": settings.BASE_URL,
        "after_sign_out_path": settings.BASE_URL,
        "help_page_text": "GitLab Emulator",
        "help_page_hide_commercial_content": True,
        "performance_bar_allowed_group_id": None,
        "instance_administration_project_id": None,
        "diff_max_patch_bytes": 204800,
        "commit_email_hostname": None,
        "deletion_adjourned_period": 0,
        "package_registry_cleanup_policies_worker_capacity": 0,
        "ci_max_total_yaml_size_bytes": 1048576,
        "ci_max_includes": 150,
        "ci_jwt_signing_key": None,
        "rate_limiting_response_text": "Retry later",
    }


@router.get("/application/settings")
async def get_application_settings(user: AuthUser):
    """Return GitLab-shaped application settings for site admins."""
    _require_admin(user)
    return _application_settings_json()


async def _count(db: DbSession, model) -> int:
    return int((await db.execute(select(func.count(model.id)))).scalar() or 0)


@router.get("/application/statistics")
async def get_application_statistics(user: AuthUser, db: DbSession):
    """Return GitLab-shaped application statistics for site admins."""
    _require_admin(user)
    users = await _count(db, User)
    groups = await _count(db, Group)
    projects = await _count(db, Project)
    issues = await _count(db, Issue)
    merge_requests = await _count(db, PullRequest)
    pipelines = await _count(db, Pipeline)
    jobs = await _count(db, PipelineJob)
    runners = await _count(db, CiRunner)
    ci_variables = await _count(db, CiVariable)
    ci_secrets = await _count(db, CiSecret)
    return {
        "counts": {
            "users": users,
            "groups": groups,
            "projects": projects,
            "issues": issues,
            "merge_requests": merge_requests,
            "pipelines": pipelines,
            "jobs": jobs,
            "runners": runners,
            "ci_variables": ci_variables,
            "ci_secrets": ci_secrets,
        },
        "statistics": {
            "projects_count": projects,
            "forks_count": 0,
            "issues_count": issues,
            "merge_requests_count": merge_requests,
            "snippets_count": 0,
            "users_count": users,
            "groups_count": groups,
            "runners_count": runners,
        },
    }
