"""Tests for the browser-oriented repository and source UI."""

import re
from urllib.parse import urlsplit

import pytest

from tests.test_projects_api import _create_user_and_token


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
    assert 'href="/ui/testuser/ui-source-renamed/-/jobs">Jobs</a>' in work_item.text

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
    assert "Run pipeline" in pipelines_page.text
    assert "Recent pipelines" in pipelines_page.text
    assert "Edit .gitlab-ci.yml" in pipelines_page.text
    assert "/ui/testuser/ui-ci-repo/edit/main/.gitlab-ci.yml" in pipelines_page.text

    edit_ci = await client.get("/ui/testuser/ui-ci-repo/edit/main/.gitlab-ci.yml")
    assert edit_ci.status_code == 200
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
