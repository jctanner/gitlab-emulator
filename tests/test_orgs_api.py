"""Tests for the Organization REST API endpoints."""

import pytest

from tests.conftest import auth_headers
from tests.test_projects_api import _create_user_and_token

API = "/api/v4"


@pytest.mark.asyncio
async def test_create_org(client, test_user, test_token):
    """POST /orgs creates an organization."""
    resp = await client.post(
        f"{API}/orgs",
        json={"login": "test-org", "name": "Test Organization"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["login"] == "test-org"
    assert data["name"] == "Test Organization"
    assert data["type"] == "Organization"


@pytest.mark.asyncio
async def test_get_org(client, test_user, test_token):
    """GET /orgs/{org} returns organization details."""
    await client.post(
        f"{API}/orgs",
        json={"login": "get-org"},
        headers=auth_headers(test_token),
    )
    resp = await client.get(f"{API}/orgs/get-org")
    assert resp.status_code == 200
    assert resp.json()["login"] == "get-org"


@pytest.mark.asyncio
async def test_get_org_not_found(client):
    """GET /orgs/{org} returns 404 for missing org."""
    resp = await client.get(f"{API}/orgs/nonexistent-org")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_org(client, test_user, test_token):
    """PATCH /orgs/{org} updates organization."""
    await client.post(
        f"{API}/orgs",
        json={"login": "upd-org"},
        headers=auth_headers(test_token),
    )
    resp = await client.patch(
        f"{API}/orgs/upd-org",
        json={"description": "Updated desc", "location": "NYC"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["description"] == "Updated desc"
    assert data["location"] == "NYC"


@pytest.mark.asyncio
async def test_update_org_requires_owner(client, db_session, test_user, test_token):
    maintainer, maintainer_token = await _create_user_and_token(
        db_session, "org-settings-maintainer"
    )
    org = await client.post(
        f"{API}/orgs",
        json={"login": "org-owner-gate"},
        headers=auth_headers(test_token),
    )
    assert org.status_code == 201
    member = await client.post(
        f"{API}/groups/{org.json()['id']}/members",
        json={"user_id": maintainer.id, "access_level": 40},
        headers=auth_headers(test_token),
    )
    assert member.status_code == 201

    denied = await client.patch(
        f"{API}/orgs/org-owner-gate",
        json={"description": "denied"},
        headers=auth_headers(maintainer_token),
    )
    assert denied.status_code == 403

    allowed = await client.patch(
        f"{API}/orgs/org-owner-gate",
        json={"description": "allowed"},
        headers=auth_headers(test_token),
    )
    assert allowed.status_code == 200
    assert allowed.json()["description"] == "allowed"


@pytest.mark.asyncio
async def test_org_repo_and_team_management_requires_owner(
    client, db_session, test_user, test_token
):
    maintainer, maintainer_token = await _create_user_and_token(
        db_session, "org-team-maintainer"
    )
    org = await client.post(
        f"{API}/orgs",
        json={"login": "org-team-gate"},
        headers=auth_headers(test_token),
    )
    assert org.status_code == 201
    member = await client.post(
        f"{API}/groups/{org.json()['id']}/members",
        json={"user_id": maintainer.id, "access_level": 40},
        headers=auth_headers(test_token),
    )
    assert member.status_code == 201

    denied_repo = await client.post(
        f"{API}/orgs/org-team-gate/repos",
        json={"name": "maintainer-denied"},
        headers=auth_headers(maintainer_token),
    )
    assert denied_repo.status_code == 403

    repo = await client.post(
        f"{API}/orgs/org-team-gate/repos",
        json={"name": "owner-allowed"},
        headers=auth_headers(test_token),
    )
    assert repo.status_code == 201

    denied_team = await client.post(
        f"{API}/orgs/org-team-gate/teams",
        json={"name": "Maintainer Team"},
        headers=auth_headers(maintainer_token),
    )
    assert denied_team.status_code == 403

    team = await client.post(
        f"{API}/orgs/org-team-gate/teams",
        json={"name": "Owner Team"},
        headers=auth_headers(test_token),
    )
    assert team.status_code == 201
    team_id = team.json()["id"]

    denied_update = await client.patch(
        f"{API}/teams/{team_id}",
        json={"description": "denied"},
        headers=auth_headers(maintainer_token),
    )
    assert denied_update.status_code == 403

    updated = await client.patch(
        f"{API}/teams/{team_id}",
        json={"description": "allowed"},
        headers=auth_headers(test_token),
    )
    assert updated.status_code == 200
    assert updated.json()["description"] == "allowed"

    denied_delete = await client.delete(
        f"{API}/teams/{team_id}",
        headers=auth_headers(maintainer_token),
    )
    assert denied_delete.status_code == 403

    deleted = await client.delete(
        f"{API}/teams/{team_id}",
        headers=auth_headers(test_token),
    )
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_duplicate_org(client, test_user, test_token):
    """Creating duplicate org returns 422."""
    await client.post(
        f"{API}/orgs",
        json={"login": "dupe-org"},
        headers=auth_headers(test_token),
    )
    resp = await client.post(
        f"{API}/orgs",
        json={"login": "dupe-org"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_user_orgs(client, test_user, test_token):
    """GET /user/orgs lists authenticated user's organizations."""
    await client.post(
        f"{API}/orgs",
        json={"login": "my-org"},
        headers=auth_headers(test_token),
    )
    resp = await client.get(f"{API}/user/orgs", headers=auth_headers(test_token))
    assert resp.status_code == 200
    data = resp.json()
    assert any(o["login"] == "my-org" for o in data)


@pytest.mark.asyncio
async def test_list_user_orgs_public(client, test_user, test_token):
    """GET /users/{username}/orgs lists public organizations."""
    await client.post(
        f"{API}/orgs",
        json={"login": "pub-org"},
        headers=auth_headers(test_token),
    )
    resp = await client.get(f"{API}/users/testuser/orgs")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_list_org_members(client, test_user, test_token):
    """GET /orgs/{org}/members lists org members."""
    await client.post(
        f"{API}/orgs",
        json={"login": "mem-org"},
        headers=auth_headers(test_token),
    )
    resp = await client.get(f"{API}/orgs/mem-org/members")
    assert resp.status_code == 200
    data = resp.json()
    # Creator should be a member
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_org_requires_login(client, test_user, test_token):
    """Creating org without login returns 422."""
    resp = await client.post(
        f"{API}/orgs",
        json={"name": "No Login Org"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_org_response_format(client, test_user, test_token):
    """Org response has required fields."""
    await client.post(
        f"{API}/orgs",
        json={"login": "fmt-org", "description": "Test", "email": "org@test.com"},
        headers=auth_headers(test_token),
    )
    resp = await client.get(f"{API}/orgs/fmt-org")
    data = resp.json()
    for field in ["login", "id", "node_id", "url", "repos_url", "events_url",
                  "hooks_url", "members_url", "avatar_url", "description",
                  "created_at", "updated_at", "type"]:
        assert field in data, f"Missing field: {field}"
