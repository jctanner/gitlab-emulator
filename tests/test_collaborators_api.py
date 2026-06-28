"""Tests for the Collaborator REST API endpoints."""

import pytest

from tests.conftest import auth_headers
from tests.test_projects_api import _create_user_and_token

API = "/api/v4"


@pytest.mark.asyncio
async def test_list_collaborators(client, test_user, test_token):
    """GET /repos/{owner}/{repo}/collaborators lists collaborators."""
    await client.post(
        f"{API}/user/repos", json={"name": "collab-repo"}, headers=auth_headers(test_token)
    )
    resp = await client.get(
        f"{API}/repos/testuser/collab-repo/collaborators",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    # Owner is always included
    assert len(data) >= 1
    assert any(c["login"] == "testuser" for c in data)


@pytest.mark.asyncio
async def test_add_collaborator(client, test_user, test_token, admin_user, admin_token):
    """PUT /repos/{owner}/{repo}/collaborators/{username} adds a collaborator."""
    await client.post(
        f"{API}/user/repos", json={"name": "collab-add"}, headers=auth_headers(test_token)
    )
    resp = await client.put(
        f"{API}/repos/testuser/collab-add/collaborators/admin",
        json={"permission": "push"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_check_collaborator_is_owner(client, test_user, test_token):
    """GET /repos/{owner}/{repo}/collaborators/{username} returns 204 for owner."""
    await client.post(
        f"{API}/user/repos", json={"name": "collab-chk"}, headers=auth_headers(test_token)
    )
    resp = await client.get(
        f"{API}/repos/testuser/collab-chk/collaborators/testuser",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_check_collaborator_not_found(client, test_user, test_token):
    """GET /repos/{owner}/{repo}/collaborators/{username} returns 404 for non-collaborator."""
    await client.post(
        f"{API}/user/repos", json={"name": "collab-404"}, headers=auth_headers(test_token)
    )
    resp = await client.get(
        f"{API}/repos/testuser/collab-404/collaborators/nobody",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_remove_collaborator(client, test_user, test_token, admin_user, admin_token):
    """DELETE /repos/{owner}/{repo}/collaborators/{username} removes collaborator."""
    await client.post(
        f"{API}/user/repos", json={"name": "collab-rm"}, headers=auth_headers(test_token)
    )
    await client.put(
        f"{API}/repos/testuser/collab-rm/collaborators/admin",
        json={"permission": "push"},
        headers=auth_headers(test_token),
    )
    resp = await client.delete(
        f"{API}/repos/testuser/collab-rm/collaborators/admin",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_get_collaborator_permission(client, test_user, test_token, admin_user, admin_token):
    """GET /repos/{owner}/{repo}/collaborators/{username}/permission returns permission."""
    await client.post(
        f"{API}/user/repos", json={"name": "collab-perm"}, headers=auth_headers(test_token)
    )
    await client.put(
        f"{API}/repos/testuser/collab-perm/collaborators/admin",
        json={"permission": "push"},
        headers=auth_headers(test_token),
    )
    resp = await client.get(
        f"{API}/repos/testuser/collab-perm/collaborators/admin/permission",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["permission"] == "push"
    assert "user" in data


@pytest.mark.asyncio
async def test_owner_permission_is_admin(client, test_user, test_token):
    """Owner always has admin permission."""
    await client.post(
        f"{API}/user/repos", json={"name": "collab-own"}, headers=auth_headers(test_token)
    )
    resp = await client.get(
        f"{API}/repos/testuser/collab-own/collaborators/testuser/permission",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    assert resp.json()["permission"] == "admin"


@pytest.mark.asyncio
async def test_add_nonexistent_user_as_collaborator(client, test_user, test_token):
    """PUT collaborator for non-existent user returns 404."""
    await client.post(
        f"{API}/user/repos", json={"name": "collab-nf"}, headers=auth_headers(test_token)
    )
    resp = await client.put(
        f"{API}/repos/testuser/collab-nf/collaborators/nonexistent_user_xyz",
        json={"permission": "push"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_gitlab_project_members_crud(client, test_user, test_token, admin_user):
    """GitLab project members map onto repository collaborators."""
    project = await client.post(
        f"{API}/user/repos",
        json={"name": "members-crud"},
        headers=auth_headers(test_token),
    )
    project_id = project.json()["id"]

    listed = await client.get(
        f"{API}/projects/{project_id}/members",
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert any(
        member["username"] == "testuser" and member["access_level"] == 50
        for member in listed.json()
    )

    created = await client.post(
        f"{API}/projects/{project_id}/members",
        json={"user_id": admin_user.id, "access_level": 30},
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201
    assert created.json()["username"] == "admin"
    assert created.json()["access_level"] == 30

    fetched = await client.get(
        f"{API}/projects/{project_id}/members/{admin_user.id}",
        headers=auth_headers(test_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["access_level"] == 30

    deleted = await client.delete(
        f"{API}/projects/{project_id}/members/{admin_user.id}",
        headers=auth_headers(test_token),
    )
    assert deleted.status_code == 204

    missing = await client.get(
        f"{API}/projects/{project_id}/members/{admin_user.id}",
        headers=auth_headers(test_token),
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_gitlab_project_member_preserves_guest_access_level(
    client, db_session, test_user, test_token
):
    guest, _ = await _create_user_and_token(db_session, "member-guest-access")
    project = await client.post(
        f"{API}/user/repos",
        json={"name": "members-guest-access"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    created = await client.post(
        f"{API}/projects/{project_id}/members",
        json={"user_id": guest.id, "access_level": 10},
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201
    assert created.json()["access_level"] == 10

    fetched = await client.get(
        f"{API}/projects/{project_id}/members/{guest.id}",
        headers=auth_headers(test_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["access_level"] == 10

    listed = await client.get(
        f"{API}/projects/{project_id}/members",
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert any(
        member["username"] == guest.login and member["access_level"] == 10
        for member in listed.json()
    )


@pytest.mark.asyncio
async def test_gitlab_project_member_writes_require_maintainer(
    client, db_session, test_user, test_token
):
    maintainer, maintainer_token = await _create_user_and_token(
        db_session, "member-maintainer"
    )
    developer, developer_token = await _create_user_and_token(
        db_session, "member-developer"
    )
    target, _ = await _create_user_and_token(db_session, "member-target")
    project = await client.post(
        f"{API}/user/repos",
        json={"name": "members-role-gate"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    for user, level in ((maintainer, 40), (developer, 30)):
        created = await client.post(
            f"{API}/projects/{project_id}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert created.status_code == 201

    denied = await client.post(
        f"{API}/projects/{project_id}/members",
        json={"user_id": target.id, "access_level": 20},
        headers=auth_headers(developer_token),
    )
    assert denied.status_code == 403

    allowed = await client.post(
        f"{API}/projects/{project_id}/members",
        json={"user_id": target.id, "access_level": 20},
        headers=auth_headers(maintainer_token),
    )
    assert allowed.status_code == 201

    delete_denied = await client.delete(
        f"{API}/projects/{project_id}/members/{target.id}",
        headers=auth_headers(developer_token),
    )
    assert delete_denied.status_code == 403

    deleted = await client.delete(
        f"{API}/projects/{project_id}/members/{target.id}",
        headers=auth_headers(maintainer_token),
    )
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_github_collaborator_writes_require_maintainer(
    client, db_session, test_user, test_token
):
    maintainer, maintainer_token = await _create_user_and_token(
        db_session, "collab-maintainer"
    )
    developer, developer_token = await _create_user_and_token(
        db_session, "collab-developer"
    )
    target, _ = await _create_user_and_token(db_session, "collab-target")
    project = await client.post(
        f"{API}/user/repos",
        json={"name": "collab-role-gate"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    for user, level in ((maintainer, 40), (developer, 30)):
        created = await client.post(
            f"{API}/projects/{project_id}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert created.status_code == 201

    denied = await client.put(
        f"{API}/repos/testuser/collab-role-gate/collaborators/{target.login}",
        json={"permission": "pull"},
        headers=auth_headers(developer_token),
    )
    assert denied.status_code == 403

    allowed = await client.put(
        f"{API}/repos/testuser/collab-role-gate/collaborators/{target.login}",
        json={"permission": "pull"},
        headers=auth_headers(maintainer_token),
    )
    assert allowed.status_code == 201

    delete_denied = await client.delete(
        f"{API}/repos/testuser/collab-role-gate/collaborators/{target.login}",
        headers=auth_headers(developer_token),
    )
    assert delete_denied.status_code == 403

    deleted = await client.delete(
        f"{API}/repos/testuser/collab-role-gate/collaborators/{target.login}",
        headers=auth_headers(maintainer_token),
    )
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_gitlab_project_members_pagination_and_query(
    client, test_user, test_token, admin_user
):
    project = await client.post(
        f"{API}/user/repos",
        json={"name": "members-pages"},
        headers=auth_headers(test_token),
    )
    project_id = project.json()["id"]
    created = await client.post(
        f"{API}/projects/{project_id}/members",
        json={"user_id": admin_user.id, "access_level": 30},
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201

    listed = await client.get(
        f"{API}/projects/{project_id}/members",
        params={"page": 1, "per_page": 1},
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.headers["X-Total"] == "2"
    assert listed.headers["X-Total-Pages"] == "2"
    assert listed.headers["X-Next-Page"] == "2"
    assert 'rel="next"' in listed.headers["Link"]
    member = listed.json()[0]
    assert "created_at" in member
    assert "created_by" in member
    assert "invite_email" in member
    assert "group_saml_identity" in member
    assert "group_scim_identity" in member

    filtered = await client.get(
        f"{API}/projects/{project_id}/members",
        params={"query": "adm", "page": 1, "per_page": 10},
        headers=auth_headers(test_token),
    )
    assert filtered.status_code == 200
    assert filtered.headers["X-Total"] == "1"
    assert [member["username"] for member in filtered.json()] == ["admin"]


@pytest.mark.asyncio
async def test_gitlab_project_members_accept_url_encoded_path(client, test_user, test_token):
    """GitLab project member routes accept URL-encoded path_with_namespace."""
    await client.post(
        f"{API}/user/repos",
        json={"name": "members-path"},
        headers=auth_headers(test_token),
    )

    resp = await client.get(
        f"{API}/projects/testuser%2Fmembers-path/members",
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    assert any(member["username"] == "testuser" for member in resp.json())
