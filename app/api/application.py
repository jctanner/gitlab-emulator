"""GitLab application-level settings endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import AuthUser
from app.config import settings

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
