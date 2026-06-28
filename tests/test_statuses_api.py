"""Tests for commit status authorization."""

import pytest

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
