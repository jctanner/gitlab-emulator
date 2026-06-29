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
async def test_basic_auth_with_password(client, db_session):
    """Authorization: Basic <base64(username:password)> works."""
    from app.models.user import User
    from app.services.auth_service import hash_password

    user = User(
        login="basic-user",
        hashed_password=hash_password("secret-password"),
        name="Basic User",
        email="basic-user@test.com",
    )
    db_session.add(user)
    await db_session.commit()

    creds = base64.b64encode(b"basic-user:secret-password").decode()
    resp = await client.get(
        f"{API}/user",
        headers={"Authorization": f"Basic {creds}"},
    )
    assert resp.status_code == 200
    assert resp.json()["login"] == "basic-user"


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
    assert data["public_email"] == "test@test.com"
    assert data["website_url"] == ""
    assert data["bot"] is False


@pytest.mark.asyncio
async def test_list_users_supports_gitlab_search_and_pagination(client, db_session):
    """GET /users supports GitLab-shaped search, username, and pagination."""
    from app.models.user import User
    from app.services.auth_service import hash_password

    for login, name in (
        ("alice-search", "Alice Search"),
        ("bob-search", "Bob Search"),
    ):
        db_session.add(
            User(
                login=login,
                hashed_password=hash_password("password"),
                name=name,
                email=f"{login}@test.com",
            )
        )
    await db_session.commit()

    search = await client.get(f"{API}/users?search=search&per_page=1&page=1")
    assert search.status_code == 200
    assert search.headers["X-Total"] == "2"
    assert search.headers["X-Total-Pages"] == "2"
    assert search.headers["X-Next-Page"] == "2"
    assert "rel=\"next\"" in search.headers["Link"]
    assert len(search.json()) == 1
    assert search.json()[0]["username"] == "alice-search"

    username = await client.get(f"{API}/users?username=bob-search")
    assert username.status_code == 200
    assert [user["username"] for user in username.json()] == ["bob-search"]
    assert username.json()[0]["public_email"] == "bob-search@test.com"


@pytest.mark.asyncio
async def test_admin_token_helper_creates_gitlab_pat(client, test_user, admin_token):
    """Admin token helper returns a GitLab-style token that authenticates."""
    token_resp = await client.post(
        f"{API}/admin/tokens",
        json={"login": "testuser", "name": "gitlab-token", "scopes": ["api"]},
        headers=auth_headers(admin_token),
    )
    assert token_resp.status_code == 201
    token = token_resp.json()["token"]
    assert token.startswith("glpat-")

    user_resp = await client.get(f"{API}/user", headers={"PRIVATE-TOKEN": token})
    assert user_resp.status_code == 200
    assert user_resp.json()["username"] == "testuser"


@pytest.mark.asyncio
async def test_admin_token_helper_accepts_basic_admin_password(client, db_session):
    """Admin bootstrap helpers can be driven by Basic site-admin credentials."""
    from app.models.user import User
    from app.services.auth_service import hash_password

    admin = User(
        login="basic-admin",
        hashed_password=hash_password("admin-password"),
        name="Basic Admin",
        email="basic-admin@test.com",
        site_admin=True,
    )
    db_session.add(admin)
    await db_session.commit()

    creds = base64.b64encode(b"basic-admin:admin-password").decode()
    token_resp = await client.post(
        f"{API}/admin/tokens",
        json={"login": "basic-admin", "name": "basic-admin-token", "scopes": ["api"]},
        headers={"Authorization": f"Basic {creds}"},
    )
    assert token_resp.status_code == 201
    assert token_resp.json()["token"].startswith("glpat-")


@pytest.mark.asyncio
async def test_admin_helpers_require_site_admin(client, test_user, test_token, admin_token):
    unauthenticated = await client.post(
        f"{API}/admin/tokens",
        json={"login": "testuser"},
    )
    assert unauthenticated.status_code == 401

    non_admin_token = await client.post(
        f"{API}/admin/tokens",
        json={"login": "testuser"},
        headers=auth_headers(test_token),
    )
    assert non_admin_token.status_code == 403

    non_admin_user = await client.post(
        f"{API}/admin/users",
        json={
            "login": "not-allowed-user",
            "email": "not-allowed@example.com",
            "password": "password",
        },
        headers=auth_headers(test_token),
    )
    assert non_admin_user.status_code == 403

    admin_user = await client.post(
        f"{API}/admin/users",
        json={
            "login": "admin-created-user",
            "email": "admin-created@example.com",
            "password": "password",
        },
        headers=auth_headers(admin_token),
    )
    assert admin_user.status_code == 201
    assert admin_user.json()["username"] == "admin-created-user"


@pytest.mark.asyncio
async def test_get_nonexistent_user(client):
    """GET /users/{username} returns 404 for missing user."""
    resp = await client.get(f"{API}/users/nosuchuser")
    assert resp.status_code == 404
