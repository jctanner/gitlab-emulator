"""Tests for Git data object write authorization."""

import pytest

from tests.conftest import API, auth_headers
from tests.test_projects_api import _create_user_and_token


@pytest.mark.asyncio
async def test_git_data_writes_require_developer_access(
    client, db_session, test_token
):
    reporter, reporter_token = await _create_user_and_token(
        db_session, "git-data-reporter"
    )
    developer, developer_token = await _create_user_and_token(
        db_session, "git-data-developer"
    )
    repo = await client.post(
        f"{API}/user/repos",
        json={"name": "git-data-role-repo", "auto_init": True},
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

    denied_blob = await client.post(
        f"{API}/repos/testuser/git-data-role-repo/git/blobs",
        json={"content": "reporter denied"},
        headers=auth_headers(reporter_token),
    )
    assert denied_blob.status_code == 403

    blob = await client.post(
        f"{API}/repos/testuser/git-data-role-repo/git/blobs",
        json={"content": "developer allowed"},
        headers=auth_headers(developer_token),
    )
    assert blob.status_code == 201
    blob_sha = blob.json()["sha"]

    denied_tree = await client.post(
        f"{API}/repos/testuser/git-data-role-repo/git/trees",
        json={
            "tree": [
                {
                    "path": "developer.txt",
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_sha,
                }
            ]
        },
        headers=auth_headers(reporter_token),
    )
    assert denied_tree.status_code == 403

    tree = await client.post(
        f"{API}/repos/testuser/git-data-role-repo/git/trees",
        json={
            "tree": [
                {
                    "path": "developer.txt",
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_sha,
                }
            ]
        },
        headers=auth_headers(developer_token),
    )
    assert tree.status_code == 201
    tree_sha = tree.json()["sha"]

    denied_commit = await client.post(
        f"{API}/repos/testuser/git-data-role-repo/git/commits",
        json={"message": "reporter denied", "tree": tree_sha},
        headers=auth_headers(reporter_token),
    )
    assert denied_commit.status_code == 403

    commit = await client.post(
        f"{API}/repos/testuser/git-data-role-repo/git/commits",
        json={"message": "developer commit", "tree": tree_sha},
        headers=auth_headers(developer_token),
    )
    assert commit.status_code == 201
    commit_sha = commit.json()["sha"]

    denied_tag = await client.post(
        f"{API}/repos/testuser/git-data-role-repo/git/tags",
        json={"tag": "v0-denied", "object": commit_sha},
        headers=auth_headers(reporter_token),
    )
    assert denied_tag.status_code == 403

    tag = await client.post(
        f"{API}/repos/testuser/git-data-role-repo/git/tags",
        json={"tag": "v1", "object": commit_sha},
        headers=auth_headers(developer_token),
    )
    assert tag.status_code == 201
