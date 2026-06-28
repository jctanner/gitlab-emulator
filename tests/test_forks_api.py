"""Tests for the Fork REST API endpoints."""

import pytest

from tests.conftest import auth_headers
from tests.test_projects_api import _create_user_and_token

API = "/api/v4"


@pytest.mark.asyncio
async def test_create_fork(client, test_user, test_token, admin_user, admin_token):
    """POST /repos/{owner}/{repo}/forks creates a fork."""
    # Create original repo
    await client.post(
        f"{API}/user/repos",
        json={"name": "fork-source", "auto_init": True},
        headers=auth_headers(test_token),
    )
    # Fork it as admin
    resp = await client.post(
        f"{API}/repos/testuser/fork-source/forks",
        json={},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["fork"] is True
    assert data["full_name"] == "admin/fork-source"


@pytest.mark.asyncio
async def test_list_forks(client, test_user, test_token, admin_user, admin_token):
    """GET /repos/{owner}/{repo}/forks lists forks."""
    await client.post(
        f"{API}/user/repos",
        json={"name": "fork-list"},
        headers=auth_headers(test_token),
    )
    await client.post(
        f"{API}/repos/testuser/fork-list/forks",
        json={},
        headers=auth_headers(admin_token),
    )
    resp = await client.get(f"{API}/repos/testuser/fork-list/forks")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_fork_preserves_description(client, test_user, test_token, admin_user, admin_token):
    """Forked repo inherits parent description."""
    await client.post(
        f"{API}/user/repos",
        json={"name": "fork-desc", "description": "Original description"},
        headers=auth_headers(test_token),
    )
    resp = await client.post(
        f"{API}/repos/testuser/fork-desc/forks",
        json={},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 202
    assert resp.json()["description"] == "Original description"


@pytest.mark.asyncio
async def test_duplicate_fork_fails(client, test_user, test_token, admin_user, admin_token):
    """Creating a duplicate fork returns 422."""
    await client.post(
        f"{API}/user/repos",
        json={"name": "fork-dupe"},
        headers=auth_headers(test_token),
    )
    await client.post(
        f"{API}/repos/testuser/fork-dupe/forks",
        json={},
        headers=auth_headers(admin_token),
    )
    resp = await client.post(
        f"{API}/repos/testuser/fork-dupe/forks",
        json={},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_fork_with_custom_name(client, test_user, test_token, admin_user, admin_token):
    """Fork can have a custom name."""
    await client.post(
        f"{API}/user/repos",
        json={"name": "fork-named"},
        headers=auth_headers(test_token),
    )
    resp = await client.post(
        f"{API}/repos/testuser/fork-named/forks",
        json={"name": "my-custom-fork"},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 202
    assert resp.json()["name"] == "my-custom-fork"


@pytest.mark.asyncio
async def test_fork_into_organization_requires_owner(
    client, db_session, test_user, test_token
):
    maintainer, maintainer_token = await _create_user_and_token(
        db_session, "fork-org-maintainer"
    )
    source = await client.post(
        f"{API}/user/repos",
        json={"name": "fork-org-source", "auto_init": True},
        headers=auth_headers(test_token),
    )
    assert source.status_code == 201
    org = await client.post(
        f"{API}/orgs",
        json={"login": "fork-target-org"},
        headers=auth_headers(test_token),
    )
    assert org.status_code == 201
    member = await client.post(
        f"{API}/groups/{org.json()['id']}/members",
        json={"user_id": maintainer.id, "access_level": 40},
        headers=auth_headers(test_token),
    )
    assert member.status_code == 201

    denied = await client.post(
        f"{API}/repos/testuser/fork-org-source/forks",
        json={"organization": "fork-target-org", "name": "denied-fork"},
        headers=auth_headers(maintainer_token),
    )
    assert denied.status_code == 403

    allowed = await client.post(
        f"{API}/repos/testuser/fork-org-source/forks",
        json={"organization": "fork-target-org", "name": "allowed-fork"},
        headers=auth_headers(test_token),
    )
    assert allowed.status_code == 202
    assert allowed.json()["full_name"] == "fork-target-org/allowed-fork"


@pytest.mark.asyncio
async def test_fork_requires_auth(client, test_user, test_token):
    """Creating a fork requires authentication."""
    await client.post(
        f"{API}/user/repos",
        json={"name": "fork-auth"},
        headers=auth_headers(test_token),
    )
    resp = await client.post(
        f"{API}/repos/testuser/fork-auth/forks",
        json={},
    )
    assert resp.status_code == 401
