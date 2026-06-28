"""Tests for GitHub-compatible commit endpoints."""

import pytest

from tests.conftest import auth_headers

API = "/api/v4"


@pytest.mark.asyncio
async def test_compare_commits_returns_git_backed_data(client, test_user, test_token):
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "legacy-compare-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project_resp.status_code == 201
    project = project_resp.json()

    branch_resp = await client.post(
        f"{API}/projects/{project['id']}/repository/branches",
        json={"branch": "feature", "ref": "main"},
        headers=auth_headers(test_token),
    )
    assert branch_resp.status_code == 201
    base_sha = branch_resp.json()["commit"]["id"]

    file_resp = await client.post(
        f"{API}/projects/{project['id']}/repository/files/src%2Ffeature.txt",
        json={
            "branch": "feature",
            "commit_message": "add compare feature",
            "content": "one\ntwo\n",
        },
        headers=auth_headers(test_token),
    )
    assert file_resp.status_code == 201
    head_sha = file_resp.json()["commit_id"]

    compare = await client.get(
        f"{API}/repos/testuser/legacy-compare-project/compare/main...feature",
        headers=auth_headers(test_token),
    )

    assert compare.status_code == 200
    data = compare.json()
    assert data["status"] == "ahead"
    assert data["ahead_by"] == 1
    assert data["behind_by"] == 0
    assert data["total_commits"] == 1
    assert data["base_commit"]["sha"] == base_sha
    assert data["merge_base_commit"]["sha"] == base_sha
    assert data["commits"][0]["sha"] == head_sha
    assert data["commits"][0]["commit"]["message"] == "add compare feature"
    assert data["files"] == [
        {
            "sha": head_sha,
            "filename": "src/feature.txt",
            "status": "added",
            "additions": 2,
            "deletions": 0,
            "changes": 2,
            "blob_url": data["files"][0]["blob_url"],
            "raw_url": data["files"][0]["raw_url"],
            "contents_url": data["files"][0]["contents_url"],
        }
    ]
    assert data["files"][0]["blob_url"].endswith(f"/blob/{head_sha}/src/feature.txt")
    assert data["files"][0]["contents_url"].endswith(
        f"/contents/src/feature.txt?ref={head_sha}"
    )


@pytest.mark.asyncio
async def test_compare_commits_rejects_malformed_basehead(
    client, test_user, test_token
):
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "legacy-compare-bad", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project_resp.status_code == 201

    compare = await client.get(
        f"{API}/repos/testuser/legacy-compare-bad/compare/main..feature",
        headers=auth_headers(test_token),
    )

    assert compare.status_code == 422
