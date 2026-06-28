"""Tests for GitLab merge request API endpoints."""

from urllib.parse import quote

import pytest

from tests.conftest import API, auth_headers
from tests.test_projects_api import _create_user_and_token


async def _project_with_source_branch(client, test_token, name: str) -> dict:
    project = await client.post(
        f"{API}/projects",
        json={"name": name, "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    data = project.json()

    branch = await client.post(
        f"{API}/projects/{data['id']}/repository/branches",
        json={"branch": "feature", "ref": "main"},
        headers=auth_headers(test_token),
    )
    assert branch.status_code == 201

    file_path = quote("feature.txt", safe="")
    create_file = await client.post(
        f"{API}/projects/{data['id']}/repository/files/{file_path}",
        json={
            "branch": "feature",
            "commit_message": "add feature file",
            "content": "feature\n",
        },
        headers=auth_headers(test_token),
    )
    assert create_file.status_code == 201
    return data


async def _create_mr(client, test_token, project_id: int, title: str = "Add feature"):
    return await client.post(
        f"{API}/projects/{project_id}/merge_requests",
        json={
            "title": title,
            "description": "Merge the feature branch",
            "source_branch": "feature",
            "target_branch": "main",
        },
        headers=auth_headers(test_token),
    )


@pytest.mark.asyncio
async def test_create_merge_request_returns_gitlab_shape(client, test_user, test_token):
    project = await _project_with_source_branch(client, test_token, "mr-create-project")

    resp = await _create_mr(client, test_token, project["id"])

    assert resp.status_code == 201
    data = resp.json()
    assert data["iid"] == 1
    assert data["project_id"] == project["id"]
    assert data["title"] == "Add feature"
    assert data["description"] == "Merge the feature branch"
    assert data["state"] == "opened"
    assert data["source_branch"] == "feature"
    assert data["target_branch"] == "main"
    assert data["source_branch_exists"] is True
    assert data["target_branch_exists"] is True
    assert data["source_project_id"] == project["id"]
    assert data["target_project_id"] == project["id"]
    assert data["author"]["username"] == "testuser"
    assert data["user"]["can_merge"] is True
    assert data["merge_status"] == "can_be_merged"
    assert data["detailed_merge_status"] == "mergeable"
    assert data["changes_count"] is None
    assert data["diff_refs"]["head_sha"] == data["sha"]
    assert data["references"]["full"] == "testuser/mr-create-project!1"
    assert data["web_url"].endswith("/testuser/mr-create-project/-/merge_requests/1")


@pytest.mark.asyncio
async def test_merge_request_routes_accept_encoded_project_path(client, test_token):
    project = await _project_with_source_branch(client, test_token, "mr-path-project")

    create = await client.post(
        f"{API}/projects/testuser%2Fmr-path-project/merge_requests",
        json={
            "title": "Path MR",
            "source_branch": "feature",
            "target_branch": "main",
        },
        headers=auth_headers(test_token),
    )
    assert create.status_code == 201

    get_resp = await client.get(
        f"{API}/projects/testuser%2Fmr-path-project/merge_requests/{create.json()['iid']}",
        headers=auth_headers(test_token),
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["project_id"] == project["id"]
    assert get_resp.json()["title"] == "Path MR"


@pytest.mark.asyncio
async def test_list_and_filter_merge_requests(client, test_token):
    project = await _project_with_source_branch(client, test_token, "mr-list-project")
    first = await _create_mr(client, test_token, project["id"], "First MR")
    assert first.status_code == 201

    close = await client.put(
        f"{API}/projects/{project['id']}/merge_requests/{first.json()['iid']}",
        json={"state_event": "close"},
        headers=auth_headers(test_token),
    )
    assert close.status_code == 200

    opened = await client.get(
        f"{API}/projects/{project['id']}/merge_requests",
        params={"state": "opened"},
        headers=auth_headers(test_token),
    )
    assert opened.status_code == 200
    assert opened.json() == []

    closed = await client.get(
        f"{API}/projects/{project['id']}/merge_requests",
        params={"state": "closed"},
        headers=auth_headers(test_token),
    )
    assert closed.status_code == 200
    assert [mr["title"] for mr in closed.json()] == ["First MR"]
    assert closed.json()[0]["state"] == "closed"
    assert closed.json()[0]["closed_by"]["username"] == "testuser"


@pytest.mark.asyncio
async def test_list_merge_requests_paginates_and_accepts_glab_params(client, test_token):
    project = await _project_with_source_branch(client, test_token, "mr-pagination-project")
    first = await _create_mr(client, test_token, project["id"], "First page MR")
    assert first.status_code == 201
    close = await client.put(
        f"{API}/projects/{project['id']}/merge_requests/{first.json()['iid']}",
        json={"state_event": "close"},
        headers=auth_headers(test_token),
    )
    assert close.status_code == 200
    second = await _create_mr(client, test_token, project["id"], "Second page MR")
    assert second.status_code == 201

    resp = await client.get(
        f"{API}/projects/{project['id']}/merge_requests",
        params={
            "state": "all",
            "page": 1,
            "per_page": 1,
            "order_by": "updated_at",
            "sort": "desc",
            "view": "simple",
            "with_merge_status_recheck": True,
        },
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.headers["X-Total"] == "2"
    assert resp.headers["X-Total-Pages"] == "2"
    assert resp.headers["X-Next-Page"] == "2"


@pytest.mark.asyncio
async def test_update_merge_request(client, test_token):
    project = await _project_with_source_branch(client, test_token, "mr-update-project")
    create = await _create_mr(client, test_token, project["id"])
    assert create.status_code == 201

    resp = await client.put(
        f"{API}/projects/{project['id']}/merge_requests/{create.json()['iid']}",
        json={"title": "Updated MR", "description": "Updated description"},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    assert resp.json()["title"] == "Updated MR"
    assert resp.json()["description"] == "Updated description"


@pytest.mark.asyncio
async def test_update_merge_request_state_reopen_and_branch_validation(client, test_token):
    project = await _project_with_source_branch(client, test_token, "mr-update-state-project")
    create = await _create_mr(client, test_token, project["id"])
    assert create.status_code == 201
    iid = create.json()["iid"]

    close = await client.put(
        f"{API}/projects/{project['id']}/merge_requests/{iid}",
        json={"state_event": "close"},
        headers=auth_headers(test_token),
    )
    assert close.status_code == 200
    assert close.json()["state"] == "closed"

    reopen = await client.put(
        f"{API}/projects/{project['id']}/merge_requests/{iid}",
        json={"state_event": "reopen"},
        headers=auth_headers(test_token),
    )
    assert reopen.status_code == 200
    assert reopen.json()["state"] == "opened"
    assert reopen.json()["closed_by"] is None

    same_branch = await client.put(
        f"{API}/projects/{project['id']}/merge_requests/{iid}",
        json={"target_branch": "feature"},
        headers=auth_headers(test_token),
    )
    assert same_branch.status_code == 400

    missing_branch = await client.put(
        f"{API}/projects/{project['id']}/merge_requests/{iid}",
        json={"source_branch": "does-not-exist"},
        headers=auth_headers(test_token),
    )
    assert missing_branch.status_code == 400


@pytest.mark.asyncio
async def test_merge_request_commits_and_changes(client, test_token):
    project = await _project_with_source_branch(client, test_token, "mr-changes-project")
    create = await _create_mr(client, test_token, project["id"], "Changes MR")
    assert create.status_code == 201
    iid = create.json()["iid"]

    commits = await client.get(
        f"{API}/projects/{project['id']}/merge_requests/{iid}/commits",
        params={"page": 1, "per_page": 1},
        headers=auth_headers(test_token),
    )
    assert commits.status_code == 200
    assert len(commits.json()) >= 1
    assert commits.json()[0]["title"] == "add feature file"
    assert commits.headers["X-Total"] == "1"

    changes = await client.get(
        f"{API}/projects/{project['id']}/merge_requests/{iid}/changes",
        headers=auth_headers(test_token),
    )
    assert changes.status_code == 200
    data = changes.json()
    assert data["iid"] == iid
    assert data["changes_count"] == "1"
    assert any(change["new_path"] == "feature.txt" for change in data["changes"])
    assert data["changes"][0]["diff"].startswith("diff --git")
    assert data["changes"][0]["too_large"] is False
    assert data["changes"][0]["collapsed"] is False
    assert data["overflow"] is False

    diffs = await client.get(
        f"{API}/projects/{project['id']}/merge_requests/{iid}/diffs",
        params={"page": 1, "per_page": 1},
        headers=auth_headers(test_token),
    )
    assert diffs.status_code == 200
    assert diffs.headers["X-Total"] == "1"
    assert diffs.json()[0]["new_path"] == "feature.txt"
    assert diffs.json()[0]["diff"].startswith("diff --git")


@pytest.mark.asyncio
async def test_merge_merge_request(client, test_token):
    project = await _project_with_source_branch(client, test_token, "mr-merge-project")
    create = await _create_mr(client, test_token, project["id"])
    assert create.status_code == 201

    resp = await client.put(
        f"{API}/projects/{project['id']}/merge_requests/{create.json()['iid']}/merge",
        json={},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "merged"
    assert data["merged_at"] is not None
    assert data["merged_by"]["username"] == "testuser"
    assert data["closed_by"]["username"] == "testuser"
    assert data["detailed_merge_status"] == "merged"
    assert len(data["merge_commit_sha"]) == 40


@pytest.mark.asyncio
async def test_merge_merge_request_rejects_stale_sha_and_invalid_method(client, test_token):
    project = await _project_with_source_branch(client, test_token, "mr-merge-guard-project")
    create = await _create_mr(client, test_token, project["id"])
    assert create.status_code == 201
    iid = create.json()["iid"]

    stale = await client.put(
        f"{API}/projects/{project['id']}/merge_requests/{iid}/merge",
        json={"sha": "0" * 40},
        headers=auth_headers(test_token),
    )
    assert stale.status_code == 409

    invalid_method = await client.put(
        f"{API}/projects/{project['id']}/merge_requests/{iid}/merge",
        json={"merge_method": "octopus"},
        headers=auth_headers(test_token),
    )
    assert invalid_method.status_code == 400


@pytest.mark.asyncio
async def test_create_merge_request_rejects_duplicate(client, test_token):
    project = await _project_with_source_branch(client, test_token, "mr-duplicate-project")
    first = await _create_mr(client, test_token, project["id"])
    assert first.status_code == 201

    duplicate = await _create_mr(client, test_token, project["id"])

    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_create_merge_request_rejects_branch_edge_cases(client, test_token):
    project = await _project_with_source_branch(client, test_token, "mr-branch-edge-project")

    same_branch = await client.post(
        f"{API}/projects/{project['id']}/merge_requests",
        json={
            "title": "Same branch",
            "source_branch": "main",
            "target_branch": "main",
        },
        headers=auth_headers(test_token),
    )
    assert same_branch.status_code == 400

    missing_source = await client.post(
        f"{API}/projects/{project['id']}/merge_requests",
        json={
            "title": "Missing source",
            "source_branch": "does-not-exist",
            "target_branch": "main",
        },
        headers=auth_headers(test_token),
    )
    assert missing_source.status_code == 400


@pytest.mark.asyncio
async def test_create_merge_request_requires_auth(client, test_token):
    project = await _project_with_source_branch(client, test_token, "mr-auth-project")

    resp = await client.post(
        f"{API}/projects/{project['id']}/merge_requests",
        json={
            "title": "No auth",
            "source_branch": "feature",
            "target_branch": "main",
        },
    )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_merge_request_writes_require_developer(
    client, db_session, test_user, test_token
):
    developer, developer_token = await _create_user_and_token(
        db_session, "mr-role-developer"
    )
    reporter, reporter_token = await _create_user_and_token(
        db_session, "mr-role-reporter"
    )
    project = await _project_with_source_branch(
        client, test_token, "mr-role-boundary-project"
    )
    project_id = project["id"]

    for user, level in ((developer, 30), (reporter, 20)):
        member = await client.post(
            f"{API}/projects/{project_id}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201

    denied_create = await client.post(
        f"{API}/projects/{project_id}/merge_requests",
        json={
            "title": "Reporter denied",
            "source_branch": "feature",
            "target_branch": "main",
        },
        headers=auth_headers(reporter_token),
    )
    assert denied_create.status_code == 403

    allowed_create = await client.post(
        f"{API}/projects/{project_id}/merge_requests",
        json={
            "title": "Developer allowed",
            "source_branch": "feature",
            "target_branch": "main",
        },
        headers=auth_headers(developer_token),
    )
    assert allowed_create.status_code == 201
    iid = allowed_create.json()["iid"]

    denied_update = await client.put(
        f"{API}/projects/{project_id}/merge_requests/{iid}",
        json={"title": "Reporter denied update"},
        headers=auth_headers(reporter_token),
    )
    assert denied_update.status_code == 403

    allowed_update = await client.put(
        f"{API}/projects/{project_id}/merge_requests/{iid}",
        json={"title": "Developer allowed update"},
        headers=auth_headers(developer_token),
    )
    assert allowed_update.status_code == 200
    assert allowed_update.json()["title"] == "Developer allowed update"

    denied_merge = await client.put(
        f"{API}/projects/{project_id}/merge_requests/{iid}/merge",
        json={},
        headers=auth_headers(reporter_token),
    )
    assert denied_merge.status_code == 403
