"""Tests for check run and check suite authorization."""

import pytest

from tests.conftest import API, auth_headers
from tests.test_projects_api import _create_user_and_token


@pytest.mark.asyncio
async def test_check_writes_require_developer_access(client, db_session, test_token):
    reporter, reporter_token = await _create_user_and_token(
        db_session, "check-reporter"
    )
    developer, developer_token = await _create_user_and_token(
        db_session, "check-developer"
    )
    repo = await client.post(
        f"{API}/user/repos",
        json={"name": "check-role-repo", "auto_init": True},
        headers=auth_headers(test_token),
    )
    assert repo.status_code == 201
    project = repo.json()
    for user, level in ((reporter, 20), (developer, 30)):
        member = await client.post(
            f"{API}/projects/{project['id']}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201

    head_sha = "1" * 40
    denied_run = await client.post(
        f"{API}/repos/testuser/check-role-repo/check-runs",
        json={"name": "ci", "head_sha": head_sha},
        headers=auth_headers(reporter_token),
    )
    assert denied_run.status_code == 403

    run = await client.post(
        f"{API}/repos/testuser/check-role-repo/check-runs",
        json={"name": "ci", "head_sha": head_sha},
        headers=auth_headers(developer_token),
    )
    assert run.status_code == 201
    run_id = run.json()["id"]

    denied_update = await client.patch(
        f"{API}/repos/testuser/check-role-repo/check-runs/{run_id}",
        json={"status": "completed", "conclusion": "success"},
        headers=auth_headers(reporter_token),
    )
    assert denied_update.status_code == 403

    update = await client.patch(
        f"{API}/repos/testuser/check-role-repo/check-runs/{run_id}",
        json={"status": "completed", "conclusion": "success"},
        headers=auth_headers(developer_token),
    )
    assert update.status_code == 200
    assert update.json()["conclusion"] == "success"

    denied_suite = await client.post(
        f"{API}/repos/testuser/check-role-repo/check-suites",
        json={"head_sha": "2" * 40},
        headers=auth_headers(reporter_token),
    )
    assert denied_suite.status_code == 403

    suite = await client.post(
        f"{API}/repos/testuser/check-role-repo/check-suites",
        json={"head_sha": "2" * 40},
        headers=auth_headers(developer_token),
    )
    assert suite.status_code == 201
