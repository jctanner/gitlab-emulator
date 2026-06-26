"""Tests for the browser-oriented repository and source UI."""

import re
from urllib.parse import urlsplit

import pytest


def _ui_session(client, username: str) -> None:
    from app.web.routes import _sign_session

    client.cookies.set("ui_session", _sign_session(username), path="/ui")


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

    rename_repo = await client.post(
        "/ui/testuser/ui-source-repo/settings",
        data={
            "name": "ui-source-renamed",
            "description": "Updated from UI",
            "default_branch": "main",
            "private": "1",
        },
        follow_redirects=False,
    )
    assert rename_repo.status_code in (302, 303)
    assert rename_repo.headers["location"] == "/ui/testuser/ui-source-renamed/settings?saved=1"

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
    assert create_file.headers["location"] == "/ui/testuser/ui-source-renamed/blob/main/src/app.py"

    blob = await client.get("/ui/testuser/ui-source-renamed/blob/main/src/app.py")
    assert blob.status_code == 200
    assert "print(&#39;created&#39;)" in blob.text
    assert "Delete" in blob.text

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

    job_match = re.search(r"/-/jobs/(\d+)", pipeline_page.text)
    assert job_match is not None
    job_id = int(job_match.group(1))

    job_page = await client.get(f"/ui/testuser/ui-ci-repo/-/jobs/{job_id}")
    assert job_page.status_code == 200
    assert f"Job #{job_id}" in job_page.text
    assert f"Pipeline #{pipeline_id}" in job_page.text
    assert "Recent pipelines" not in job_page.text
    assert "Back to pipelines" in job_page.text
    assert "Trace API" in job_page.text
    assert "window.setTimeout" in job_page.text

    cancel_job = await client.post(
        f"/ui/testuser/ui-ci-repo/-/jobs/{job_id}/cancel",
        follow_redirects=False,
    )
    assert cancel_job.status_code in (302, 303)
    assert cancel_job.headers["location"].startswith(f"/ui/testuser/ui-ci-repo/-/jobs/{job_id}")

    canceled_page = await client.get(cancel_job.headers["location"])
    assert canceled_page.status_code == 200
    assert "Job canceled." in canceled_page.text
    assert "canceled" in canceled_page.text
