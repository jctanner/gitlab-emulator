"""Tests for the Milestone REST API endpoints."""

from urllib.parse import quote

import pytest

from tests.conftest import auth_headers
from tests.test_projects_api import _create_user_and_token

API = "/api/v4"


@pytest.mark.asyncio
async def test_gitlab_project_milestones_crud_and_pagination(
    client, test_user, test_token
):
    """GitLab-shaped project milestones support CRUD, filters, and pagination."""
    project = await client.post(
        f"{API}/user/repos",
        json={"name": "gitlab-milestones"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    first = await client.post(
        f"{API}/projects/{project_id}/milestones",
        json={
            "title": "v1.0",
            "description": "First GitLab milestone",
            "due_on": "2026-07-01",
        },
        headers=auth_headers(test_token),
    )
    assert first.status_code == 201
    first_data = first.json()
    assert first_data["iid"] == 1
    assert first_data["project_id"] == project_id
    assert first_data["due_date"] == "2026-07-01"
    assert first_data["web_url"].endswith("/testuser/gitlab-milestones/-/milestones/1")

    second = await client.post(
        f"{API}/projects/{project_id}/milestones",
        json={"title": "v2.0"},
        headers=auth_headers(test_token),
    )
    assert second.status_code == 201

    listed = await client.get(
        f"{API}/projects/{project_id}/milestones",
        params={"state": "active", "search": "v", "page": 1, "per_page": 1},
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert listed.headers["x-total"] == "2"
    assert listed.headers["x-next-page"] == "2"
    assert 'rel="next"' in listed.headers["link"]
    assert len(listed.json()) == 1

    encoded = quote("testuser/gitlab-milestones", safe="")
    fetched = await client.get(
        f"{API}/projects/{encoded}/milestones/{first_data['id']}",
        headers=auth_headers(test_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "v1.0"

    updated = await client.put(
        f"{API}/projects/{project_id}/milestones/{first_data['id']}",
        json={
            "title": "v1.0 closed",
            "state": "closed",
            "due_on": "2026-07-15",
        },
        headers=auth_headers(test_token),
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "v1.0 closed"
    assert updated.json()["state"] == "closed"
    assert updated.json()["due_date"] == "2026-07-15"

    closed = await client.get(
        f"{API}/projects/{project_id}/milestones",
        params={"state": "closed"},
        headers=auth_headers(test_token),
    )
    assert closed.status_code == 200
    assert [item["title"] for item in closed.json()] == ["v1.0 closed"]

    deleted = await client.delete(
        f"{API}/projects/{project_id}/milestones/{first_data['id']}",
        headers=auth_headers(test_token),
    )
    assert deleted.status_code == 204

    missing = await client.get(
        f"{API}/projects/{project_id}/milestones/{first_data['id']}",
        headers=auth_headers(test_token),
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_gitlab_project_milestone_lookup_prefers_global_id(
    client, test_user, test_token
):
    """Milestone lookup prefers GitLab's global milestone ID over project IID."""
    warmup = await client.post(
        f"{API}/user/repos",
        json={"name": "milestone-warmup"},
        headers=auth_headers(test_token),
    )
    assert warmup.status_code == 201
    warmup_ms = await client.post(
        f"{API}/projects/{warmup.json()['id']}/milestones",
        json={"title": "warmup"},
        headers=auth_headers(test_token),
    )
    assert warmup_ms.status_code == 201

    project = await client.post(
        f"{API}/user/repos",
        json={"name": "milestone-id-precedence"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    first = await client.post(
        f"{API}/projects/{project_id}/milestones",
        json={"title": "global-id-target"},
        headers=auth_headers(test_token),
    )
    assert first.status_code == 201
    second = await client.post(
        f"{API}/projects/{project_id}/milestones",
        json={"title": "iid-collision"},
        headers=auth_headers(test_token),
    )
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["iid"]

    fetched = await client.get(
        f"{API}/projects/{project_id}/milestones/{first.json()['id']}",
        headers=auth_headers(test_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "global-id-target"


@pytest.mark.asyncio
async def test_create_milestone(client, test_user, test_token):
    """POST /repos/{owner}/{repo}/milestones creates a milestone."""
    await client.post(
        f"{API}/user/repos", json={"name": "ms-repo"}, headers=auth_headers(test_token)
    )
    resp = await client.post(
        f"{API}/repos/testuser/ms-repo/milestones",
        json={"title": "v1.0", "description": "First release"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "v1.0"
    assert data["number"] == 1
    assert data["state"] == "open"
    assert data["description"] == "First release"


@pytest.mark.asyncio
async def test_milestone_writes_require_maintainer(
    client, db_session, test_user, test_token
):
    maintainer, maintainer_token = await _create_user_and_token(
        db_session, "milestone-maintainer"
    )
    developer, developer_token = await _create_user_and_token(
        db_session, "milestone-developer"
    )
    project = await client.post(
        f"{API}/user/repos",
        json={"name": "milestone-role-gate"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    for user, level in ((maintainer, 40), (developer, 30)):
        member = await client.post(
            f"{API}/projects/{project.json()['id']}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201

    denied = await client.post(
        f"{API}/repos/testuser/milestone-role-gate/milestones",
        json={"title": "developer"},
        headers=auth_headers(developer_token),
    )
    assert denied.status_code == 403

    allowed = await client.post(
        f"{API}/repos/testuser/milestone-role-gate/milestones",
        json={"title": "maintainer"},
        headers=auth_headers(maintainer_token),
    )
    assert allowed.status_code == 201


@pytest.mark.asyncio
async def test_list_milestones(client, test_user, test_token):
    """GET /repos/{owner}/{repo}/milestones lists milestones."""
    await client.post(
        f"{API}/user/repos", json={"name": "ms-list"}, headers=auth_headers(test_token)
    )
    await client.post(
        f"{API}/repos/testuser/ms-list/milestones",
        json={"title": "v1.0"},
        headers=auth_headers(test_token),
    )
    await client.post(
        f"{API}/repos/testuser/ms-list/milestones",
        json={"title": "v2.0"},
        headers=auth_headers(test_token),
    )
    resp = await client.get(f"{API}/repos/testuser/ms-list/milestones")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2


@pytest.mark.asyncio
async def test_get_milestone(client, test_user, test_token):
    """GET /repos/{owner}/{repo}/milestones/{number} returns milestone."""
    await client.post(
        f"{API}/user/repos", json={"name": "ms-get"}, headers=auth_headers(test_token)
    )
    await client.post(
        f"{API}/repos/testuser/ms-get/milestones",
        json={"title": "v1.0"},
        headers=auth_headers(test_token),
    )
    resp = await client.get(f"{API}/repos/testuser/ms-get/milestones/1")
    assert resp.status_code == 200
    assert resp.json()["title"] == "v1.0"


@pytest.mark.asyncio
async def test_get_milestone_not_found(client, test_user, test_token):
    """GET /repos/{owner}/{repo}/milestones/{number} returns 404 for missing."""
    await client.post(
        f"{API}/user/repos", json={"name": "ms-404"}, headers=auth_headers(test_token)
    )
    resp = await client.get(f"{API}/repos/testuser/ms-404/milestones/99")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_milestone(client, test_user, test_token):
    """PATCH /repos/{owner}/{repo}/milestones/{number} updates milestone."""
    await client.post(
        f"{API}/user/repos", json={"name": "ms-upd"}, headers=auth_headers(test_token)
    )
    await client.post(
        f"{API}/repos/testuser/ms-upd/milestones",
        json={"title": "v1.0"},
        headers=auth_headers(test_token),
    )
    resp = await client.patch(
        f"{API}/repos/testuser/ms-upd/milestones/1",
        json={"title": "v1.1", "state": "closed"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "v1.1"
    assert data["state"] == "closed"


@pytest.mark.asyncio
async def test_delete_milestone(client, test_user, test_token):
    """DELETE /repos/{owner}/{repo}/milestones/{number} removes milestone."""
    await client.post(
        f"{API}/user/repos", json={"name": "ms-del"}, headers=auth_headers(test_token)
    )
    await client.post(
        f"{API}/repos/testuser/ms-del/milestones",
        json={"title": "v1.0"},
        headers=auth_headers(test_token),
    )
    resp = await client.delete(
        f"{API}/repos/testuser/ms-del/milestones/1",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 204

    # Verify gone
    resp = await client.get(f"{API}/repos/testuser/ms-del/milestones/1")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_milestone_numbering(client, test_user, test_token):
    """Milestones get auto-incrementing numbers per repo."""
    await client.post(
        f"{API}/user/repos", json={"name": "ms-num"}, headers=auth_headers(test_token)
    )
    r1 = await client.post(
        f"{API}/repos/testuser/ms-num/milestones",
        json={"title": "First"},
        headers=auth_headers(test_token),
    )
    r2 = await client.post(
        f"{API}/repos/testuser/ms-num/milestones",
        json={"title": "Second"},
        headers=auth_headers(test_token),
    )
    assert r1.json()["number"] == 1
    assert r2.json()["number"] == 2


@pytest.mark.asyncio
async def test_milestone_with_due_date(client, test_user, test_token):
    """Milestones support due_on field."""
    await client.post(
        f"{API}/user/repos", json={"name": "ms-due"}, headers=auth_headers(test_token)
    )
    resp = await client.post(
        f"{API}/repos/testuser/ms-due/milestones",
        json={"title": "v1.0", "due_on": "2025-12-31T00:00:00Z"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    assert resp.json()["due_on"] is not None


@pytest.mark.asyncio
async def test_list_milestones_filter_state(client, test_user, test_token):
    """Milestones can be filtered by state."""
    await client.post(
        f"{API}/user/repos", json={"name": "ms-filter"}, headers=auth_headers(test_token)
    )
    await client.post(
        f"{API}/repos/testuser/ms-filter/milestones",
        json={"title": "Open"},
        headers=auth_headers(test_token),
    )
    await client.post(
        f"{API}/repos/testuser/ms-filter/milestones",
        json={"title": "Closed", "state": "closed"},
        headers=auth_headers(test_token),
    )
    resp = await client.get(f"{API}/repos/testuser/ms-filter/milestones?state=open")
    assert resp.status_code == 200
    data = resp.json()
    assert all(m["state"] == "open" for m in data)


@pytest.mark.asyncio
async def test_milestone_response_format(client, test_user, test_token):
    """Milestone response has required fields."""
    await client.post(
        f"{API}/user/repos", json={"name": "ms-fmt"}, headers=auth_headers(test_token)
    )
    resp = await client.post(
        f"{API}/repos/testuser/ms-fmt/milestones",
        json={"title": "v1.0"},
        headers=auth_headers(test_token),
    )
    data = resp.json()
    for field in ["id", "number", "title", "state", "url", "html_url",
                  "created_at", "updated_at", "open_issues", "closed_issues"]:
        assert field in data, f"Missing field: {field}"
