"""Tests for GitLab-shaped group API endpoints."""

import pytest

from tests.conftest import API, auth_headers


@pytest.mark.asyncio
async def test_create_group_returns_gitlab_shape(client, test_token):
    resp = await client.post(
        f"{API}/groups",
        json={"path": "platform", "name": "Platform", "description": "Team group"},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 201
    data = resp.json()
    assert data["id"]
    assert data["path"] == "platform"
    assert data["full_path"] == "platform"
    assert data["name"] == "Platform"
    assert data["description"] == "Team group"
    assert data["web_url"] == "http://testserver/groups/platform"
    assert data["_links"]["projects"].endswith(f"/api/v4/groups/{data['id']}/projects")


@pytest.mark.asyncio
async def test_get_group_by_id_and_path(client, test_token):
    create = await client.post(
        f"{API}/groups",
        json={"path": "ops", "name": "Operations"},
        headers=auth_headers(test_token),
    )
    group_id = create.json()["id"]

    by_id = await client.get(f"{API}/groups/{group_id}")
    assert by_id.status_code == 200
    assert by_id.json()["full_path"] == "ops"

    by_path = await client.get(f"{API}/groups/ops")
    assert by_path.status_code == 200
    assert by_path.json()["id"] == group_id


@pytest.mark.asyncio
async def test_list_groups_supports_search(client, test_token):
    await client.post(
        f"{API}/groups",
        json={"path": "searchable-group", "name": "Searchable Group"},
        headers=auth_headers(test_token),
    )
    await client.post(
        f"{API}/groups",
        json={"path": "other-group", "name": "Other Group"},
        headers=auth_headers(test_token),
    )

    resp = await client.get(
        f"{API}/groups",
        params={"search": "searchable"},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    assert [group["full_path"] for group in resp.json()] == ["searchable-group"]


@pytest.mark.asyncio
async def test_list_groups_filters_shape_and_pagination_headers(client, test_token):
    parent = await client.post(
        f"{API}/groups",
        json={"path": "filter-parent", "name": "Filter Parent"},
        headers=auth_headers(test_token),
    )
    assert parent.status_code == 201
    parent_id = parent.json()["id"]
    child = await client.post(
        f"{API}/groups",
        json={"path": "child", "name": "Filter Child", "parent_id": parent_id},
        headers=auth_headers(test_token),
    )
    assert child.status_code == 201
    child_id = child.json()["id"]

    top_level = await client.get(
        f"{API}/groups",
        params={
            "search": "filter-",
            "top_level_only": True,
            "page": 1,
            "per_page": 1,
        },
        headers=auth_headers(test_token),
    )
    assert top_level.status_code == 200
    assert top_level.headers["X-Total"] == "1"
    assert top_level.headers["X-Total-Pages"] == "1"
    assert top_level.headers["X-Page"] == "1"
    assert top_level.headers["X-Per-Page"] == "1"
    data = top_level.json()
    assert [group["full_path"] for group in data] == ["filter-parent"]
    assert data[0]["organization_id"] == parent_id
    assert data[0]["updated_at"]
    assert data[0]["shared_runners_setting"] == "enabled"
    assert data[0]["default_branch_protection_defaults"]["allow_force_push"] is False
    assert data[0]["_links"]["hooks"].endswith(f"/api/v4/groups/{parent_id}/hooks")

    skipped = await client.get(
        f"{API}/groups",
        params=[("search", "filter-"), ("skip_groups", str(parent_id))],
        headers=auth_headers(test_token),
    )
    assert skipped.status_code == 200
    assert [group["id"] for group in skipped.json()] == [child_id]
    assert skipped.json()[0]["parent_id"] == parent_id


@pytest.mark.asyncio
async def test_list_groups_owned_and_min_access_level_filters(client, test_token):
    owned = await client.post(
        f"{API}/groups",
        json={"path": "owned-filter", "name": "Owned Filter"},
        headers=auth_headers(test_token),
    )
    assert owned.status_code == 201
    await client.post(
        f"{API}/groups",
        json={"path": "owned-filter-two", "name": "Owned Filter Two"},
        headers=auth_headers(test_token),
    )

    listed = await client.get(
        f"{API}/groups",
        params={
            "search": "owned-filter",
            "owned": True,
            "order_by": "id",
            "sort": "desc",
        },
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert [group["full_path"] for group in listed.json()] == [
        "owned-filter-two",
        "owned-filter",
    ]

    maintainers = await client.get(
        f"{API}/groups",
        params={"search": "owned-filter", "min_access_level": 40},
        headers=auth_headers(test_token),
    )
    assert maintainers.status_code == 200
    assert maintainers.json() == []


@pytest.mark.asyncio
async def test_group_projects_lists_group_namespace_projects(client, test_token):
    group = await client.post(
        f"{API}/groups",
        json={"path": "delivery", "name": "Delivery"},
        headers=auth_headers(test_token),
    )
    group_id = group.json()["id"]

    project = await client.post(
        f"{API}/projects",
        json={"name": "api", "namespace_id": group_id},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201

    resp = await client.get(f"{API}/groups/{group_id}/projects")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["path_with_namespace"] == "delivery/api"
    assert data[0]["namespace"]["kind"] == "group"


@pytest.mark.asyncio
async def test_group_projects_route_accepts_group_path(client, test_token):
    group = await client.post(
        f"{API}/groups",
        json={"path": "qa", "name": "QA"},
        headers=auth_headers(test_token),
    )
    assert group.status_code == 201

    project = await client.post(
        f"{API}/projects",
        json={"name": "checks", "namespace_path": "qa"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201

    resp = await client.get(f"{API}/groups/qa/projects")
    assert resp.status_code == 200
    assert [item["path_with_namespace"] for item in resp.json()] == ["qa/checks"]


@pytest.mark.asyncio
async def test_nested_group_namespace_project_creation(client, test_token):
    parent = await client.post(
        f"{API}/groups",
        json={"path": "platform-parent", "name": "Platform Parent"},
        headers=auth_headers(test_token),
    )
    assert parent.status_code == 201

    child = await client.post(
        f"{API}/groups",
        json={
            "path": "backend",
            "name": "Backend",
            "parent_id": parent.json()["id"],
        },
        headers=auth_headers(test_token),
    )
    assert child.status_code == 201
    child_data = child.json()
    assert child_data["path"] == "backend"
    assert child_data["full_path"] == "platform-parent/backend"
    assert child_data["parent_id"] == parent.json()["id"]

    fetched = await client.get(
        f"{API}/groups/platform-parent%2Fbackend",
        headers=auth_headers(test_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["full_path"] == "platform-parent/backend"
    assert fetched.json()["parent_id"] == parent.json()["id"]

    project = await client.post(
        f"{API}/projects",
        json={"name": "api", "namespace_path": "platform-parent/backend"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    data = project.json()
    assert data["path_with_namespace"] == "platform-parent/backend/api"
    assert data["namespace"]["path"] == "backend"
    assert data["namespace"]["full_path"] == "platform-parent/backend"

    projects = await client.get(
        f"{API}/groups/platform-parent%2Fbackend/projects",
        headers=auth_headers(test_token),
    )
    assert projects.status_code == 200
    assert [item["path_with_namespace"] for item in projects.json()] == [
        "platform-parent/backend/api"
    ]

    literal_projects = await client.get(
        f"{API}/groups/platform-parent/backend/projects",
        headers=auth_headers(test_token),
    )
    assert literal_projects.status_code == 200
    assert [item["path_with_namespace"] for item in literal_projects.json()] == [
        "platform-parent/backend/api"
    ]


@pytest.mark.asyncio
async def test_create_duplicate_group_fails(client, test_token):
    first = await client.post(
        f"{API}/groups",
        json={"path": "duplicate"},
        headers=auth_headers(test_token),
    )
    assert first.status_code == 201

    second = await client.post(
        f"{API}/groups",
        json={"path": "duplicate"},
        headers=auth_headers(test_token),
    )
    assert second.status_code == 400


@pytest.mark.asyncio
async def test_gitlab_group_members_crud(client, test_user, test_token, admin_user):
    group = await client.post(
        f"{API}/groups",
        json={"path": "members", "name": "Members"},
        headers=auth_headers(test_token),
    )
    assert group.status_code == 201
    group_id = group.json()["id"]

    listed = await client.get(
        f"{API}/groups/{group_id}/members",
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert any(
        member["username"] == "testuser" and member["access_level"] == 50
        for member in listed.json()
    )

    created = await client.post(
        f"{API}/groups/{group_id}/members",
        json={"user_id": admin_user.id, "access_level": 30},
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201
    assert created.json()["username"] == "admin"
    assert created.json()["access_level"] == 30

    fetched = await client.get(
        f"{API}/groups/{group_id}/members/{admin_user.id}",
        headers=auth_headers(test_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["access_level"] == 30

    deleted = await client.delete(
        f"{API}/groups/{group_id}/members/{admin_user.id}",
        headers=auth_headers(test_token),
    )
    assert deleted.status_code == 204

    missing = await client.get(
        f"{API}/groups/{group_id}/members/{admin_user.id}",
        headers=auth_headers(test_token),
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_gitlab_group_members_all_and_duplicate_edge_cases(
    client, test_user, test_token
):
    group = await client.post(
        f"{API}/groups",
        json={"path": "members-all", "name": "Members All"},
        headers=auth_headers(test_token),
    )
    assert group.status_code == 201
    group_id = group.json()["id"]

    listed = await client.get(
        f"{API}/groups/{group_id}/members/all",
        params={"query": "test"},
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert listed.headers["X-Total"] == "1"
    assert [member["username"] for member in listed.json()] == ["testuser"]

    duplicate = await client.post(
        f"{API}/groups/{group_id}/members",
        json={"user_id": test_user.id, "access_level": 50},
        headers=auth_headers(test_token),
    )
    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_gitlab_group_members_pagination_and_query(
    client, test_user, test_token, admin_user
):
    group = await client.post(
        f"{API}/groups",
        json={"path": "member-pages", "name": "Member Pages"},
        headers=auth_headers(test_token),
    )
    assert group.status_code == 201
    group_id = group.json()["id"]
    created = await client.post(
        f"{API}/groups/{group_id}/members",
        json={"user_id": admin_user.id, "access_level": 30},
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201

    listed = await client.get(
        f"{API}/groups/{group_id}/members",
        params={"page": 1, "per_page": 1},
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.headers["X-Total"] == "2"
    assert listed.headers["X-Total-Pages"] == "2"
    assert listed.headers["X-Next-Page"] == "2"
    member = listed.json()[0]
    assert member["created_at"]
    assert "created_by" in member
    assert "invite_email" in member
    assert "group_saml_identity" in member
    assert "group_scim_identity" in member

    filtered = await client.get(
        f"{API}/groups/{group_id}/members",
        params={"query": "adm", "page": 1, "per_page": 10},
        headers=auth_headers(test_token),
    )
    assert filtered.status_code == 200
    assert filtered.headers["X-Total"] == "1"
    assert [member["username"] for member in filtered.json()] == ["admin"]


@pytest.mark.asyncio
async def test_gitlab_group_members_accept_group_path(client, test_user, test_token):
    group = await client.post(
        f"{API}/groups",
        json={"path": "member-path", "name": "Member Path"},
        headers=auth_headers(test_token),
    )
    assert group.status_code == 201

    resp = await client.get(
        f"{API}/groups/member-path/members",
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    assert any(member["username"] == "testuser" for member in resp.json())


@pytest.mark.asyncio
async def test_gitlab_nested_group_hooks_list_with_pagination(client, test_token):
    parent = await client.post(
        f"{API}/groups",
        json={"path": "hook-parent", "name": "Hook Parent"},
        headers=auth_headers(test_token),
    )
    assert parent.status_code == 201
    child = await client.post(
        f"{API}/groups",
        json={"path": "child", "name": "Hook Child", "parent_id": parent.json()["id"]},
        headers=auth_headers(test_token),
    )
    assert child.status_code == 201

    first = await client.post(
        f"{API}/groups/hook-parent%2Fchild/hooks",
        json={"url": "https://example.com/one", "push_events": True},
        headers=auth_headers(test_token),
    )
    assert first.status_code == 201
    second = await client.post(
        f"{API}/groups/hook-parent/child/hooks",
        json={"url": "https://example.com/two", "pipeline_events": True},
        headers=auth_headers(test_token),
    )
    assert second.status_code == 201

    listed = await client.get(
        f"{API}/groups/hook-parent%2Fchild/hooks",
        params={"page": 1, "per_page": 1},
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert listed.headers["X-Total"] == "2"
    assert listed.headers["X-Next-Page"] == "2"
    data = listed.json()
    assert len(data) == 1
    assert data[0]["group_id"] == child.json()["id"]
    assert data[0]["push_events"] is True
    assert data[0]["_links"]["self"].endswith(
        f"/api/v4/groups/{child.json()['id']}/hooks/{data[0]['id']}"
    )
