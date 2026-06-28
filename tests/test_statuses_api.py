"""Tests for commit status authorization."""

import pytest
from urllib.parse import quote

from tests.conftest import API, auth_headers
from tests.test_projects_api import _create_user_and_token


@pytest.mark.asyncio
async def test_commit_status_writes_require_developer(
    client, db_session, test_user, test_token, test_repo_with_init
):
    owner, repo_name, repo_data = test_repo_with_init
    developer, developer_token = await _create_user_and_token(
        db_session, "status-developer"
    )
    reporter, reporter_token = await _create_user_and_token(
        db_session, "status-reporter"
    )
    for user, level in ((developer, 30), (reporter, 20)):
        member = await client.post(
            f"{API}/projects/{repo_data['id']}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201

    ref = await client.get(
        f"{API}/repos/{owner}/{repo_name}/git/ref/heads/main",
        headers=auth_headers(test_token),
    )
    assert ref.status_code == 200
    sha = ref.json()["object"]["sha"]

    denied = await client.post(
        f"{API}/repos/{owner}/{repo_name}/statuses/{sha}",
        json={"state": "success", "context": "reporter"},
        headers=auth_headers(reporter_token),
    )
    assert denied.status_code == 403

    allowed = await client.post(
        f"{API}/repos/{owner}/{repo_name}/statuses/{sha}",
        json={"state": "success", "context": "developer"},
        headers=auth_headers(developer_token),
    )
    assert allowed.status_code == 201


@pytest.mark.asyncio
async def test_gitlab_commit_statuses_create_and_list_by_project(
    client, test_token, test_repo_with_init
):
    owner, repo_name, repo_data = test_repo_with_init
    ref = await client.get(
        f"{API}/repos/{owner}/{repo_name}/git/ref/heads/main",
        headers=auth_headers(test_token),
    )
    assert ref.status_code == 200
    sha = ref.json()["object"]["sha"]

    created = await client.post(
        f"{API}/projects/{repo_data['id']}/statuses/{sha}",
        json={
            "state": "running",
            "name": "integration",
            "description": "integration suite started",
            "target_url": "https://ci.example.test/runs/1",
        },
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["sha"] == sha
    assert payload["status"] == "running"
    assert payload["name"] == "integration"
    assert payload["context"] == "integration"
    assert payload["description"] == "integration suite started"
    assert payload["target_url"] == "https://ci.example.test/runs/1"
    assert payload["project_id"] == repo_data["id"]
    assert payload["author"]["username"] == "testuser"

    failed = await client.post(
        f"{API}/projects/{repo_data['id']}/statuses/{sha}",
        json={"state": "failed", "context": "lint"},
        headers=auth_headers(test_token),
    )
    assert failed.status_code == 201
    assert failed.json()["status"] == "failed"
    assert failed.json()["name"] == "lint"
    assert failed.json()["finished_at"] is not None

    project_ref = quote(f"{owner}/{repo_name}", safe="")
    listed = await client.get(
        f"{API}/projects/{project_ref}/repository/commits/{sha}/statuses",
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    statuses = listed.json()
    assert [status["name"] for status in statuses] == ["lint", "integration"]
    assert [status["status"] for status in statuses] == ["failed", "running"]


@pytest.mark.asyncio
async def test_gitlab_commit_status_writes_require_developer(
    client, db_session, test_token, test_repo_with_init
):
    owner, repo_name, repo_data = test_repo_with_init
    reporter, reporter_token = await _create_user_and_token(
        db_session, "gitlab-status-reporter"
    )
    member = await client.post(
        f"{API}/projects/{repo_data['id']}/members",
        json={"user_id": reporter.id, "access_level": 20},
        headers=auth_headers(test_token),
    )
    assert member.status_code == 201

    ref = await client.get(
        f"{API}/repos/{owner}/{repo_name}/git/ref/heads/main",
        headers=auth_headers(test_token),
    )
    assert ref.status_code == 200
    sha = ref.json()["object"]["sha"]

    denied = await client.post(
        f"{API}/projects/{repo_data['id']}/statuses/{sha}",
        json={"state": "success", "context": "reporter"},
        headers=auth_headers(reporter_token),
    )
    assert denied.status_code == 403

    invalid = await client.post(
        f"{API}/projects/{repo_data['id']}/statuses/{sha}",
        json={"state": "failure", "context": "github-state"},
        headers=auth_headers(test_token),
    )
    assert invalid.status_code == 400
    assert "Invalid status state" in invalid.text
