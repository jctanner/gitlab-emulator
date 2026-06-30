"""Tests for the browser-oriented repository and source UI."""

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from app.models.organization import OrgMembership, Organization
from app.models.repository import Repository
from tests.test_projects_api import _create_user_and_token


REPO_ROOT = Path(__file__).resolve().parents[1]


def _ui_session(client, username: str) -> None:
    from app.web.routes import _sign_session

    client.cookies.set("ui_session", _sign_session(username), path="/ui")


@pytest.mark.asyncio
async def test_ui_explore_lists_repositories_by_default_with_pagination(
    client, test_user
):
    """Explore lists repositories without requiring a search query."""
    _ui_session(client, test_user.login)

    for name in ["explore-alpha", "explore-beta", "explore-gamma"]:
        create_repo = await client.post(
            "/ui/new",
            data={"name": name, "auto_init": "true"},
            follow_redirects=False,
        )
        assert create_repo.status_code in (302, 303)

    first_page = await client.get("/ui/search?per_page=2")
    assert first_page.status_code == 200
    assert "Explore projects" in first_page.text
    assert "All projects" in first_page.text
    assert "testuser/explore-" in first_page.text
    assert "Next" in first_page.text
    assert "/ui/search?page=2&amp;per_page=2" in first_page.text

    second_page = await client.get("/ui/search?page=2&per_page=2")
    assert second_page.status_code == 200
    assert "Previous" in second_page.text
    assert "/ui/search?page=1&amp;per_page=2" in second_page.text


@pytest.mark.asyncio
async def test_ui_create_repo_under_nested_group_namespace(
    client, test_user, db_session
):
    """The repository create form can target a nested GitLab group namespace."""
    parent = Organization(login="redhat", name="Red Hat")
    child = Organization(login="redhat/rhel-ai", name="RHEL AI")
    grandchild = Organization(login="redhat/rhel-ai/agentic-ci", name="Agentic CI")
    db_session.add_all([parent, child, grandchild])
    await db_session.flush()
    db_session.add(
        OrgMembership(
            org_id=grandchild.id,
            user_id=test_user.id,
            role="admin",
            state="active",
        )
    )
    await db_session.commit()

    _ui_session(client, test_user.login)
    form = await client.get("/ui/new")
    assert form.status_code == 200
    assert '<option value="redhat/rhel-ai/agentic-ci"' in form.text

    create_repo = await client.post(
        "/ui/new",
        data={
            "namespace_path": "redhat/rhel-ai/agentic-ci",
            "name": "strat-pipeline",
            "description": "Nested project",
            "private": "true",
            "auto_init": "true",
        },
        follow_redirects=False,
    )
    assert create_repo.status_code in (302, 303)
    assert (
        create_repo.headers["location"]
        == "/ui/redhat/rhel-ai/agentic-ci/strat-pipeline"
    )

    repo = (
        await db_session.execute(
            select(Repository).where(
                Repository.full_name == "redhat/rhel-ai/agentic-ci/strat-pipeline"
            )
        )
    ).scalar_one()
    assert repo.owner_type == "Organization"
    assert repo.private is True

    nested_page = await client.get("/ui/redhat/rhel-ai/agentic-ci/strat-pipeline")
    assert nested_page.status_code == 200
    assert "strat-pipeline" in nested_page.text
    assert "Nested project" in nested_page.text

    new_file_page = await client.get(
        "/ui/redhat/rhel-ai/agentic-ci/strat-pipeline/new/main"
    )
    assert new_file_page.status_code == 200
    assert "Create new file" in new_file_page.text

    create_file = await client.post(
        "/ui/redhat/rhel-ai/agentic-ci/strat-pipeline/new/main",
        data={
            "filename": "README.md",
            "content": "# Strategy pipeline\n",
            "commit_message": "Create nested source file",
        },
        follow_redirects=False,
    )
    assert create_file.status_code in (302, 303)
    assert (
        create_file.headers["location"]
        == "/ui/redhat/rhel-ai/agentic-ci/strat-pipeline/blob/main/README.md"
    )

    blob = await client.get(
        "/ui/redhat/rhel-ai/agentic-ci/strat-pipeline/blob/main/README.md"
    )
    assert blob.status_code == 200
    assert "Strategy pipeline" in blob.text

    pipelines_page = await client.get(
        "/ui/redhat/rhel-ai/agentic-ci/strat-pipeline/-/pipelines"
    )
    assert pipelines_page.status_code == 200
    assert "New pipeline" in pipelines_page.text
    assert "Filter pipelines" in pipelines_page.text
    assert "Show Pipeline ID" in pipelines_page.text
    assert (
        "/ui/redhat/rhel-ai/agentic-ci/strat-pipeline/-/pipelines"
        in pipelines_page.text
    )

    left_nav_paths = [
        "/settings",
        "/issues",
        "/pulls",
        "/branches",
        "/commits/main",
        "/tags",
        "/-/ci/editor",
        "/-/members",
        "/-/labels",
        "/-/snippets",
        "/-/milestones",
        "/-/variables",
        "/-/secrets",
        "/-/deploy_keys",
        "/-/hooks",
        "/-/releases",
        "/-/pipeline_schedules",
        "/-/pipeline_schedules/new",
        "/-/pipelines/new",
        "/-/jobs",
        "/-/artifacts",
    ]
    for suffix in left_nav_paths:
        response = await client.get(
            f"/ui/redhat/rhel-ai/agentic-ci/strat-pipeline{suffix}"
        )
        assert response.status_code == 200, suffix


def test_gitlab_project_shell_sidebar_css_contract():
    """Project shell templates and CSS keep sidebar behavior GitLab-like."""
    css = (REPO_ROOT / "app/web/static/css/web.css").read_text()
    base_template = (REPO_ROOT / "app/web/templates/base.html").read_text()
    repo_nav = (REPO_ROOT / "app/web/templates/_repo_nav.html").read_text()
    pipeline_detail = (
        REPO_ROOT / "app/web/templates/repo_pipeline_detail.html"
    ).read_text()
    jobs_template = (REPO_ROOT / "app/web/templates/repo_jobs.html").read_text()
    pipelines_template = (
        REPO_ROOT / "app/web/templates/repo_pipelines.html"
    ).read_text()
    pipeline_editor_template = (
        REPO_ROOT / "app/web/templates/repo_pipeline_editor.html"
    ).read_text()
    pipeline_schedules_template = (
        REPO_ROOT / "app/web/templates/repo_pipeline_schedules.html"
    ).read_text()
    pipeline_schedule_new_template = (
        REPO_ROOT / "app/web/templates/repo_pipeline_schedule_new.html"
    ).read_text()

    assert "--gl-shell-bg: #eceaf4;" in css
    assert "--gl-topbar-height: 44px;" in css
    assert ".gl-topbar" in css
    assert "background: var(--gl-shell-bg) !important;" in css
    assert "background-color: var(--gl-shell-bg) !important;" in css
    assert ".gl-app-shell:has(.gl-project-sidebar) .gl-main-column" in css
    assert "border-top-left-radius: 10px;" in css
    assert ".gl-sidebar-scroll" in css
    assert "overflow-y: auto;" in css
    assert "scrollbar-gutter: stable;" in css
    assert "max-height: calc(100vh - var(--gl-topbar-height));" in css
    assert "overflow: hidden;" in css
    assert ".gl-sidebar-footer" in css
    assert "flex: 0 0 auto;" in css
    assert "height: calc(100vh - var(--gl-topbar-height));" in css

    for template in (base_template, repo_nav):
        assert '<div class="gl-sidebar-scroll">' in template
        assert '<div class="gl-sidebar-footer">' in template
        assert 'aria-label="Help">Help</button>' in template
        assert 'aria-label="Collapse sidebar">Collapse sidebar</button>' in template

    assert 'href="{{ url_prefix }}/{{ owner }}/{{ repo.name }}/-/pipelines"' in repo_nav
    assert 'href="{{ url_prefix }}/{{ owner }}/{{ repo.name }}/branches"' in repo_nav
    assert "job_diagnostics.get(job.id)" in pipeline_detail
    assert "This job is pending because" in pipeline_detail
    assert "downstream_by_job.get(job.id)" in pipeline_detail
    assert "Downstream pipeline" in pipeline_detail
    assert "Downstream pending:" in pipeline_detail
    assert "job_diagnostics.get(job.id)" in jobs_template
    assert "Pending: {{ diagnostic.reasons|join" in jobs_template

    for template in (
        pipelines_template,
        pipeline_editor_template,
        pipeline_schedules_template,
        pipeline_schedule_new_template,
    ):
        assert (
            '<a href="{{ url_prefix }}/{{ owner }}/{{ repo.name }}">{{ repo.full_name }}</a>'
            in template
        )
        assert (
            '<a href="{{ url_prefix }}/{{ owner }}/{{ repo.name }}">{{ owner }}</a>'
            not in template
        )


def test_web_job_scheduling_diagnostics_are_scoped_per_pipeline(monkeypatch):
    """Project jobs pages must not explain blockers using jobs from old pipelines."""
    from app.web import routes as web_routes

    calls = []

    async def fake_runner_diagnostics(_db):
        return {"run_untagged": True}

    def fake_explain(jobs, runner):
        calls.append([job.id for job in jobs])
        return {job.id: {"job_id": job.id, "runner": runner} for job in jobs}

    monkeypatch.setattr(web_routes, "registered_runner_diagnostics", fake_runner_diagnostics)
    monkeypatch.setattr(web_routes, "explain_job_scheduling", fake_explain)

    jobs = [
        SimpleNamespace(id=1, pipeline_id=10),
        SimpleNamespace(id=2, pipeline_id=10),
        SimpleNamespace(id=3, pipeline_id=11),
    ]

    diagnostics = asyncio.run(web_routes._job_scheduling_diagnostics(None, jobs))

    assert calls == [[1, 2], [3]]
    assert sorted(diagnostics) == [1, 2, 3]


@pytest.mark.asyncio
async def test_ui_nested_bridge_pipeline_missing_target_redirects_error(
    client, test_user, db_session
):
    """Missing bridge targets render as form errors instead of 500s."""
    parent = Organization(login="redhat", name="Red Hat")
    child = Organization(login="redhat/rhel-ai", name="RHEL AI")
    grandchild = Organization(login="redhat/rhel-ai/agentic-ci", name="Agentic CI")
    db_session.add_all([parent, child, grandchild])
    await db_session.flush()
    db_session.add(
        OrgMembership(
            org_id=grandchild.id,
            user_id=test_user.id,
            role="admin",
            state="active",
        )
    )
    await db_session.commit()

    _ui_session(client, test_user.login)
    create_repo = await client.post(
        "/ui/new",
        data={
            "namespace_path": "redhat/rhel-ai/agentic-ci",
            "name": "bridge-source",
            "auto_init": "true",
        },
        follow_redirects=False,
    )
    assert create_repo.status_code in (302, 303)

    ci_yaml = """
build-dashboard:
  stage: deploy
  trigger:
    project: redhat/rhel-ai/agentic-ci/strat-dashboard
    branch: main
"""
    save_yaml = await client.post(
        "/ui/redhat/rhel-ai/agentic-ci/bridge-source/new/main",
        data={
            "filename": ".gitlab-ci.yml",
            "content": ci_yaml,
            "commit_message": "Create bridge CI config",
        },
        follow_redirects=False,
    )
    assert save_yaml.status_code in (302, 303)

    create_pipeline = await client.post(
        "/ui/redhat/rhel-ai/agentic-ci/bridge-source/-/pipelines",
        data={"ref": "main"},
        follow_redirects=False,
    )
    assert create_pipeline.status_code in (302, 303)
    redirect = urlsplit(create_pipeline.headers["location"])
    assert redirect.path == "/ui/redhat/rhel-ai/agentic-ci/bridge-source/-/pipelines/new"
    error = parse_qs(redirect.query)["error"][0]
    assert (
        error
        == "Bridge job build-dashboard target project not found: redhat/rhel-ai/agentic-ci/strat-dashboard"
    )


@pytest.mark.asyncio
async def test_ui_repo_and_source_management_workflow(client, test_user):
    """The web UI can create/edit/delete repositories and source files."""
    _ui_session(client, test_user.login)

    create_repo = await client.post(
        "/ui/new",
        data={
            "name": "ui-source-repo",
            "description": "Created from UI",
            "auto_init": "true",
        },
        follow_redirects=False,
    )
    assert create_repo.status_code in (302, 303)
    assert create_repo.headers["location"] == "/ui/testuser/ui-source-repo"

    settings_page = await client.get("/ui/testuser/ui-source-repo/settings")
    assert settings_page.status_code == 200
    assert "Repository settings" in settings_page.text
    assert "Delete repository" in settings_page.text
    assert "CI/CD security" in settings_page.text

    rename_repo = await client.post(
        "/ui/testuser/ui-source-repo/settings",
        data={
            "name": "ui-source-renamed",
            "description": "Updated from UI",
            "default_branch": "main",
            "private": "1",
            "ci_pipeline_variables_minimum_override_role": "developer",
        },
        follow_redirects=False,
    )
    assert rename_repo.status_code in (302, 303)
    assert (
        rename_repo.headers["location"]
        == "/ui/testuser/ui-source-renamed/settings?saved=1"
    )

    renamed_page = await client.get("/ui/testuser/ui-source-renamed")
    assert renamed_page.status_code == 200
    assert "Updated from UI" in renamed_page.text
    assert "Private" in renamed_page.text

    create_file = await client.post(
        "/ui/testuser/ui-source-renamed/new/main",
        data={
            "filename": "src/app.py",
            "content": "print('created')\n",
            "commit_message": "Create app source",
        },
        follow_redirects=False,
    )
    assert create_file.status_code in (302, 303)
    assert (
        create_file.headers["location"]
        == "/ui/testuser/ui-source-renamed/blob/main/src/app.py"
    )

    blob = await client.get("/ui/testuser/ui-source-renamed/blob/main/src/app.py")
    assert blob.status_code == 200
    assert "print(&#39;created&#39;)" in blob.text
    assert "Delete" in blob.text

    branches_page = await client.get("/ui/testuser/ui-source-renamed/branches")
    assert branches_page.status_code == 200
    assert re.search(
        r'class="gl-sidebar-link gl-sidebar-subitem selected"[^>]*href="/ui/testuser/ui-source-renamed/branches">Branches</a>',
        branches_page.text,
    )
    assert not re.search(
        r'class="gl-sidebar-link gl-sidebar-subitem selected"[^>]*href="/ui/testuser/ui-source-renamed">Repository</a>',
        branches_page.text,
    )

    commits_page = await client.get("/ui/testuser/ui-source-renamed/commits/main")
    assert commits_page.status_code == 200
    assert re.search(
        r'class="gl-sidebar-link gl-sidebar-subitem selected"[^>]*href="/ui/testuser/ui-source-renamed/commits/main">Commits</a>',
        commits_page.text,
    )
    assert not re.search(
        r'class="gl-sidebar-link gl-sidebar-subitem selected"[^>]*href="/ui/testuser/ui-source-renamed">Repository</a>',
        commits_page.text,
    )

    tags_page = await client.get("/ui/testuser/ui-source-renamed/tags")
    assert tags_page.status_code == 200
    assert re.search(
        r'class="gl-sidebar-link gl-sidebar-subitem selected"[^>]*href="/ui/testuser/ui-source-renamed/tags">Tags</a>',
        tags_page.text,
    )
    assert not re.search(
        r'class="gl-sidebar-link gl-sidebar-subitem selected"[^>]*href="/ui/testuser/ui-source-renamed">Repository</a>',
        tags_page.text,
    )

    snippets_page = await client.get("/ui/testuser/ui-source-renamed/-/snippets")
    assert snippets_page.status_code == 200
    assert "New snippet" in snippets_page.text
    assert re.search(
        r'class="gl-sidebar-link gl-sidebar-subitem selected"[^>]*href="/ui/testuser/ui-source-renamed/-/snippets">Snippets</a>',
        snippets_page.text,
    )

    create_snippet = await client.post(
        "/ui/testuser/ui-source-renamed/-/snippets",
        data={
            "title": "Reusable helper",
            "file_name": "helper.py",
            "description": "Small helper snippet",
            "content": "def helper():\n    return 'ok'\n",
            "visibility": "private",
        },
        follow_redirects=False,
    )
    assert create_snippet.status_code in (302, 303)
    snippet_location = create_snippet.headers["location"]
    assert snippet_location.startswith("/ui/testuser/ui-source-renamed/-/snippets/")

    snippet_detail = await client.get(snippet_location)
    assert snippet_detail.status_code == 200
    assert "Reusable helper" in snippet_detail.text
    assert "helper.py" in snippet_detail.text
    assert "def helper():" in snippet_detail.text

    snippet_id = urlsplit(snippet_location).path.rsplit("/", 1)[-1]
    delete_snippet = await client.post(
        f"/ui/testuser/ui-source-renamed/-/snippets/{snippet_id}/delete",
        follow_redirects=False,
    )
    assert delete_snippet.status_code in (302, 303)

    snippets_after_delete = await client.get("/ui/testuser/ui-source-renamed/-/snippets")
    assert snippets_after_delete.status_code == 200
    assert "No snippets yet." in snippets_after_delete.text
    assert "Reusable helper" not in snippets_after_delete.text

    create_issue = await client.post(
        "/ui/testuser/ui-source-renamed/issues/new",
        data={
            "title": "Track work item rendering",
            "body": "Make the work item split view useful.",
        },
        follow_redirects=False,
    )
    assert create_issue.status_code in (302, 303)
    assert create_issue.headers["location"] == "/ui/testuser/ui-source-renamed/issues/1"

    work_item = await client.get("/ui/testuser/ui-source-renamed/issues/1")
    assert work_item.status_code == 200
    assert "Work items" in work_item.text
    assert "Track work item rendering" in work_item.text
    assert "Make the work item split view useful." in work_item.text
    assert "work-items-shell has-selection" in work_item.text
    assert "work-item-row selected" in work_item.text
    assert "Activity" in work_item.text
    assert "Time tracking" in work_item.text
    assert '<span class="gl-sidebar-link disabled">' in work_item.text
    assert "Issue boards</span>" in work_item.text
    assert "Repository graph</span>" in work_item.text
    assert (
        'href="/ui/testuser/ui-source-renamed/branches">Branches</a>' in work_item.text
    )
    assert (
        'href="/ui/testuser/ui-source-renamed/-/snippets">Snippets</a>'
        in work_item.text
    )
    assert 'href="/ui/testuser/ui-source-renamed/-/jobs">Jobs</a>' in work_item.text
    assert (
        'href="/ui/testuser/ui-source-renamed/-/pipeline_schedules">Pipeline schedules</a>'
        in work_item.text
    )

    edit_file = await client.post(
        "/ui/testuser/ui-source-renamed/edit/main/src/app.py",
        data={
            "content": "print('updated')\n",
            "commit_message": "Update app source",
        },
        follow_redirects=False,
    )
    assert edit_file.status_code in (302, 303)

    raw = await client.get("/ui/testuser/ui-source-renamed/raw/main/src/app.py")
    assert raw.status_code == 200
    assert raw.text == "print('updated')\n"

    delete_file = await client.post(
        "/ui/testuser/ui-source-renamed/delete-file/main/src/app.py",
        data={"commit_message": "Delete app source"},
        follow_redirects=False,
    )
    assert delete_file.status_code in (302, 303)

    deleted_raw = await client.get("/ui/testuser/ui-source-renamed/raw/main/src/app.py")
    assert deleted_raw.status_code == 404

    delete_repo = await client.post(
        "/ui/testuser/ui-source-renamed/settings/delete",
        data={"confirm_repository": "testuser/ui-source-renamed"},
        follow_redirects=False,
    )
    assert delete_repo.status_code in (302, 303)
    assert delete_repo.headers["location"] == "/ui/"

    missing_repo = await client.get("/ui/testuser/ui-source-renamed")
    assert missing_repo.status_code == 404


@pytest.mark.asyncio
async def test_ui_repo_settings_update_ci_security_controls(
    client, test_user, test_token
):
    """Repository settings can update project CI security controls."""
    _ui_session(client, test_user.login)

    create_repo = await client.post(
        "/ui/new",
        data={"name": "ui-ci-security", "auto_init": "true"},
        follow_redirects=False,
    )
    assert create_repo.status_code in (302, 303)

    update_settings = await client.post(
        "/ui/testuser/ui-ci-security/settings",
        data={
            "name": "ui-ci-security",
            "description": "",
            "default_branch": "main",
            "ci_pipeline_variables_minimum_override_role": "no_one_allowed",
            "ci_strict_security_mode": "1",
        },
        follow_redirects=False,
    )
    assert update_settings.status_code in (302, 303)
    assert (
        update_settings.headers["location"]
        == "/ui/testuser/ui-ci-security/settings?saved=1"
    )

    settings_page = await client.get("/ui/testuser/ui-ci-security/settings?saved=1")
    assert settings_page.status_code == 200
    assert 'value="no_one_allowed" selected' in settings_page.text
    assert "Strict CI security mode" in settings_page.text
    assert "checked" in settings_page.text

    create_pipeline = await client.post(
        "/api/v4/projects/testuser%2Fui-ci-security/pipeline",
        json={
            "ref": "main",
            "variables": [{"key": "CUSTOM", "value": "blocked"}],
            "job": {"name": "blocked", "script": ["echo blocked"]},
        },
        headers={"Authorization": f"token {test_token}"},
    )
    assert create_pipeline.status_code == 400


@pytest.mark.asyncio
async def test_ui_project_ci_variables_management(client, test_user):
    """The project UI can create, update, and delete CI/CD variables."""
    _ui_session(client, test_user.login)

    create_repo = await client.post(
        "/ui/new",
        data={"name": "ui-ci-vars", "auto_init": "true"},
        follow_redirects=False,
    )
    assert create_repo.status_code in (302, 303)

    variables_page = await client.get("/ui/testuser/ui-ci-vars/-/variables")
    assert variables_page.status_code == 200
    assert "CI/CD variables" in variables_page.text
    assert "Add variable" in variables_page.text
    assert (
        'href="/ui/testuser/ui-ci-vars/-/variables">CI/CD variables</a>'
        in variables_page.text
    )

    create_variable = await client.post(
        "/ui/testuser/ui-ci-vars/-/variables",
        data={
            "key": "DEPLOY_TOKEN",
            "value": "super-secret-variable",
            "variable_type": "env_var",
            "environment_scope": "production",
            "description": "Deploy credential",
            "masked": "1",
            "hidden": "1",
            "protected": "1",
        },
        follow_redirects=False,
    )
    assert create_variable.status_code in (302, 303)

    variables_page = await client.get("/ui/testuser/ui-ci-vars/-/variables")
    assert variables_page.status_code == 200
    assert "DEPLOY_TOKEN" in variables_page.text
    assert "production" in variables_page.text
    assert "Deploy credential" in variables_page.text
    assert "super-secret-variable" not in variables_page.text
    assert "Value: hidden" in variables_page.text

    update_variable = await client.post(
        "/ui/testuser/ui-ci-vars/-/variables/1/update",
        data={
            "value": "rotated-variable",
            "variable_type": "file",
            "environment_scope": "staging",
            "description": "Rotated credential",
            "masked": "1",
            "raw": "1",
        },
        follow_redirects=False,
    )
    assert update_variable.status_code in (302, 303)

    variables_page = await client.get("/ui/testuser/ui-ci-vars/-/variables")
    assert variables_page.status_code == 200
    assert "staging" in variables_page.text
    assert "Rotated credential" in variables_page.text
    assert "rotated-variable" not in variables_page.text

    delete_variable = await client.post(
        "/ui/testuser/ui-ci-vars/-/variables/1/delete",
        follow_redirects=False,
    )
    assert delete_variable.status_code in (302, 303)
    variables_page = await client.get("/ui/testuser/ui-ci-vars/-/variables")
    assert "No CI/CD variables yet." in variables_page.text


@pytest.mark.asyncio
async def test_ui_project_secrets_management(client, test_user):
    """The project UI can create, update, and delete CI/CD secrets."""
    _ui_session(client, test_user.login)

    create_repo = await client.post(
        "/ui/new",
        data={"name": "ui-ci-secrets", "auto_init": "true"},
        follow_redirects=False,
    )
    assert create_repo.status_code in (302, 303)

    secrets_page = await client.get("/ui/testuser/ui-ci-secrets/-/secrets")
    assert secrets_page.status_code == 200
    assert "Secrets" in secrets_page.text
    assert "Add secret" in secrets_page.text
    assert (
        'href="/ui/testuser/ui-ci-secrets/-/secrets">Secrets</a>' in secrets_page.text
    )

    create_secret = await client.post(
        "/ui/testuser/ui-ci-secrets/-/secrets",
        data={
            "name": "DATABASE_PASSWORD",
            "value": "database-password-value",
            "environment_scope": "production",
            "branch_scope": "main",
            "description": "Database password",
            "rotation_reminder_days": "30",
            "protected": "1",
            "status": "healthy",
        },
        follow_redirects=False,
    )
    assert create_secret.status_code in (302, 303)

    secrets_page = await client.get("/ui/testuser/ui-ci-secrets/-/secrets")
    assert secrets_page.status_code == 200
    assert "DATABASE_PASSWORD" in secrets_page.text
    assert "production" in secrets_page.text
    assert "main" in secrets_page.text
    assert "Database password" in secrets_page.text
    assert "database-password-value" not in secrets_page.text

    update_secret = await client.post(
        "/ui/testuser/ui-ci-secrets/-/secrets/1/update",
        data={
            "value": "rotated-database-password",
            "environment_scope": "staging",
            "branch_scope": "release/*",
            "description": "Rotated database password",
            "rotation_reminder_days": "60",
            "status": "healthy",
        },
        follow_redirects=False,
    )
    assert update_secret.status_code in (302, 303)

    secrets_page = await client.get("/ui/testuser/ui-ci-secrets/-/secrets")
    assert secrets_page.status_code == 200
    assert "staging" in secrets_page.text
    assert "release/*" in secrets_page.text
    assert "Rotated database password" in secrets_page.text
    assert "rotated-database-password" not in secrets_page.text

    delete_secret = await client.post(
        "/ui/testuser/ui-ci-secrets/-/secrets/1/delete",
        follow_redirects=False,
    )
    assert delete_secret.status_code in (302, 303)
    secrets_page = await client.get("/ui/testuser/ui-ci-secrets/-/secrets")
    assert "No secrets yet." in secrets_page.text


@pytest.mark.asyncio
async def test_ui_project_deploy_keys_management(client, test_user):
    """The project UI can create and delete deploy keys."""
    _ui_session(client, test_user.login)

    create_repo = await client.post(
        "/ui/new",
        data={"name": "ui-deploy-keys", "auto_init": "true"},
        follow_redirects=False,
    )
    assert create_repo.status_code in (302, 303)

    keys_page = await client.get("/ui/testuser/ui-deploy-keys/-/deploy_keys")
    assert keys_page.status_code == 200
    assert "Deploy keys" in keys_page.text
    assert "Add deploy key" in keys_page.text
    assert (
        'href="/ui/testuser/ui-deploy-keys/-/deploy_keys">Deploy keys</a>'
        in keys_page.text
    )

    deploy_key = (
        "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7uiwebdeploykey"
        " gitlab-emulator@test"
    )
    add_key = await client.post(
        "/ui/testuser/ui-deploy-keys/-/deploy_keys",
        data={
            "title": "Production deploy",
            "key": deploy_key,
            "read_only": "1",
        },
        follow_redirects=False,
    )
    assert add_key.status_code in (302, 303)

    keys_page = await client.get("/ui/testuser/ui-deploy-keys/-/deploy_keys")
    assert keys_page.status_code == 200
    assert "Production deploy" in keys_page.text
    assert "Read-only" in keys_page.text
    assert deploy_key in keys_page.text

    delete_key = await client.post(
        "/ui/testuser/ui-deploy-keys/-/deploy_keys/1/delete",
        follow_redirects=False,
    )
    assert delete_key.status_code in (302, 303)
    keys_page = await client.get("/ui/testuser/ui-deploy-keys/-/deploy_keys")
    assert "No deploy keys yet." in keys_page.text
    assert deploy_key not in keys_page.text


@pytest.mark.asyncio
async def test_ui_project_members_management(client, db_session, test_user):
    """The project UI can create, update, and delete direct project members."""
    member, _ = await _create_user_and_token(db_session, "ui-project-member")
    _ui_session(client, test_user.login)

    create_repo = await client.post(
        "/ui/new",
        data={"name": "ui-members", "auto_init": "true"},
        follow_redirects=False,
    )
    assert create_repo.status_code in (302, 303)

    members_page = await client.get("/ui/testuser/ui-members/-/members")
    assert members_page.status_code == 200
    assert "Members" in members_page.text
    assert "Add member" in members_page.text
    assert "@testuser" in members_page.text
    assert "Owner" in members_page.text
    assert 'href="/ui/testuser/ui-members/-/members">Members</a>' in members_page.text

    add_member = await client.post(
        "/ui/testuser/ui-members/-/members",
        data={"username": member.login, "access_level": "30"},
        follow_redirects=False,
    )
    assert add_member.status_code in (302, 303)

    members_page = await client.get("/ui/testuser/ui-members/-/members")
    assert members_page.status_code == 200
    assert "@ui-project-member" in members_page.text
    assert '<option value="30" selected>Developer</option>' in members_page.text

    update_member = await client.post(
        f"/ui/testuser/ui-members/-/members/{member.id}/update",
        data={"access_level": "40"},
        follow_redirects=False,
    )
    assert update_member.status_code in (302, 303)

    members_page = await client.get("/ui/testuser/ui-members/-/members")
    assert members_page.status_code == 200
    assert '<option value="40" selected>Maintainer</option>' in members_page.text

    delete_member = await client.post(
        f"/ui/testuser/ui-members/-/members/{member.id}/delete",
        follow_redirects=False,
    )
    assert delete_member.status_code in (302, 303)
    members_page = await client.get("/ui/testuser/ui-members/-/members")
    assert "@ui-project-member" not in members_page.text
    assert "@testuser" in members_page.text


@pytest.mark.asyncio
async def test_ui_project_labels_and_milestones_management(client, test_user):
    """The project UI can create, update, and delete labels and milestones."""
    _ui_session(client, test_user.login)

    create_repo = await client.post(
        "/ui/new",
        data={"name": "ui-plan-metadata", "auto_init": "true"},
        follow_redirects=False,
    )
    assert create_repo.status_code in (302, 303)

    labels_page = await client.get("/ui/testuser/ui-plan-metadata/-/labels")
    assert labels_page.status_code == 200
    assert "Labels" in labels_page.text
    assert "Add label" in labels_page.text
    assert 'href="/ui/testuser/ui-plan-metadata/-/labels">Labels</a>' in labels_page.text

    create_label = await client.post(
        "/ui/testuser/ui-plan-metadata/-/labels",
        data={
            "name": "bug",
            "color": "cc0000",
            "description": "Something is broken",
        },
        follow_redirects=False,
    )
    assert create_label.status_code in (302, 303)
    labels_page = await client.get("/ui/testuser/ui-plan-metadata/-/labels")
    assert "bug" in labels_page.text
    assert "Something is broken" in labels_page.text
    assert "cc0000" in labels_page.text

    update_label = await client.post(
        "/ui/testuser/ui-plan-metadata/-/labels/1/update",
        data={
            "name": "defect",
            "color": "0052cc",
            "description": "Confirmed defect",
        },
        follow_redirects=False,
    )
    assert update_label.status_code in (302, 303)
    labels_page = await client.get("/ui/testuser/ui-plan-metadata/-/labels")
    assert "defect" in labels_page.text
    assert "Confirmed defect" in labels_page.text
    assert "0052cc" in labels_page.text
    assert "Something is broken" not in labels_page.text

    milestones_page = await client.get("/ui/testuser/ui-plan-metadata/-/milestones")
    assert milestones_page.status_code == 200
    assert "Milestones" in milestones_page.text
    assert "Add milestone" in milestones_page.text
    assert (
        'href="/ui/testuser/ui-plan-metadata/-/milestones">Milestones</a>'
        in milestones_page.text
    )

    create_milestone = await client.post(
        "/ui/testuser/ui-plan-metadata/-/milestones",
        data={
            "title": "v1.0",
            "description": "First release",
            "state": "open",
            "due_on": "2026-07-31",
        },
        follow_redirects=False,
    )
    assert create_milestone.status_code in (302, 303)
    milestones_page = await client.get("/ui/testuser/ui-plan-metadata/-/milestones")
    assert "#1 v1.0" in milestones_page.text
    assert "Due 2026-07-31" in milestones_page.text
    assert "First release" in milestones_page.text

    update_milestone = await client.post(
        "/ui/testuser/ui-plan-metadata/-/milestones/1/update",
        data={
            "title": "v1.0 shipped",
            "description": "Released",
            "state": "closed",
            "due_on": "2026-08-01",
        },
        follow_redirects=False,
    )
    assert update_milestone.status_code in (302, 303)
    milestones_page = await client.get("/ui/testuser/ui-plan-metadata/-/milestones")
    assert "v1.0 shipped" in milestones_page.text
    assert "Closed" in milestones_page.text
    assert "Due 2026-08-01" in milestones_page.text

    delete_label = await client.post(
        "/ui/testuser/ui-plan-metadata/-/labels/1/delete",
        follow_redirects=False,
    )
    assert delete_label.status_code in (302, 303)
    labels_page = await client.get("/ui/testuser/ui-plan-metadata/-/labels")
    assert "No labels yet." in labels_page.text

    delete_milestone = await client.post(
        "/ui/testuser/ui-plan-metadata/-/milestones/1/delete",
        follow_redirects=False,
    )
    assert delete_milestone.status_code in (302, 303)
    milestones_page = await client.get("/ui/testuser/ui-plan-metadata/-/milestones")
    assert "No milestones yet." in milestones_page.text


@pytest.mark.asyncio
async def test_ui_project_releases_management(client, test_user):
    """The project UI can create, update, and delete releases."""
    _ui_session(client, test_user.login)

    create_repo = await client.post(
        "/ui/new",
        data={"name": "ui-releases", "auto_init": "true"},
        follow_redirects=False,
    )
    assert create_repo.status_code in (302, 303)

    releases_page = await client.get("/ui/testuser/ui-releases/-/releases")
    assert releases_page.status_code == 200
    assert "Releases" in releases_page.text
    assert "Create release" in releases_page.text
    assert 'href="/ui/testuser/ui-releases/-/releases">Releases</a>' in releases_page.text

    create_release = await client.post(
        "/ui/testuser/ui-releases/-/releases",
        data={
            "tag_name": "v1.0.0",
            "name": "Version 1.0.0",
            "ref": "main",
            "description": "Initial release",
            "prerelease": "1",
        },
        follow_redirects=False,
    )
    assert create_release.status_code in (302, 303)

    releases_page = await client.get("/ui/testuser/ui-releases/-/releases")
    assert releases_page.status_code == 200
    assert "Version 1.0.0" in releases_page.text
    assert "v1.0.0" in releases_page.text
    assert "Initial release" in releases_page.text
    assert "Prerelease" in releases_page.text

    update_release = await client.post(
        "/ui/testuser/ui-releases/-/releases/1/update",
        data={
            "name": "Version 1.0.1",
            "description": "Updated release notes",
            "draft": "1",
        },
        follow_redirects=False,
    )
    assert update_release.status_code in (302, 303)

    releases_page = await client.get("/ui/testuser/ui-releases/-/releases")
    assert releases_page.status_code == 200
    assert "Version 1.0.1" in releases_page.text
    assert "Updated release notes" in releases_page.text
    assert "Draft" in releases_page.text

    delete_release = await client.post(
        "/ui/testuser/ui-releases/-/releases/1/delete",
        follow_redirects=False,
    )
    assert delete_release.status_code in (302, 303)
    releases_page = await client.get("/ui/testuser/ui-releases/-/releases")
    assert "No releases yet." in releases_page.text


@pytest.mark.asyncio
async def test_ui_project_webhooks_management(client, test_user):
    """The project UI can create, update, and delete webhooks."""
    _ui_session(client, test_user.login)

    create_repo = await client.post(
        "/ui/new",
        data={"name": "ui-webhooks", "auto_init": "true"},
        follow_redirects=False,
    )
    assert create_repo.status_code in (302, 303)

    hooks_page = await client.get("/ui/testuser/ui-webhooks/-/hooks")
    assert hooks_page.status_code == 200
    assert "Webhooks" in hooks_page.text
    assert "Add webhook" in hooks_page.text
    assert 'href="/ui/testuser/ui-webhooks/-/hooks">Webhooks</a>' in hooks_page.text

    create_hook = await client.post(
        "/ui/testuser/ui-webhooks/-/hooks",
        data={
            "url": "https://example.test/hook",
            "token": "super-hook-token",
            "events": ["push_events", "pipeline_events"],
            "enable_ssl_verification": "1",
            "active": "1",
        },
        follow_redirects=False,
    )
    assert create_hook.status_code in (302, 303)

    hooks_page = await client.get("/ui/testuser/ui-webhooks/-/hooks")
    assert hooks_page.status_code == 200
    assert "https://example.test/hook" in hooks_page.text
    assert "Token ****oken" in hooks_page.text
    assert "super-hook-token" not in hooks_page.text
    assert "SSL verified" in hooks_page.text
    assert "Active" in hooks_page.text

    update_hook = await client.post(
        "/ui/testuser/ui-webhooks/-/hooks/1/update",
        data={
            "url": "https://example.test/updated-hook",
            "events": ["issues_events", "merge_requests_events"],
        },
        follow_redirects=False,
    )
    assert update_hook.status_code in (302, 303)

    hooks_page = await client.get("/ui/testuser/ui-webhooks/-/hooks")
    assert hooks_page.status_code == 200
    assert "https://example.test/updated-hook" in hooks_page.text
    assert "Inactive" in hooks_page.text
    assert "SSL not verified" in hooks_page.text
    assert "Token ****oken" in hooks_page.text

    delete_hook = await client.post(
        "/ui/testuser/ui-webhooks/-/hooks/1/delete",
        follow_redirects=False,
    )
    assert delete_hook.status_code in (302, 303)
    hooks_page = await client.get("/ui/testuser/ui-webhooks/-/hooks")
    assert "No webhooks yet." in hooks_page.text
    assert "updated-hook" not in hooks_page.text


@pytest.mark.asyncio
async def test_ui_repo_pipeline_and_job_interface(client, test_user):
    """The web UI can create repository pipelines and inspect job runs."""
    _ui_session(client, test_user.login)

    create_repo = await client.post(
        "/ui/new",
        data={"name": "ui-ci-repo", "auto_init": "true"},
        follow_redirects=False,
    )
    assert create_repo.status_code in (302, 303)

    ci_yaml = """
ui_job:
  script:
    - echo from repo ui
"""
    save_yaml = await client.post(
        "/ui/testuser/ui-ci-repo/new/main",
        data={
            "filename": ".gitlab-ci.yml",
            "content": ci_yaml,
            "commit_message": "Create CI config",
        },
        follow_redirects=False,
    )
    assert save_yaml.status_code in (302, 303)

    pipelines_page = await client.get("/ui/testuser/ui-ci-repo/-/pipelines")
    assert pipelines_page.status_code == 200
    assert "New pipeline" in pipelines_page.text
    assert "Filter pipelines" in pipelines_page.text
    assert "Show Pipeline ID" in pipelines_page.text
    assert "Pipeline editor" in pipelines_page.text
    assert "/ui/testuser/ui-ci-repo/-/pipelines/new" in pipelines_page.text
    assert "/ui/testuser/ui-ci-repo/-/ci/editor" in pipelines_page.text
    assert "All" in pipelines_page.text
    assert "Finished" in pipelines_page.text
    assert "Branches" in pipelines_page.text
    assert "Tags" in pipelines_page.text

    run_pipeline_page = await client.get("/ui/testuser/ui-ci-repo/-/pipelines/new")
    assert run_pipeline_page.status_code == 200
    assert "Run new pipeline" in run_pipeline_page.text
    assert 'name="variable_key"' in run_pipeline_page.text
    assert "Run for branch name or tag" in run_pipeline_page.text

    edit_ci = await client.get("/ui/testuser/ui-ci-repo/-/ci/editor")
    assert edit_ci.status_code == 200
    assert "Pipeline editor" in edit_ci.text
    assert "Configuration file loaded." in edit_ci.text
    assert "Full configuration" in edit_ci.text
    assert "Commit changes" in edit_ci.text
    assert 'data-code-editor="yaml"' in edit_ci.text
    assert "/ui/static/js/codemirror-yaml.js" in edit_ci.text

    save_plain = await client.post(
        "/ui/testuser/ui-ci-repo/new/main",
        data={
            "filename": "notes.txt",
            "content": "plain text\n",
            "commit_message": "Create plain text",
        },
        follow_redirects=False,
    )
    assert save_plain.status_code in (302, 303)
    edit_plain = await client.get("/ui/testuser/ui-ci-repo/edit/main/notes.txt")
    assert edit_plain.status_code == 200
    assert 'data-code-editor="yaml"' not in edit_plain.text

    create_pipeline = await client.post(
        "/ui/testuser/ui-ci-repo/-/pipelines",
        data={"ref": "main"},
        follow_redirects=False,
    )
    assert create_pipeline.status_code in (302, 303)
    create_location = create_pipeline.headers["location"]
    create_path = urlsplit(create_location).path
    assert re.search(r"/-/pipelines/\d+$", create_path)
    pipeline_id = int(create_path.rsplit("/", 1)[1])

    pipeline_page = await client.get(
        f"/ui/testuser/ui-ci-repo/-/pipelines/{pipeline_id}"
    )
    assert pipeline_page.status_code == 200
    assert f"Pipeline #{pipeline_id}" in pipeline_page.text
    assert "Recent pipelines" not in pipeline_page.text
    assert "Back to pipelines" in pipeline_page.text
    assert "window.setTimeout" in pipeline_page.text
    assert "window.location.reload" in pipeline_page.text
    assert "ui_job" in pipeline_page.text
    assert "pending" in pipeline_page.text
    assert re.search(
        r'class="gl-sidebar-link gl-sidebar-subitem selected"[^>]*>Pipelines</a>',
        pipeline_page.text,
    )
    assert not re.search(
        r'class="gl-sidebar-link gl-sidebar-subitem selected"[^>]*>Jobs</a>',
        pipeline_page.text,
    )

    job_match = re.search(r"/-/jobs/(\d+)", pipeline_page.text)
    assert job_match is not None
    job_id = int(job_match.group(1))

    jobs_page = await client.get("/ui/testuser/ui-ci-repo/-/jobs")
    assert jobs_page.status_code == 200
    assert "Recent jobs" in jobs_page.text
    assert f"#{job_id} ui_job" in jobs_page.text
    assert f"/ui/testuser/ui-ci-repo/-/jobs/{job_id}" in jobs_page.text
    assert 'href="/ui/testuser/ui-ci-repo/-/jobs">Jobs</a>' in jobs_page.text
    assert not re.search(
        r'class="gl-sidebar-link gl-sidebar-subitem selected"[^>]*>Pipelines</a>',
        jobs_page.text,
    )
    assert re.search(
        r'class="gl-sidebar-link gl-sidebar-subitem selected"[^>]*href="/ui/testuser/ui-ci-repo/-/jobs">Jobs</a>',
        jobs_page.text,
    )

    job_page = await client.get(f"/ui/testuser/ui-ci-repo/-/jobs/{job_id}")
    assert job_page.status_code == 200
    assert f"Job #{job_id}" in job_page.text
    assert f"Pipeline #{pipeline_id}" in job_page.text
    assert "Recent pipelines" not in job_page.text
    assert "Back to pipelines" in job_page.text
    assert "Trace API" in job_page.text
    assert "window.setTimeout" in job_page.text
    assert not re.search(
        r'class="gl-sidebar-link gl-sidebar-subitem selected"[^>]*>Pipelines</a>',
        job_page.text,
    )
    assert re.search(
        r'class="gl-sidebar-link gl-sidebar-subitem selected"[^>]*>Jobs</a>',
        job_page.text,
    )

    cancel_job = await client.post(
        f"/ui/testuser/ui-ci-repo/-/jobs/{job_id}/cancel",
        follow_redirects=False,
    )
    assert cancel_job.status_code in (302, 303)
    assert cancel_job.headers["location"].startswith(
        f"/ui/testuser/ui-ci-repo/-/jobs/{job_id}"
    )

    canceled_page = await client.get(cancel_job.headers["location"])
    assert canceled_page.status_code == 200
    assert "Job canceled." in canceled_page.text
    assert "canceled" in canceled_page.text


@pytest.mark.asyncio
async def test_ui_project_pipeline_schedules_management(client, test_user):
    """The project UI can create, update, play, and delete pipeline schedules."""
    _ui_session(client, test_user.login)

    create_repo = await client.post(
        "/ui/new",
        data={"name": "ui-schedules", "auto_init": "true"},
        follow_redirects=False,
    )
    assert create_repo.status_code in (302, 303)

    ci_yaml = """
scheduled_probe:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "schedule"'
  script:
    - echo schedule $SCHEDULE_VAR
api_probe:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "api"'
  script:
    - echo api
"""
    save_yaml = await client.post(
        "/ui/testuser/ui-schedules/new/main",
        data={
            "filename": ".gitlab-ci.yml",
            "content": ci_yaml,
            "commit_message": "Create scheduled CI config",
        },
        follow_redirects=False,
    )
    assert save_yaml.status_code in (302, 303)

    schedules_page = await client.get("/ui/testuser/ui-schedules/-/pipeline_schedules")
    assert schedules_page.status_code == 200
    assert "Pipeline schedules" in schedules_page.text
    assert "New schedule" in schedules_page.text
    assert (
        'href="/ui/testuser/ui-schedules/-/pipeline_schedules/new">New schedule</a>'
        in schedules_page.text
    )
    assert (
        'href="/ui/testuser/ui-schedules/-/pipeline_schedules">Pipeline schedules</a>'
        in schedules_page.text
    )
    assert "Schedule a new pipeline" not in schedules_page.text

    new_schedule_page = await client.get(
        "/ui/testuser/ui-schedules/-/pipeline_schedules/new"
    )
    assert new_schedule_page.status_code == 200
    assert 'href="/ui/testuser/ui-schedules/-/pipelines">Pipelines</a>' in new_schedule_page.text
    assert (
        'href="/ui/testuser/ui-schedules/-/ci/editor">Pipeline editor</a>'
        in new_schedule_page.text
    )
    assert ">Schedules</a>" in new_schedule_page.text
    assert "Schedule a new pipeline" in new_schedule_page.text
    assert "Cron timezone" in new_schedule_page.text
    assert "Select timezone" in new_schedule_page.text
    assert "Interval Pattern" in new_schedule_page.text
    assert "Every day (at 7:57am)" in new_schedule_page.text
    assert "Select target branch or tag" in new_schedule_page.text
    assert "Inputs" in new_schedule_page.text
    assert "Variable type" in new_schedule_page.text
    assert "Create pipeline schedule" in new_schedule_page.text

    create_schedule = await client.post(
        "/ui/testuser/ui-schedules/-/pipeline_schedules",
        data={
            "description": "Nightly",
            "ref": "main",
            "cron": "0 3 * * *",
            "cron_timezone": "UTC",
            "active": "1",
            "variable_type": "variable",
            "variable_key": "SCHEDULE_VAR",
            "variable_value": "from-ui",
        },
        follow_redirects=False,
    )
    assert create_schedule.status_code in (302, 303)

    schedules_page = await client.get("/ui/testuser/ui-schedules/-/pipeline_schedules")
    assert schedules_page.status_code == 200
    assert "Nightly" in schedules_page.text
    assert "0 3 * * *" in schedules_page.text
    assert "SCHEDULE_VAR=from-ui" in schedules_page.text
    assert "Active" in schedules_page.text
    schedule_match = re.search(r"/pipeline_schedules/(\d+)/update", schedules_page.text)
    assert schedule_match is not None
    schedule_id = int(schedule_match.group(1))

    update_schedule = await client.post(
        f"/ui/testuser/ui-schedules/-/pipeline_schedules/{schedule_id}/update",
        data={
            "description": "Nightly updated",
            "ref": "main",
            "cron": "30 4 * * 1",
            "cron_timezone": "America/New_York",
            "variables_text": "SCHEDULE_VAR=rotated\n",
            "variable_type": "variable",
        },
        follow_redirects=False,
    )
    assert update_schedule.status_code in (302, 303)

    schedules_page = await client.get("/ui/testuser/ui-schedules/-/pipeline_schedules")
    assert "Nightly updated" in schedules_page.text
    assert "30 4 * * 1" in schedules_page.text
    assert "America/New_York" in schedules_page.text
    assert "Inactive" in schedules_page.text
    assert "SCHEDULE_VAR=rotated" in schedules_page.text

    play_schedule = await client.post(
        f"/ui/testuser/ui-schedules/-/pipeline_schedules/{schedule_id}/play",
        follow_redirects=False,
    )
    assert play_schedule.status_code in (302, 303)
    play_path = urlsplit(play_schedule.headers["location"]).path
    assert re.search(r"/ui/testuser/ui-schedules/-/pipelines/\d+$", play_path)
    pipeline_id = int(play_path.rsplit("/", 1)[1])

    pipeline_page = await client.get(play_path)
    assert pipeline_page.status_code == 200
    assert f"Pipeline #{pipeline_id}" in pipeline_page.text
    assert "scheduled_probe" in pipeline_page.text
    assert "api_probe" not in pipeline_page.text

    schedules_page = await client.get("/ui/testuser/ui-schedules/-/pipeline_schedules")
    assert f"/ui/testuser/ui-schedules/-/pipelines/{pipeline_id}" in schedules_page.text
    assert f"#{pipeline_id}" in schedules_page.text

    delete_schedule = await client.post(
        f"/ui/testuser/ui-schedules/-/pipeline_schedules/{schedule_id}/delete",
        follow_redirects=False,
    )
    assert delete_schedule.status_code in (302, 303)
    schedules_page = await client.get("/ui/testuser/ui-schedules/-/pipeline_schedules")
    assert "No pipeline schedules yet." in schedules_page.text
    assert "Nightly updated" not in schedules_page.text


@pytest.mark.asyncio
async def test_ui_project_artifacts_page(client, db_session, test_user):
    """The project UI lists job artifacts and links to existing downloads."""
    from sqlalchemy import select

    from app.models.ci import JobArtifact, Pipeline, PipelineJob
    from app.models.repository import Repository

    _ui_session(client, test_user.login)

    create_repo = await client.post(
        "/ui/new",
        data={"name": "ui-artifacts", "auto_init": "true"},
        follow_redirects=False,
    )
    assert create_repo.status_code in (302, 303)

    repo = (
        await db_session.execute(
            select(Repository).where(Repository.full_name == "testuser/ui-artifacts")
        )
    ).scalar_one()
    pipeline = Pipeline(
        project_id=repo.id,
        iid=1,
        ref="main",
        sha="abc123artifact",
        status="success",
        source="web",
    )
    db_session.add(pipeline)
    await db_session.flush()
    job = PipelineJob(
        pipeline_id=pipeline.id,
        project_id=repo.id,
        name="package",
        stage="build",
        status="success",
        script=["echo package"],
        artifacts_paths=["dist/package.zip"],
        job_token="gljt-ui-artifacts-job",
    )
    db_session.add(job)
    await db_session.flush()
    db_session.add(
        JobArtifact(
            job_id=job.id,
            filename="job-package-artifacts.zip",
            content_type="application/zip",
            file_type="archive",
            file_format="zip",
            size=512,
            storage_path="/tmp/gitlab-emulator-ui-artifact.zip",
        )
    )
    await db_session.commit()

    artifacts_page = await client.get("/ui/testuser/ui-artifacts/-/artifacts")
    assert artifacts_page.status_code == 200
    assert "Artifacts" in artifacts_page.text
    assert "Recent artifacts" in artifacts_page.text
    assert "job-package-artifacts.zip" in artifacts_page.text
    assert "archive/zip" in artifacts_page.text
    assert "512 bytes" in artifacts_page.text
    assert f"/api/v4/projects/{repo.id}/jobs/{job.id}/artifacts" in artifacts_page.text
    assert f"/ui/testuser/ui-artifacts/-/jobs/{job.id}" in artifacts_page.text
    assert f"/ui/testuser/ui-artifacts/-/pipelines/{pipeline.id}" in artifacts_page.text
    assert re.search(
        r'class="gl-sidebar-link gl-sidebar-subitem selected"[^>]*href="/ui/testuser/ui-artifacts/-/artifacts">Artifacts</a>',
        artifacts_page.text,
    )
    assert not re.search(
        r'class="gl-sidebar-link gl-sidebar-subitem selected"[^>]*>Pipelines</a>',
        artifacts_page.text,
    )
    assert not re.search(
        r'class="gl-sidebar-link gl-sidebar-subitem selected"[^>]*>Jobs</a>',
        artifacts_page.text,
    )
