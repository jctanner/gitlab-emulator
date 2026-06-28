"""Tests for GitLab-shaped group API endpoints."""

import hashlib
import json
import secrets

import pytest

from tests.conftest import API, auth_headers


async def _create_user_and_token(db_session, login: str):
    from app.models.token import PersonalAccessToken
    from app.models.user import User

    user = User(
        login=login,
        hashed_password=hashlib.sha256(login.encode()).hexdigest(),
        name=login,
        email=f"{login}@test.com",
        site_admin=False,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    raw_token = f"ghp_{secrets.token_hex(20)}"
    db_session.add(
        PersonalAccessToken(
            user_id=user.id,
            name=f"{login}-token",
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            token_prefix=raw_token[:8],
            scopes=["repo", "user"],
        )
    )
    await db_session.commit()
    return user, raw_token


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
async def test_namespaces_list_search_and_get(client, test_user, test_token):
    group = await client.post(
        f"{API}/groups",
        json={"path": "namespace-group", "name": "Namespace Group"},
        headers=auth_headers(test_token),
    )
    assert group.status_code == 201

    listed = await client.get(
        f"{API}/namespaces",
        params={"search": "namespace", "page": 1, "per_page": 10},
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert listed.headers["X-Total"] == "1"
    namespaces = listed.json()
    assert namespaces[0]["kind"] == "group"
    assert namespaces[0]["path"] == "namespace-group"
    assert namespaces[0]["full_path"] == "namespace-group"
    assert namespaces[0]["web_url"] == "http://testserver/groups/namespace-group"

    by_id = await client.get(
        f"{API}/namespaces/{group.json()['id']}",
        headers=auth_headers(test_token),
    )
    assert by_id.status_code == 200
    assert by_id.json()["full_path"] == "namespace-group"

    by_path = await client.get(
        f"{API}/namespaces/namespace-group",
        headers=auth_headers(test_token),
    )
    assert by_path.status_code == 200
    assert by_path.json()["id"] == group.json()["id"]


@pytest.mark.asyncio
async def test_namespaces_include_current_user_and_owned_filter(
    client, test_user, test_token
):
    listed = await client.get(
        f"{API}/namespaces",
        params={"owned_only": True},
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    namespaces = listed.json()
    assert any(
        item["kind"] == "user" and item["full_path"] == test_user.login
        for item in namespaces
    )


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
    assert [group["full_path"] for group in maintainers.json()] == [
        "owned-filter",
        "owned-filter-two",
    ]


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
async def test_group_projects_hide_private_projects_from_non_members(
    client, db_session, test_token
):
    reporter, reporter_token = await _create_user_and_token(
        db_session, "group-project-list-reporter"
    )
    _outsider, outsider_token = await _create_user_and_token(
        db_session, "group-project-list-outsider"
    )
    group = await client.post(
        f"{API}/groups",
        json={"path": "private-project-list-group", "name": "Private Project List"},
        headers=auth_headers(test_token),
    )
    assert group.status_code == 201
    group_id = group.json()["id"]

    project = await client.post(
        f"{API}/projects",
        json={
            "name": "private-list-project",
            "namespace_id": group_id,
            "visibility": "private",
        },
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201

    denied = await client.get(
        f"{API}/groups/{group_id}/projects",
        headers=auth_headers(outsider_token),
    )
    assert denied.status_code == 200
    assert denied.json() == []

    member = await client.post(
        f"{API}/groups/{group_id}/members",
        json={"user_id": reporter.id, "access_level": 20},
        headers=auth_headers(test_token),
    )
    assert member.status_code == 201

    allowed = await client.get(
        f"{API}/groups/{group_id}/projects",
        headers=auth_headers(reporter_token),
    )
    assert allowed.status_code == 200
    assert [item["path_with_namespace"] for item in allowed.json()] == [
        "private-project-list-group/private-list-project"
    ]


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
async def test_group_subgroups_and_descendants_routes(client, test_token):
    parent = await client.post(
        f"{API}/groups",
        json={"path": "tree-parent", "name": "Tree Parent"},
        headers=auth_headers(test_token),
    )
    assert parent.status_code == 201
    parent_id = parent.json()["id"]

    child = await client.post(
        f"{API}/groups",
        json={"path": "backend", "name": "Backend", "parent_id": parent_id},
        headers=auth_headers(test_token),
    )
    assert child.status_code == 201
    child_id = child.json()["id"]

    grandchild = await client.post(
        f"{API}/groups",
        json={"path": "api", "name": "API", "parent_id": child_id},
        headers=auth_headers(test_token),
    )
    assert grandchild.status_code == 201

    frontend = await client.post(
        f"{API}/groups",
        json={"path": "frontend", "name": "Frontend", "parent_id": parent_id},
        headers=auth_headers(test_token),
    )
    assert frontend.status_code == 201
    frontend_id = frontend.json()["id"]

    subgroups = await client.get(
        f"{API}/groups/{parent_id}/subgroups",
        params={"page": 1, "per_page": 1},
        headers=auth_headers(test_token),
    )
    assert subgroups.status_code == 200
    assert subgroups.headers["X-Total"] == "2"
    assert subgroups.headers["X-Total-Pages"] == "2"
    assert [group["full_path"] for group in subgroups.json()] == [
        "tree-parent/backend"
    ]
    assert subgroups.json()[0]["parent_id"] == parent_id

    descendants = await client.get(
        f"{API}/groups/tree-parent/descendant_groups",
        headers=auth_headers(test_token),
    )
    assert descendants.status_code == 200
    assert [group["full_path"] for group in descendants.json()] == [
        "tree-parent/backend",
        "tree-parent/backend/api",
        "tree-parent/frontend",
    ]

    nested_subgroups = await client.get(
        f"{API}/groups/tree-parent%2Fbackend/subgroups",
        headers=auth_headers(test_token),
    )
    assert nested_subgroups.status_code == 200
    assert [group["full_path"] for group in nested_subgroups.json()] == [
        "tree-parent/backend/api"
    ]

    filtered = await client.get(
        f"{API}/groups/{parent_id}/descendant_groups",
        params=[
            ("search", "front"),
            ("skip_groups", str(frontend_id)),
        ],
        headers=auth_headers(test_token),
    )
    assert filtered.status_code == 200
    assert filtered.json() == []


@pytest.mark.asyncio
async def test_subgroup_creation_requires_parent_maintainer_access(
    client, db_session, test_token
):
    reporter, reporter_token = await _create_user_and_token(
        db_session, "subgroup-reporter"
    )
    maintainer, maintainer_token = await _create_user_and_token(
        db_session, "subgroup-maintainer"
    )
    parent = await client.post(
        f"{API}/groups",
        json={"path": "subgroup-gate", "name": "Subgroup Gate"},
        headers=auth_headers(test_token),
    )
    assert parent.status_code == 201
    parent_id = parent.json()["id"]

    for user, level in ((reporter, 20), (maintainer, 40)):
        member = await client.post(
            f"{API}/groups/{parent_id}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201

    denied = await client.post(
        f"{API}/groups",
        json={
            "path": "reporter-child",
            "name": "Reporter Child",
            "parent_id": parent_id,
        },
        headers=auth_headers(reporter_token),
    )
    assert denied.status_code == 403

    allowed = await client.post(
        f"{API}/groups",
        json={
            "path": "maintainer-child",
            "name": "Maintainer Child",
            "parent_id": parent_id,
        },
        headers=auth_headers(maintainer_token),
    )
    assert allowed.status_code == 201
    data = allowed.json()
    assert data["path"] == "maintainer-child"
    assert data["full_path"] == "subgroup-gate/maintainer-child"
    assert data["parent_id"] == parent_id


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
async def test_group_member_writes_require_owner(client, db_session, test_token):
    maintainer, maintainer_token = await _create_user_and_token(
        db_session, "group-member-maintainer"
    )
    target, _ = await _create_user_and_token(db_session, "group-member-target")
    second_target, _ = await _create_user_and_token(
        db_session, "group-member-target-two"
    )
    group = await client.post(
        f"{API}/groups",
        json={"path": "member-owner-gate", "name": "Member Owner Gate"},
        headers=auth_headers(test_token),
    )
    assert group.status_code == 201
    group_id = group.json()["id"]

    maintainer_member = await client.post(
        f"{API}/groups/{group_id}/members",
        json={"user_id": maintainer.id, "access_level": 40},
        headers=auth_headers(test_token),
    )
    assert maintainer_member.status_code == 201

    denied = await client.post(
        f"{API}/groups/{group_id}/members",
        json={"user_id": target.id, "access_level": 30},
        headers=auth_headers(maintainer_token),
    )
    assert denied.status_code == 403

    allowed = await client.post(
        f"{API}/groups/{group_id}/members",
        json={"user_id": target.id, "access_level": 30},
        headers=auth_headers(test_token),
    )
    assert allowed.status_code == 201

    delete_denied = await client.delete(
        f"{API}/groups/{group_id}/members/{target.id}",
        headers=auth_headers(maintainer_token),
    )
    assert delete_denied.status_code == 403

    deleted = await client.delete(
        f"{API}/groups/{group_id}/members/{target.id}",
        headers=auth_headers(test_token),
    )
    assert deleted.status_code == 204

    owner_can_still_add = await client.post(
        f"{API}/groups/{group_id}/members",
        json={"user_id": second_target.id, "access_level": 10},
        headers=auth_headers(test_token),
    )
    assert owner_can_still_add.status_code == 201


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
async def test_group_variables_crud_and_environment_scope(
    client, test_user, test_token
):
    group = await client.post(
        f"{API}/groups",
        json={"path": "variable-group", "name": "Variable Group"},
        headers=auth_headers(test_token),
    )
    assert group.status_code == 201
    group_id = group.json()["id"]

    created = await client.post(
        f"{API}/groups/{group_id}/variables",
        json={
            "key": "DEPLOY_TOKEN",
            "value": "token-one",
            "masked": True,
            "raw": True,
            "description": "deployment credential",
        },
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201
    assert created.json() == {
        "key": "DEPLOY_TOKEN",
        "variable_type": "env_var",
        "value": "token-one",
        "protected": False,
        "masked": True,
        "hidden": False,
        "raw": True,
        "environment_scope": "*",
        "description": "deployment credential",
    }

    scoped = await client.post(
        f"{API}/groups/{group_id}/variables",
        json={
            "key": "DEPLOY_TOKEN",
            "value": "token-prod",
            "environment_scope": "production",
            "protected": True,
            "variable_type": "file",
        },
        headers=auth_headers(test_token),
    )
    assert scoped.status_code == 201

    duplicate = await client.post(
        f"{API}/groups/{group_id}/variables",
        json={"key": "DEPLOY_TOKEN", "value": "again"},
        headers=auth_headers(test_token),
    )
    assert duplicate.status_code == 400

    listed = await client.get(
        f"{API}/groups/{group_id}/variables",
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert [(item["key"], item["environment_scope"]) for item in listed.json()] == [
        ("DEPLOY_TOKEN", "*"),
        ("DEPLOY_TOKEN", "production"),
    ]

    listed_prod = await client.get(
        f"{API}/groups/{group_id}/variables",
        params={"filter[environment_scope]": "production"},
        headers=auth_headers(test_token),
    )
    assert listed_prod.status_code == 200
    assert [
        (item["key"], item["environment_scope"]) for item in listed_prod.json()
    ] == [
        ("DEPLOY_TOKEN", "production"),
    ]

    get_scoped = await client.get(
        f"{API}/groups/{group_id}/variables/DEPLOY_TOKEN",
        params={"filter[environment_scope]": "production"},
        headers=auth_headers(test_token),
    )
    assert get_scoped.status_code == 200
    assert get_scoped.json()["value"] == "token-prod"
    assert get_scoped.json()["variable_type"] == "file"
    assert get_scoped.json()["protected"] is True

    updated = await client.put(
        f"{API}/groups/{group_id}/variables/DEPLOY_TOKEN",
        json={"value": "token-two", "masked": False, "description": "updated"},
        headers=auth_headers(test_token),
    )
    assert updated.status_code == 200
    assert updated.json()["value"] == "token-two"
    assert updated.json()["masked"] is False
    assert updated.json()["description"] == "updated"

    deleted = await client.delete(
        f"{API}/groups/{group_id}/variables/DEPLOY_TOKEN",
        headers=auth_headers(test_token),
    )
    assert deleted.status_code == 204

    missing = await client.get(
        f"{API}/groups/{group_id}/variables/DEPLOY_TOKEN",
        headers=auth_headers(test_token),
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_group_variable_hidden_value_is_not_read_back(
    client, test_user, test_token
):
    group = await client.post(
        f"{API}/groups",
        json={"path": "hidden-variable-group", "name": "Hidden Variable Group"},
        headers=auth_headers(test_token),
    )
    assert group.status_code == 201

    created = await client.post(
        f"{API}/groups/hidden-variable-group/variables",
        json={"key": "SECRET_TOKEN", "value": "super-secret-value", "hidden": True},
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201
    assert created.json()["value"] is None
    assert created.json()["masked"] is True
    assert created.json()["hidden"] is True

    fetched = await client.get(
        f"{API}/groups/hidden-variable-group/variables/SECRET_TOKEN",
        headers=auth_headers(test_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["value"] is None
    assert "super-secret-value" not in json.dumps(fetched.json())


@pytest.mark.asyncio
async def test_group_variable_rejects_invalid_key(client, test_user, test_token):
    group = await client.post(
        f"{API}/groups",
        json={"path": "invalid-variable-group", "name": "Invalid Variable Group"},
        headers=auth_headers(test_token),
    )
    assert group.status_code == 201

    resp = await client.post(
        f"{API}/groups/{group.json()['id']}/variables",
        json={"key": "BAD KEY", "value": "value"},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_group_ci_variables_and_secrets_require_maintainer(
    client, db_session, test_token
):
    maintainer, maintainer_token = await _create_user_and_token(
        db_session, "group-maintainer"
    )
    developer, developer_token = await _create_user_and_token(
        db_session, "group-developer"
    )
    guest, guest_token = await _create_user_and_token(db_session, "group-guest")
    group = await client.post(
        f"{API}/groups",
        json={"path": "role-bound-group-ci", "name": "Role Bound Group CI"},
        headers=auth_headers(test_token),
    )
    assert group.status_code == 201
    group_id = group.json()["id"]

    for user, level in ((maintainer, 40), (developer, 30), (guest, 10)):
        resp = await client.post(
            f"{API}/groups/{group_id}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert resp.status_code == 201
        assert resp.json()["access_level"] == level

    allowed_variable = await client.post(
        f"{API}/groups/{group_id}/variables",
        json={"key": "MAINTAINER_ONLY", "value": "allowed"},
        headers=auth_headers(maintainer_token),
    )
    assert allowed_variable.status_code == 201

    allowed_secret = await client.post(
        f"{API}/groups/{group_id}/secrets",
        json={"name": "MAINTAINER_SECRET", "value": "allowed"},
        headers=auth_headers(maintainer_token),
    )
    assert allowed_secret.status_code == 201

    for token in (developer_token, guest_token):
        denied_variable = await client.post(
            f"{API}/groups/{group_id}/variables",
            json={"key": "DENIED", "value": "nope"},
            headers=auth_headers(token),
        )
        assert denied_variable.status_code == 403

        denied_secret = await client.post(
            f"{API}/groups/{group_id}/secrets",
            json={"name": "DENIED_SECRET", "value": "nope"},
            headers=auth_headers(token),
        )
        assert denied_secret.status_code == 403


@pytest.mark.asyncio
async def test_group_variables_accept_encoded_nested_group_path(
    client, test_user, test_token
):
    parent = await client.post(
        f"{API}/groups",
        json={"path": "variable-parent", "name": "Variable Parent"},
        headers=auth_headers(test_token),
    )
    assert parent.status_code == 201
    child = await client.post(
        f"{API}/groups",
        json={
            "path": "child",
            "name": "Variable Child",
            "parent_id": parent.json()["id"],
        },
        headers=auth_headers(test_token),
    )
    assert child.status_code == 201

    created = await client.post(
        f"{API}/groups/variable-parent%2Fchild/variables",
        json={"key": "NESTED_TOKEN", "value": "nested"},
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201

    fetched = await client.get(
        f"{API}/groups/variable-parent%2Fchild/variables/NESTED_TOKEN",
        headers=auth_headers(test_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["value"] == "nested"


@pytest.mark.asyncio
async def test_site_admin_can_manage_group_variables(
    client, test_user, test_token, admin_token
):
    group = await client.post(
        f"{API}/groups",
        json={"path": "admin-variable-group", "name": "Admin Variable Group"},
        headers=auth_headers(test_token),
    )
    assert group.status_code == 201

    created = await client.post(
        f"{API}/groups/{group.json()['id']}/variables",
        json={"key": "ADMIN_TOKEN", "value": "admin"},
        headers=auth_headers(admin_token),
    )

    assert created.status_code == 201
    assert created.json()["value"] == "admin"


@pytest.mark.asyncio
async def test_group_secrets_crud_scope_and_hidden_value(client, test_user, test_token):
    group = await client.post(
        f"{API}/groups",
        json={"path": "secret-group", "name": "Secret Group"},
        headers=auth_headers(test_token),
    )
    assert group.status_code == 201
    group_id = group.json()["id"]

    created = await client.post(
        f"{API}/groups/{group_id}/secrets",
        json={
            "name": "DATABASE_PASSWORD",
            "value": "super-secret-value",
            "description": "database credential",
            "rotation_reminder_days": 30,
        },
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201
    data = created.json()
    assert data["name"] == "DATABASE_PASSWORD"
    assert data["value"] is None
    assert data["description"] == "database credential"
    assert data["environment_scope"] == "*"
    assert data["branch_scope"] == "*"
    assert data["rotation_reminder_days"] == 30
    assert "super-secret-value" not in json.dumps(data)

    scoped = await client.post(
        f"{API}/groups/{group_id}/secrets",
        json={
            "name": "DATABASE_PASSWORD",
            "value": "prod-secret",
            "environment_scope": "production",
            "branch_scope": "main",
            "protected": True,
        },
        headers=auth_headers(test_token),
    )
    assert scoped.status_code == 201

    duplicate = await client.post(
        f"{API}/groups/{group_id}/secrets",
        json={"name": "DATABASE_PASSWORD", "value": "again"},
        headers=auth_headers(test_token),
    )
    assert duplicate.status_code == 400

    listed = await client.get(
        f"{API}/groups/{group_id}/secrets",
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert [
        (item["name"], item["environment_scope"], item["branch_scope"])
        for item in listed.json()
    ] == [
        ("DATABASE_PASSWORD", "*", "*"),
        ("DATABASE_PASSWORD", "production", "main"),
    ]

    fetched = await client.get(
        f"{API}/groups/{group_id}/secrets/DATABASE_PASSWORD",
        params={
            "filter[environment_scope]": "production",
            "filter[branch_scope]": "main",
        },
        headers=auth_headers(test_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["value"] is None
    assert fetched.json()["protected"] is True

    updated = await client.put(
        f"{API}/groups/{group_id}/secrets/DATABASE_PASSWORD",
        json={"description": "updated", "status": "rotating"},
        headers=auth_headers(test_token),
    )
    assert updated.status_code == 200
    assert updated.json()["description"] == "updated"
    assert updated.json()["status"] == "rotating"

    deleted = await client.delete(
        f"{API}/groups/{group_id}/secrets/DATABASE_PASSWORD",
        headers=auth_headers(test_token),
    )
    assert deleted.status_code == 204

    missing = await client.get(
        f"{API}/groups/{group_id}/secrets/DATABASE_PASSWORD",
        headers=auth_headers(test_token),
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_group_secrets_accept_encoded_nested_group_path(
    client, test_user, test_token
):
    parent = await client.post(
        f"{API}/groups",
        json={"path": "secret-parent", "name": "Secret Parent"},
        headers=auth_headers(test_token),
    )
    assert parent.status_code == 201
    child = await client.post(
        f"{API}/groups",
        json={
            "path": "child",
            "name": "Secret Child",
            "parent_id": parent.json()["id"],
        },
        headers=auth_headers(test_token),
    )
    assert child.status_code == 201

    created = await client.post(
        f"{API}/groups/secret-parent%2Fchild/secrets",
        json={"name": "NESTED_SECRET", "value": "nested"},
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201

    fetched = await client.get(
        f"{API}/groups/secret-parent%2Fchild/secrets/NESTED_SECRET",
        headers=auth_headers(test_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["value"] is None


@pytest.mark.asyncio
async def test_group_secret_rejects_invalid_name(client, test_user, test_token):
    group = await client.post(
        f"{API}/groups",
        json={"path": "invalid-secret-group", "name": "Invalid Secret Group"},
        headers=auth_headers(test_token),
    )
    assert group.status_code == 201

    resp = await client.post(
        f"{API}/groups/{group.json()['id']}/secrets",
        json={"name": "BAD NAME", "value": "value"},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 400


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
