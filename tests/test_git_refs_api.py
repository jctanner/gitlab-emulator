"""Tests for Git refs authorization."""

import pytest

from tests.conftest import API, auth_headers
from tests.test_projects_api import _create_user_and_token


@pytest.mark.asyncio
async def test_git_refs_write_requires_developer(
    client, db_session, test_user, test_token, test_repo_with_init
):
    owner, repo_name, repo_data = test_repo_with_init
    developer, developer_token = await _create_user_and_token(
        db_session, "refs-developer"
    )
    reporter, reporter_token = await _create_user_and_token(
        db_session, "refs-reporter"
    )
    for user, level in ((developer, 30), (reporter, 20)):
        member = await client.post(
            f"{API}/projects/{repo_data['id']}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201

    main_ref = await client.get(
        f"{API}/repos/{owner}/{repo_name}/git/ref/heads/main",
        headers=auth_headers(test_token),
    )
    assert main_ref.status_code == 200
    main_sha = main_ref.json()["object"]["sha"]

    allowed = await client.post(
        f"{API}/repos/{owner}/{repo_name}/git/refs",
        json={"ref": "refs/heads/developer-ref", "sha": main_sha},
        headers=auth_headers(developer_token),
    )
    assert allowed.status_code == 201

    denied = await client.post(
        f"{API}/repos/{owner}/{repo_name}/git/refs",
        json={"ref": "refs/heads/reporter-ref", "sha": main_sha},
        headers=auth_headers(reporter_token),
    )
    assert denied.status_code == 403
