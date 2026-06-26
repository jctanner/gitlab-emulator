"""Tests for the Issues REST API endpoints."""

import pytest

from tests.conftest import auth_headers

API = "/api/v4"


@pytest.fixture
async def repo_with_issues(client, test_user, test_token):
    """Create a repo for issue tests."""
    resp = await client.post(
        f"{API}/user/repos",
        json={"name": "issue-repo"},
        headers=auth_headers(test_token),
    )
    return resp.json()


@pytest.mark.asyncio
async def test_create_issue(client, test_user, test_token, repo_with_issues):
    """POST /repos/{owner}/{repo}/issues creates an issue."""
    resp = await client.post(
        f"{API}/repos/testuser/issue-repo/issues",
        json={"title": "Bug report", "body": "Something is broken"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Bug report"
    assert data["body"] == "Something is broken"
    assert data["state"] == "open"
    assert data["number"] == 1
    assert data["user"]["login"] == "testuser"


@pytest.mark.asyncio
async def test_issue_numbering(client, test_user, test_token, repo_with_issues):
    """Issues are numbered sequentially."""
    resp1 = await client.post(
        f"{API}/repos/testuser/issue-repo/issues",
        json={"title": "First"},
        headers=auth_headers(test_token),
    )
    resp2 = await client.post(
        f"{API}/repos/testuser/issue-repo/issues",
        json={"title": "Second"},
        headers=auth_headers(test_token),
    )
    assert resp1.json()["number"] == 1
    assert resp2.json()["number"] == 2


@pytest.mark.asyncio
async def test_get_issue(client, test_user, test_token, repo_with_issues):
    """GET /repos/{owner}/{repo}/issues/{number} returns the issue."""
    await client.post(
        f"{API}/repos/testuser/issue-repo/issues",
        json={"title": "Get test"},
        headers=auth_headers(test_token),
    )
    resp = await client.get(f"{API}/repos/testuser/issue-repo/issues/1")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Get test"


@pytest.mark.asyncio
async def test_update_issue(client, test_user, test_token, repo_with_issues):
    """PATCH /repos/{owner}/{repo}/issues/{number} updates the issue."""
    await client.post(
        f"{API}/repos/testuser/issue-repo/issues",
        json={"title": "Original"},
        headers=auth_headers(test_token),
    )
    resp = await client.patch(
        f"{API}/repos/testuser/issue-repo/issues/1",
        json={"title": "Updated", "state": "closed"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Updated"
    assert data["state"] == "closed"


@pytest.mark.asyncio
async def test_list_issues(client, test_user, test_token, repo_with_issues):
    """GET /repos/{owner}/{repo}/issues lists issues."""
    for i in range(3):
        await client.post(
            f"{API}/repos/testuser/issue-repo/issues",
            json={"title": f"Issue {i+1}"},
            headers=auth_headers(test_token),
        )
    resp = await client.get(f"{API}/repos/testuser/issue-repo/issues")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3


@pytest.mark.asyncio
async def test_list_issues_filter_state(client, test_user, test_token, repo_with_issues):
    """List issues can filter by state."""
    await client.post(
        f"{API}/repos/testuser/issue-repo/issues",
        json={"title": "Open issue"},
        headers=auth_headers(test_token),
    )
    await client.post(
        f"{API}/repos/testuser/issue-repo/issues",
        json={"title": "Closed issue"},
        headers=auth_headers(test_token),
    )
    await client.patch(
        f"{API}/repos/testuser/issue-repo/issues/2",
        json={"state": "closed"},
        headers=auth_headers(test_token),
    )

    resp = await client.get(f"{API}/repos/testuser/issue-repo/issues?state=open")
    assert len(resp.json()) == 1

    resp = await client.get(f"{API}/repos/testuser/issue-repo/issues?state=closed")
    assert len(resp.json()) == 1

    resp = await client.get(f"{API}/repos/testuser/issue-repo/issues?state=all")
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_gitlab_project_issue_crud(client, test_user, test_token, repo_with_issues):
    """GitLab-shaped project issue endpoints work by project ID."""
    resp = await client.post(
        f"{API}/projects/{repo_with_issues['id']}/issues",
        json={
            "title": "GitLab issue",
            "description": "Project issue body",
            "labels": "bug,backend",
            "assignee_ids": [test_user.id],
        },
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["iid"] == 1
    assert data["project_id"] == repo_with_issues["id"]
    assert data["description"] == "Project issue body"
    assert data["state"] == "opened"
    assert data["author"]["username"] == "testuser"
    assert data["assignees"][0]["username"] == "testuser"
    assert data["labels"] == ["bug", "backend"]
    assert data["web_url"].endswith("/testuser/issue-repo/-/issues/1")
    assert data["references"]["full"] == "testuser/issue-repo#1"
    assert "repository_url" not in data
    assert "html_url" not in data
    assert "pull_request" not in data

    get_resp = await client.get(f"{API}/projects/{repo_with_issues['id']}/issues/1")
    assert get_resp.status_code == 200
    assert get_resp.json()["title"] == "GitLab issue"

    update_resp = await client.put(
        f"{API}/projects/{repo_with_issues['id']}/issues/1",
        json={"title": "Closed GitLab issue", "state_event": "close"},
        headers=auth_headers(test_token),
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["title"] == "Closed GitLab issue"
    assert update_resp.json()["state"] == "closed"
    assert update_resp.json()["closed_by"]["username"] == "testuser"

    list_closed = await client.get(
        f"{API}/projects/{repo_with_issues['id']}/issues?state=closed"
    )
    assert list_closed.status_code == 200
    assert [issue["iid"] for issue in list_closed.json()] == [1]


@pytest.mark.asyncio
async def test_gitlab_project_issues_accept_url_encoded_project_path(
    client,
    test_user,
    test_token,
    repo_with_issues,
):
    """GitLab-shaped project issue endpoints accept URL-encoded path refs."""
    project_path = "testuser%2Fissue-repo"
    resp = await client.post(
        f"{API}/projects/{project_path}/issues",
        json={"title": "Path issue"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    assert resp.json()["project_id"] == repo_with_issues["id"]

    list_resp = await client.get(f"{API}/projects/{project_path}/issues")
    assert list_resp.status_code == 200
    assert list_resp.headers["x-total"] == "1"
    assert list_resp.json()[0]["title"] == "Path issue"
