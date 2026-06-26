"""Tests for GitLab repository commits API endpoints."""

from urllib.parse import quote

import pytest

from tests.conftest import API, auth_headers


async def _create_project_with_file_commits(client, test_token, name: str) -> dict:
    project = await client.post(
        f"{API}/projects",
        json={"name": name, "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    data = project.json()

    file_path = quote("docs/commit-api.txt", safe="")
    create = await client.post(
        f"{API}/projects/{data['id']}/repository/files/{file_path}",
        json={
            "branch": "main",
            "commit_message": "create commit api file",
            "content": "first\n",
        },
        headers=auth_headers(test_token),
    )
    assert create.status_code == 201

    update = await client.put(
        f"{API}/projects/{data['id']}/repository/files/{file_path}",
        json={
            "branch": "main",
            "commit_message": "update commit api file",
            "content": "second\n",
        },
        headers=auth_headers(test_token),
    )
    assert update.status_code == 200
    data["file_path"] = file_path
    data["create_commit_id"] = create.json()["commit_id"]
    data["update_commit_id"] = update.json()["commit_id"]
    return data


@pytest.mark.asyncio
async def test_list_repository_commits_by_project_id(client, test_token):
    project = await _create_project_with_file_commits(
        client, test_token, "commits-list-project"
    )

    resp = await client.get(
        f"{API}/projects/{project['id']}/repository/commits",
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    commits = resp.json()
    assert len(commits) == 3
    assert commits[0]["id"] == project["update_commit_id"]
    assert commits[0]["short_id"] == project["update_commit_id"][:8]
    assert commits[0]["title"] == "update commit api file"
    assert commits[0]["message"] == "update commit api file"
    assert commits[0]["parent_ids"] == [project["create_commit_id"]]
    assert commits[0]["author_name"] == "GitLab Emulator"
    assert commits[0]["web_url"].endswith(f"/-/commit/{project['update_commit_id']}")


@pytest.mark.asyncio
async def test_list_repository_commits_by_encoded_project_path(client, test_token):
    await _create_project_with_file_commits(
        client, test_token, "commits-path-project"
    )

    resp = await client.get(
        f"{API}/projects/testuser%2Fcommits-path-project/repository/commits",
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    assert resp.json()[0]["title"] == "update commit api file"


@pytest.mark.asyncio
async def test_get_repository_commit_by_encoded_project_path(client, test_token):
    project = await _create_project_with_file_commits(
        client, test_token, "commits-get-path-project"
    )

    resp = await client.get(
        f"{API}/projects/testuser%2Fcommits-get-path-project/repository/commits/{project['create_commit_id']}",
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    assert resp.json()["title"] == "create commit api file"


@pytest.mark.asyncio
async def test_list_repository_commits_supports_path_filter(client, test_token):
    project = await _create_project_with_file_commits(
        client, test_token, "commits-path-filter-project"
    )

    resp = await client.get(
        f"{API}/projects/{project['id']}/repository/commits",
        params={"path": "docs/commit-api.txt"},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    commits = resp.json()
    assert [commit["id"] for commit in commits] == [
        project["update_commit_id"],
        project["create_commit_id"],
    ]


@pytest.mark.asyncio
async def test_list_repository_commits_supports_ref_alias(client, test_token):
    project = await _create_project_with_file_commits(
        client, test_token, "commits-ref-filter-project"
    )

    resp = await client.get(
        f"{API}/projects/{project['id']}/repository/commits",
        params={"ref": project["create_commit_id"]},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    commits = resp.json()
    assert commits[0]["id"] == project["create_commit_id"]
    assert project["update_commit_id"] not in [commit["id"] for commit in commits]


@pytest.mark.asyncio
async def test_list_repository_commits_supports_since_until_filters(client, test_token):
    project = await _create_project_with_file_commits(
        client, test_token, "commits-date-filter-project"
    )

    resp = await client.get(
        f"{API}/projects/{project['id']}/repository/commits",
        params={"since": "2000-01-01", "until": "2001-01-01"},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    assert resp.json() == []
    assert resp.headers["X-Total"] == "0"


@pytest.mark.asyncio
async def test_list_repository_commits_pagination_headers(client, test_token):
    project = await _create_project_with_file_commits(
        client, test_token, "commits-pagination-project"
    )

    resp = await client.get(
        f"{API}/projects/{project['id']}/repository/commits",
        params={"page": 2, "per_page": 1},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    assert [commit["id"] for commit in resp.json()] == [project["create_commit_id"]]
    assert resp.headers["X-Total"] == "3"
    assert resp.headers["X-Total-Pages"] == "3"
    assert resp.headers["X-Page"] == "2"
    assert resp.headers["X-Per-Page"] == "1"
    assert resp.headers["X-Prev-Page"] == "1"
    assert resp.headers["X-Next-Page"] == "3"
    assert 'rel="prev"' in resp.headers["Link"]
    assert 'rel="next"' in resp.headers["Link"]


@pytest.mark.asyncio
async def test_get_repository_commit(client, test_token):
    project = await _create_project_with_file_commits(
        client, test_token, "commits-get-project"
    )

    resp = await client.get(
        f"{API}/projects/{project['id']}/repository/commits/{project['create_commit_id']}",
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    commit = resp.json()
    assert commit["id"] == project["create_commit_id"]
    assert commit["title"] == "create commit api file"
    assert commit["parent_ids"]
    assert "T" in commit["committed_date"]


@pytest.mark.asyncio
async def test_get_repository_commit_supports_stats(client, test_token):
    project = await _create_project_with_file_commits(
        client, test_token, "commits-stats-project"
    )

    resp = await client.get(
        f"{API}/projects/{project['id']}/repository/commits/{project['create_commit_id']}",
        params={"stats": True},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    assert resp.json()["stats"] == {"additions": 1, "deletions": 0, "total": 1}


@pytest.mark.asyncio
async def test_get_repository_commit_diff(client, test_token):
    project = await _create_project_with_file_commits(
        client, test_token, "commits-diff-project"
    )

    resp = await client.get(
        f"{API}/projects/{project['id']}/repository/commits/{project['create_commit_id']}/diff",
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    diffs = resp.json()
    assert diffs == [
        {
            "old_path": "docs/commit-api.txt",
            "new_path": "docs/commit-api.txt",
            "a_mode": "000000",
            "b_mode": "100644",
            "diff": "",
            "new_file": True,
            "renamed_file": False,
            "deleted_file": False,
        }
    ]


@pytest.mark.asyncio
async def test_get_repository_commit_diff_marks_deleted_files(client, test_token):
    project = await _create_project_with_file_commits(
        client, test_token, "commits-delete-diff-project"
    )
    delete = await client.request(
        "DELETE",
        f"{API}/projects/{project['id']}/repository/files/{project['file_path']}",
        json={"branch": "main", "commit_message": "delete commit api file"},
        headers=auth_headers(test_token),
    )
    assert delete.status_code == 200

    resp = await client.get(
        f"{API}/projects/{project['id']}/repository/commits/{delete.json()['commit_id']}/diff",
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    assert resp.json() == [
        {
            "old_path": "docs/commit-api.txt",
            "new_path": "docs/commit-api.txt",
            "a_mode": "100644",
            "b_mode": "000000",
            "diff": "",
            "new_file": False,
            "renamed_file": False,
            "deleted_file": True,
        }
    ]


@pytest.mark.asyncio
async def test_get_repository_commit_not_found(client, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "commits-missing-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201

    resp = await client.get(
        f"{API}/projects/{project.json()['id']}/repository/commits/deadbeef",
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 404
