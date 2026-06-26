"""Tests for authentication endpoints and token validation."""

import base64

import pytest

from tests.conftest import auth_headers

API = "/api/v4"


@pytest.mark.asyncio
async def test_unauthenticated_get_user(client):
    """GET /user without auth returns 401."""
    resp = await client.get(f"{API}/user")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_authenticated_get_user(client, test_user, test_token):
    """GET /user with valid token returns user profile."""
    resp = await client.get(f"{API}/user", headers=auth_headers(test_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["login"] == "testuser"
    assert data["username"] == "testuser"
    assert data["web_url"].endswith("/testuser")
    assert data["state"] == "active"
    assert data["is_admin"] is False
    assert data["type"] == "User"


@pytest.mark.asyncio
async def test_bearer_auth(client, test_user, test_token):
    """Authorization: Bearer <token> works."""
    resp = await client.get(
        f"{API}/user",
        headers={"Authorization": f"Bearer {test_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["login"] == "testuser"


@pytest.mark.asyncio
async def test_private_token_auth(client, test_user, test_token):
    """PRIVATE-TOKEN: <token> works for GitLab CLI/API clients."""
    resp = await client.get(
        f"{API}/user",
        headers={"PRIVATE-TOKEN": test_token},
    )
    assert resp.status_code == 200
    assert resp.json()["login"] == "testuser"


@pytest.mark.asyncio
async def test_basic_auth_with_token(client, test_user, test_token):
    """Authorization: Basic <base64(username:token)> works."""
    creds = base64.b64encode(f"testuser:{test_token}".encode()).decode()
    resp = await client.get(
        f"{API}/user",
        headers={"Authorization": f"Basic {creds}"},
    )
    assert resp.status_code == 200
    assert resp.json()["login"] == "testuser"


@pytest.mark.asyncio
async def test_invalid_token(client):
    """Invalid token returns 401."""
    resp = await client.get(
        f"{API}/user",
        headers={"Authorization": "token invalid_token_here"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_public_user(client, test_user):
    """GET /users/{username} works without auth."""
    resp = await client.get(f"{API}/users/testuser")
    assert resp.status_code == 200
    data = resp.json()
    assert data["login"] == "testuser"
    assert data["username"] == "testuser"
    assert "id" in data
    assert "node_id" in data


@pytest.mark.asyncio
async def test_get_public_user_by_numeric_id(client, test_user):
    """GET /users/{id} works for GitLab-style numeric user lookup."""
    resp = await client.get(f"{API}/users/{test_user.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == test_user.id
    assert data["username"] == "testuser"


@pytest.mark.asyncio
async def test_admin_token_helper_creates_gitlab_pat(client, test_user):
    """Admin token helper returns a GitLab-style token that authenticates."""
    token_resp = await client.post(
        f"{API}/admin/tokens",
        json={"login": "testuser", "name": "gitlab-token", "scopes": ["api"]},
    )
    assert token_resp.status_code == 201
    token = token_resp.json()["token"]
    assert token.startswith("glpat-")

    user_resp = await client.get(f"{API}/user", headers={"PRIVATE-TOKEN": token})
    assert user_resp.status_code == 200
    assert user_resp.json()["username"] == "testuser"


@pytest.mark.asyncio
async def test_get_nonexistent_user(client):
    """GET /users/{username} returns 404 for missing user."""
    resp = await client.get(f"{API}/users/nosuchuser")
    assert resp.status_code == 404
