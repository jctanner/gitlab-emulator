"""Tests for GitLab-shaped project API endpoints."""

import asyncio
import hashlib
import json
import os
import secrets

import pytest

from tests.conftest import auth_headers

API = "/api/v4"


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
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    db_session.add(
        PersonalAccessToken(
            user_id=user.id,
            name=f"{login}-token",
            token_hash=token_hash,
            token_prefix=raw_token[:8],
            scopes=["repo", "user"],
        )
    )
    await db_session.commit()
    return user, raw_token


@pytest.mark.asyncio
async def test_create_project_returns_gitlab_shape(client, test_user, test_token):
    resp = await client.post(
        f"{API}/projects",
        json={
            "name": "Project Display Name",
            "path": "gitlab-project",
            "description": "created through GitLab API",
            "visibility": "private",
            "initialize_with_readme": True,
        },
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 201
    data = resp.json()
    assert data["id"]
    assert data["name"] == "gitlab-project"
    assert data["path"] == "gitlab-project"
    assert data["path_with_namespace"] == "testuser/gitlab-project"
    assert data["name_with_namespace"] == "testuser/gitlab-project"
    assert data["visibility"] == "private"
    assert data["namespace"]["path"] == "testuser"
    assert data["namespace"]["full_path"] == "testuser"
    assert data["http_url_to_repo"] == "http://testserver/testuser/gitlab-project.git"
    assert data["ssh_url_to_repo"] == "git@testserver:testuser/gitlab-project.git"
    assert data["updated_at"] is not None
    assert data["permissions"]["project_access"] is None
    assert data["statistics"]["repository_size"] == 0
    assert data["import_status"] == "none"
    assert data["_links"]["repo_branches"].endswith(
        f"/api/v4/projects/{data['id']}/repository/branches"
    )


@pytest.mark.asyncio
async def test_get_project_by_id(client, test_user, test_token):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "get-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    project_id = create_resp.json()["id"]

    resp = await client.get(
        f"{API}/projects/{project_id}",
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == project_id
    assert data["path_with_namespace"] == "testuser/get-project"
    assert data["default_branch"] == "main"


@pytest.mark.asyncio
async def test_create_project_normalizes_blank_default_branch(
    client, test_user, test_token
):
    resp = await client.post(
        f"{API}/projects",
        json={"name": "blank-default-branch", "default_branch": ""},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 201
    data = resp.json()
    assert data["default_branch"] == "main"

    branches = await client.get(
        f"{API}/projects/{data['id']}/repository/branches",
        headers=auth_headers(test_token),
    )
    assert branches.status_code == 200


@pytest.mark.asyncio
async def test_get_project_by_url_encoded_path(client, test_user, test_token):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "path-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    project_id = create_resp.json()["id"]

    resp = await client.get(
        f"{API}/projects/testuser%2Fpath-project",
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == project_id
    assert data["path_with_namespace"] == "testuser/path-project"

    double_encoded = await client.get(
        f"{API}/projects/testuser%252Fpath-project",
        headers=auth_headers(test_token),
    )
    assert double_encoded.status_code == 200
    assert double_encoded.json()["id"] == project_id


@pytest.mark.asyncio
async def test_list_user_projects(client, test_user, test_token):
    for name in ("project-list-a", "project-list-b"):
        resp = await client.post(
            f"{API}/projects",
            json={"name": name},
            headers=auth_headers(test_token),
        )
        assert resp.status_code == 201

    resp = await client.get(
        f"{API}/users/{test_user.id}/projects",
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    names = {project["path"] for project in resp.json()}
    assert {"project-list-a", "project-list-b"}.issubset(names)


@pytest.mark.asyncio
async def test_list_projects_supports_search_and_delete(client, test_user, test_token):
    first = await client.post(
        f"{API}/projects",
        json={"name": "global-list-keep"},
        headers=auth_headers(test_token),
    )
    assert first.status_code == 201
    second = await client.post(
        f"{API}/projects",
        json={"name": "global-list-delete"},
        headers=auth_headers(test_token),
    )
    assert second.status_code == 201
    no_issues = await client.post(
        f"{API}/projects",
        json={"name": "global-list-noissues", "issues_enabled": False},
        headers=auth_headers(test_token),
    )
    assert no_issues.status_code == 201

    listed = await client.get(
        f"{API}/projects",
        params={
            "search": "global-list",
            "with_issues_enabled": True,
            "visibility": "public",
            "order_by": "path",
            "sort": "desc",
            "page": 1,
            "per_page": 20,
        },
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert listed.headers["X-Page"] == "1"
    assert listed.headers["X-Per-Page"] == "20"
    assert "X-Next-Page" in listed.headers
    assert {project["path"] for project in listed.json()} == {
        "global-list-keep",
        "global-list-delete",
    }

    by_ids = await client.get(
        f"{API}/projects",
        params=[
            ("ids", first.json()["id"]),
            ("ids", no_issues.json()["id"]),
            ("owned", "true"),
            ("per_page", "1"),
        ],
        headers=auth_headers(test_token),
    )
    assert by_ids.status_code == 200
    assert by_ids.headers["X-Total"] == "2"
    assert len(by_ids.json()) == 1

    deleted = await client.delete(
        f"{API}/projects/{second.json()['id']}",
        headers=auth_headers(test_token),
    )
    assert deleted.status_code == 202

    missing = await client.get(
        f"{API}/projects/{second.json()['id']}",
        headers=auth_headers(test_token),
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_project_variables_crud_and_environment_scope(
    client, test_user, test_token
):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "variable-project"},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201
    project_id = create_resp.json()["id"]

    created = await client.post(
        f"{API}/projects/{project_id}/variables",
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
        f"{API}/projects/{project_id}/variables",
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
        f"{API}/projects/{project_id}/variables",
        json={"key": "DEPLOY_TOKEN", "value": "again"},
        headers=auth_headers(test_token),
    )
    assert duplicate.status_code == 400

    listed = await client.get(
        f"{API}/projects/{project_id}/variables",
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert [(item["key"], item["environment_scope"]) for item in listed.json()] == [
        ("DEPLOY_TOKEN", "*"),
        ("DEPLOY_TOKEN", "production"),
    ]

    get_scoped = await client.get(
        f"{API}/projects/{project_id}/variables/DEPLOY_TOKEN",
        params={"filter[environment_scope]": "production"},
        headers=auth_headers(test_token),
    )
    assert get_scoped.status_code == 200
    assert get_scoped.json()["value"] == "token-prod"
    assert get_scoped.json()["variable_type"] == "file"
    assert get_scoped.json()["protected"] is True

    updated = await client.put(
        f"{API}/projects/{project_id}/variables/DEPLOY_TOKEN",
        json={"value": "token-two", "masked": False, "description": "updated"},
        headers=auth_headers(test_token),
    )
    assert updated.status_code == 200
    assert updated.json()["value"] == "token-two"
    assert updated.json()["masked"] is False
    assert updated.json()["description"] == "updated"

    deleted = await client.delete(
        f"{API}/projects/{project_id}/variables/DEPLOY_TOKEN",
        headers=auth_headers(test_token),
    )
    assert deleted.status_code == 204

    missing = await client.get(
        f"{API}/projects/{project_id}/variables/DEPLOY_TOKEN",
        headers=auth_headers(test_token),
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_project_variable_hidden_value_is_not_read_back(
    client, test_user, test_token
):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "hidden-variable-project"},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201
    project_path = "testuser%2Fhidden-variable-project"

    created = await client.post(
        f"{API}/projects/{project_path}/variables",
        json={"key": "SECRET_TOKEN", "value": "super-secret-value", "hidden": True},
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201
    assert created.json()["value"] is None
    assert created.json()["masked"] is True
    assert created.json()["hidden"] is True

    fetched = await client.get(
        f"{API}/projects/{project_path}/variables/SECRET_TOKEN",
        headers=auth_headers(test_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["value"] is None
    assert "super-secret-value" not in json.dumps(fetched.json())


@pytest.mark.asyncio
async def test_project_variable_rejects_invalid_key(client, test_user, test_token):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "invalid-variable-project"},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201
    project_id = create_resp.json()["id"]

    resp = await client.post(
        f"{API}/projects/{project_id}/variables",
        json={"key": "BAD KEY", "value": "value"},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_project_ci_variables_and_secrets_require_maintainer(
    client, db_session, test_user, test_token
):
    maintainer, maintainer_token = await _create_user_and_token(
        db_session, "project-maintainer"
    )
    developer, developer_token = await _create_user_and_token(
        db_session, "project-developer"
    )
    guest, guest_token = await _create_user_and_token(db_session, "project-guest")
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "role-bound-project-ci"},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201
    project_id = create_resp.json()["id"]

    for user, level in ((maintainer, 40), (developer, 30), (guest, 10)):
        resp = await client.post(
            f"{API}/projects/{project_id}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert resp.status_code == 201

    allowed_variable = await client.post(
        f"{API}/projects/{project_id}/variables",
        json={"key": "MAINTAINER_ONLY", "value": "allowed"},
        headers=auth_headers(maintainer_token),
    )
    assert allowed_variable.status_code == 201

    allowed_secret = await client.post(
        f"{API}/projects/{project_id}/secrets",
        json={"name": "MAINTAINER_SECRET", "value": "allowed"},
        headers=auth_headers(maintainer_token),
    )
    assert allowed_secret.status_code == 201

    for token in (developer_token, guest_token):
        denied_variable = await client.post(
            f"{API}/projects/{project_id}/variables",
            json={"key": "DENIED", "value": "nope"},
            headers=auth_headers(token),
        )
        assert denied_variable.status_code == 403

        denied_secret = await client.post(
            f"{API}/projects/{project_id}/secrets",
            json={"name": "DENIED_SECRET", "value": "nope"},
            headers=auth_headers(token),
        )
        assert denied_secret.status_code == 403


@pytest.mark.asyncio
async def test_project_secrets_crud_scope_and_hidden_value(
    client, test_user, test_token
):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "secret-project"},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201
    project_id = create_resp.json()["id"]

    created = await client.post(
        f"{API}/projects/{project_id}/secrets",
        json={
            "name": "DATABASE_PASSWORD",
            "value": "super-secret-value",
            "description": "database credential",
            "rotation_reminder_days": 30,
        },
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201
    created_data = created.json()
    assert created_data["name"] == "DATABASE_PASSWORD"
    assert created_data["value"] is None
    assert created_data["description"] == "database credential"
    assert created_data["environment_scope"] == "*"
    assert created_data["branch_scope"] == "*"
    assert created_data["rotation_reminder_days"] == 30
    assert "super-secret-value" not in json.dumps(created_data)

    scoped = await client.post(
        f"{API}/projects/{project_id}/secrets",
        json={
            "name": "DATABASE_PASSWORD",
            "value": "prod-secret",
            "environment_scope": "production",
            "branch_scope": "main",
            "protected": True,
            "status": "healthy",
        },
        headers=auth_headers(test_token),
    )
    assert scoped.status_code == 201

    duplicate = await client.post(
        f"{API}/projects/{project_id}/secrets",
        json={"name": "DATABASE_PASSWORD", "value": "again"},
        headers=auth_headers(test_token),
    )
    assert duplicate.status_code == 400

    listed = await client.get(
        f"{API}/projects/{project_id}/secrets",
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert [(item["name"], item["environment_scope"], item["branch_scope"]) for item in listed.json()] == [
        ("DATABASE_PASSWORD", "*", "*"),
        ("DATABASE_PASSWORD", "production", "main"),
    ]

    fetched = await client.get(
        f"{API}/projects/{project_id}/secrets/DATABASE_PASSWORD",
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
        f"{API}/projects/{project_id}/secrets/DATABASE_PASSWORD",
        json={"description": "updated", "status": "rotating"},
        headers=auth_headers(test_token),
    )
    assert updated.status_code == 200
    assert updated.json()["description"] == "updated"
    assert updated.json()["status"] == "rotating"

    deleted = await client.delete(
        f"{API}/projects/{project_id}/secrets/DATABASE_PASSWORD",
        headers=auth_headers(test_token),
    )
    assert deleted.status_code == 204

    missing = await client.get(
        f"{API}/projects/{project_id}/secrets/DATABASE_PASSWORD",
        headers=auth_headers(test_token),
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_project_secret_rejects_invalid_name(client, test_user, test_token):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "invalid-secret-project"},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201

    resp = await client.post(
        f"{API}/projects/{create_resp.json()['id']}/secrets",
        json={"name": "BAD NAME", "value": "value"},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_project_ci_security_settings_round_trip(client, test_user, test_token):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "ci-security-settings-project"},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201
    project_id = create_resp.json()["id"]

    defaults = await client.get(
        f"{API}/projects/{project_id}/ci/security_settings",
        headers=auth_headers(test_token),
    )
    assert defaults.status_code == 200
    assert defaults.json() == {
        "ci_pipeline_variables_minimum_override_role": "developer",
        "ci_strict_security_mode": False,
    }

    updated = await client.put(
        f"{API}/projects/{project_id}/ci/security_settings",
        headers=auth_headers(test_token),
        json={
            "ci_pipeline_variables_minimum_override_role": "no_one_allowed",
            "ci_strict_security_mode": True,
        },
    )
    assert updated.status_code == 200
    assert updated.json() == {
        "ci_pipeline_variables_minimum_override_role": "no_one_allowed",
        "ci_strict_security_mode": True,
    }

    project = await client.get(
        f"{API}/projects/{project_id}",
        headers=auth_headers(test_token),
    )
    assert project.status_code == 200
    assert project.json()["ci_security_settings"]["ci_strict_security_mode"] is True


@pytest.mark.asyncio
async def test_create_project_in_group_namespace_by_id(client, test_user, test_token):
    org_resp = await client.post(
        f"{API}/orgs",
        json={"login": "team-space", "name": "Team Space"},
        headers=auth_headers(test_token),
    )
    assert org_resp.status_code == 201
    namespace_id = org_resp.json()["id"]

    resp = await client.post(
        f"{API}/projects",
        json={
            "name": "group-project",
            "namespace_id": namespace_id,
            "initialize_with_readme": True,
        },
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 201
    data = resp.json()
    assert data["path_with_namespace"] == "team-space/group-project"
    assert data["namespace"]["id"] == namespace_id
    assert data["namespace"]["path"] == "team-space"
    assert data["namespace"]["kind"] == "group"
    assert data["namespace"]["name"] == "Team Space"
    assert data["http_url_to_repo"] == "http://testserver/team-space/group-project.git"

    path_resp = await client.get(
        f"{API}/projects/team-space%2Fgroup-project",
        headers=auth_headers(test_token),
    )
    assert path_resp.status_code == 200
    assert path_resp.json()["id"] == data["id"]

    upload_refs = await client.get(
        "/team-space/group-project.git/info/refs?service=git-upload-pack"
    )
    assert upload_refs.status_code == 200
    assert b"# service=git-upload-pack" in upload_refs.content


@pytest.mark.asyncio
async def test_create_project_in_group_namespace_by_path(client, test_user, test_token):
    org_resp = await client.post(
        f"{API}/orgs",
        json={"login": "path-space", "name": "Path Space"},
        headers=auth_headers(test_token),
    )
    assert org_resp.status_code == 201

    resp = await client.post(
        f"{API}/projects",
        json={
            "name": "path-group-project",
            "namespace_path": "path-space",
        },
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 201
    data = resp.json()
    assert data["path_with_namespace"] == "path-space/path-group-project"
    assert data["namespace"]["kind"] == "group"
    assert data["namespace"]["path"] == "path-space"


@pytest.mark.asyncio
async def test_create_project_in_nested_group_namespace(client, test_user, test_token):
    parent = await client.post(
        f"{API}/groups",
        json={"path": "parent-space", "name": "Parent Space"},
        headers=auth_headers(test_token),
    )
    assert parent.status_code == 201
    child = await client.post(
        f"{API}/groups",
        json={
            "path": "child-space",
            "name": "Child Space",
            "parent_id": parent.json()["id"],
        },
        headers=auth_headers(test_token),
    )
    assert child.status_code == 201

    resp = await client.post(
        f"{API}/projects",
        json={
            "name": "nested-project",
            "namespace_path": "parent-space%2Fchild-space",
        },
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 201
    data = resp.json()
    assert data["path_with_namespace"] == "parent-space/child-space/nested-project"
    assert data["namespace"]["path"] == "child-space"
    assert data["namespace"]["full_path"] == "parent-space/child-space"
    assert data["namespace"]["parent_id"] == parent.json()["id"]


@pytest.mark.asyncio
async def test_project_branches_list_from_bare_repo(client, test_user, test_token):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "branch-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    project_id = create_resp.json()["id"]

    resp = await client.get(
        f"{API}/projects/{project_id}/repository/branches",
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    branches = resp.json()
    assert resp.headers["X-Total"] == "1"
    assert [branch["name"] for branch in branches] == ["main"]
    assert branches[0]["default"] is True
    assert len(branches[0]["commit"]["id"]) == 40
    assert branches[0]["commit"]["web_url"].endswith(
        f"/-/commit/{branches[0]['commit']['id']}"
    )
    assert branches[0]["commit"]["trailers"] == {}


@pytest.mark.asyncio
async def test_project_branches_list_by_url_encoded_path(
    client, test_user, test_token
):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "branch-path-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201

    resp = await client.get(
        f"{API}/projects/testuser%2Fbranch-path-project/repository/branches",
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    branches = resp.json()
    assert [branch["name"] for branch in branches] == ["main"]


@pytest.mark.asyncio
async def test_project_branch_get_create_and_delete(client, test_user, test_token):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "branch-crud-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201
    project_id = create_resp.json()["id"]

    create_branch = await client.post(
        f"{API}/projects/{project_id}/repository/branches",
        json={"branch": "feature/test", "ref": "main"},
        headers=auth_headers(test_token),
    )
    assert create_branch.status_code == 201
    branch = create_branch.json()
    assert branch["name"] == "feature/test"
    assert branch["default"] is False
    assert len(branch["commit"]["id"]) == 40

    get_branch = await client.get(
        f"{API}/projects/{project_id}/repository/branches/feature%2Ftest",
        headers=auth_headers(test_token),
    )
    assert get_branch.status_code == 200
    assert get_branch.json()["name"] == "feature/test"

    list_branches = await client.get(
        f"{API}/projects/{project_id}/repository/branches",
        headers=auth_headers(test_token),
    )
    assert list_branches.status_code == 200
    assert [item["name"] for item in list_branches.json()] == ["feature/test", "main"]

    delete_branch = await client.delete(
        f"{API}/projects/{project_id}/repository/branches/feature%2Ftest",
        headers=auth_headers(test_token),
    )
    assert delete_branch.status_code == 200
    assert delete_branch.json()["branch_name"] == "feature/test"

    missing_branch = await client.get(
        f"{API}/projects/{project_id}/repository/branches/feature%2Ftest",
        headers=auth_headers(test_token),
    )
    assert missing_branch.status_code == 404


@pytest.mark.asyncio
async def test_project_branch_create_accepts_encoded_project_path(
    client, test_user, test_token
):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "branch-crud-path-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201

    create_branch = await client.post(
        f"{API}/projects/testuser%2Fbranch-crud-path-project/repository/branches",
        params={"branch": "release-1", "ref": "main"},
        headers=auth_headers(test_token),
    )
    assert create_branch.status_code == 201
    assert create_branch.json()["name"] == "release-1"

    get_branch = await client.get(
        f"{API}/projects/testuser%2Fbranch-crud-path-project/repository/branches/release-1",
        headers=auth_headers(test_token),
    )
    assert get_branch.status_code == 200
    assert get_branch.json()["name"] == "release-1"


@pytest.mark.asyncio
async def test_project_branch_create_rejects_duplicate(client, test_user, test_token):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "branch-duplicate-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201
    project_id = create_resp.json()["id"]

    resp = await client.post(
        f"{API}/projects/{project_id}/repository/branches",
        json={"branch": "main", "ref": "main"},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_project_branch_delete_rejects_default_branch(client, test_user, test_token):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "branch-default-delete-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201
    project_id = create_resp.json()["id"]

    resp = await client.delete(
        f"{API}/projects/{project_id}/repository/branches/main",
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_project_repository_writes_require_developer(
    client, db_session, test_user, test_token
):
    developer, developer_token = await _create_user_and_token(
        db_session, "repo-write-developer"
    )
    reporter, reporter_token = await _create_user_and_token(
        db_session, "repo-write-reporter"
    )
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "repo-write-roles", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201
    project_id = create_resp.json()["id"]

    for user, level in ((developer, 30), (reporter, 20)):
        member = await client.post(
            f"{API}/projects/{project_id}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201

    branch_allowed = await client.post(
        f"{API}/projects/{project_id}/repository/branches",
        json={"branch": "developer-branch", "ref": "main"},
        headers=auth_headers(developer_token),
    )
    assert branch_allowed.status_code == 201

    tag_allowed = await client.post(
        f"{API}/projects/{project_id}/repository/tags",
        json={"tag_name": "developer-tag", "ref": "main"},
        headers=auth_headers(developer_token),
    )
    assert tag_allowed.status_code == 201

    branch_denied = await client.post(
        f"{API}/projects/{project_id}/repository/branches",
        json={"branch": "reporter-branch", "ref": "main"},
        headers=auth_headers(reporter_token),
    )
    assert branch_denied.status_code == 403

    tag_denied = await client.post(
        f"{API}/projects/{project_id}/repository/tags",
        json={"tag_name": "reporter-tag", "ref": "main"},
        headers=auth_headers(reporter_token),
    )
    assert tag_denied.status_code == 403


@pytest.mark.asyncio
async def test_project_protected_branch_crud(client, test_user, test_token):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "protected-branch-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201
    project_id = create_resp.json()["id"]

    protected = await client.post(
        f"{API}/projects/{project_id}/protected_branches",
        json={
            "name": "main",
            "push_access_level": 40,
            "merge_access_level": 30,
            "allow_force_push": True,
        },
        headers=auth_headers(test_token),
    )
    assert protected.status_code == 201
    data = protected.json()
    assert data["name"] == "main"
    assert data["push_access_levels"][0]["access_level"] == 40
    assert data["merge_access_levels"][0]["access_level"] == 30
    assert data["allow_force_push"] is True
    assert data["unprotect_access_levels"][0]["access_level"] == 40
    assert data["code_owner_approval_required"] is False
    assert data["inherited"] is False
    assert data["web_url"].endswith("/protected-branch-project/-/branches/main")

    branch = await client.get(
        f"{API}/projects/{project_id}/repository/branches/main",
        headers=auth_headers(test_token),
    )
    assert branch.status_code == 200
    assert branch.json()["protected"] is True

    listed = await client.get(
        f"{API}/projects/{project_id}/protected_branches",
        params={"page": 1, "per_page": 1},
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert listed.headers["X-Total"] == "1"
    assert [item["name"] for item in listed.json()] == ["main"]

    fetched = await client.get(
        f"{API}/projects/{project_id}/protected_branches/main",
        headers=auth_headers(test_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "main"

    deleted = await client.delete(
        f"{API}/projects/{project_id}/protected_branches/main",
        headers=auth_headers(test_token),
    )
    assert deleted.status_code == 204

    missing = await client.get(
        f"{API}/projects/{project_id}/protected_branches/main",
        headers=auth_headers(test_token),
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_project_protected_branch_management_requires_maintainer(
    client, db_session, test_user, test_token
):
    maintainer, maintainer_token = await _create_user_and_token(
        db_session, "protected-maintainer"
    )
    developer, developer_token = await _create_user_and_token(
        db_session, "protected-developer"
    )
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "protected-role-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201
    project_id = create_resp.json()["id"]

    for user, level in ((maintainer, 40), (developer, 30)):
        member = await client.post(
            f"{API}/projects/{project_id}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201

    denied = await client.post(
        f"{API}/projects/{project_id}/protected_branches",
        json={"name": "main"},
        headers=auth_headers(developer_token),
    )
    assert denied.status_code == 403

    protected = await client.post(
        f"{API}/projects/{project_id}/protected_branches",
        json={"name": "main"},
        headers=auth_headers(maintainer_token),
    )
    assert protected.status_code == 201

    unprotect_denied = await client.delete(
        f"{API}/projects/{project_id}/protected_branches/main",
        headers=auth_headers(developer_token),
    )
    assert unprotect_denied.status_code == 403

    unprotected = await client.delete(
        f"{API}/projects/{project_id}/protected_branches/main",
        headers=auth_headers(maintainer_token),
    )
    assert unprotected.status_code == 204


@pytest.mark.asyncio
async def test_project_protected_branch_accepts_encoded_project_path(
    client, test_user, test_token
):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "protected-branch-path-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201

    protected = await client.post(
        f"{API}/projects/testuser%2Fprotected-branch-path-project/protected_branches",
        params={"name": "main"},
        headers=auth_headers(test_token),
    )
    assert protected.status_code == 201
    assert protected.json()["name"] == "main"


@pytest.mark.asyncio
async def test_project_branch_create_requires_auth(client, test_user, test_token):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "branch-auth-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201
    project_id = create_resp.json()["id"]

    resp = await client.post(
        f"{API}/projects/{project_id}/repository/branches",
        json={"branch": "feature", "ref": "main"},
    )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_project_created_via_gitlab_api_supports_git_smart_http(
    client, test_user, test_token
):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "smart-http-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201

    upload_refs = await client.get(
        "/testuser/smart-http-project.git/info/refs?service=git-upload-pack"
    )
    assert upload_refs.status_code == 200
    assert b"# service=git-upload-pack" in upload_refs.content

    receive_refs = await client.get(
        "/testuser/smart-http-project.git/info/refs?service=git-receive-pack",
        headers=auth_headers(test_token),
    )
    assert receive_refs.status_code == 200
    assert b"# service=git-receive-pack" in receive_refs.content


@pytest.mark.asyncio
async def test_project_path_route_does_not_shadow_pipeline_routes(
    client, test_user, test_token
):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "pipeline-route-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    project_id = create_resp.json()["id"]

    resp = await client.post(
        f"{API}/projects/{project_id}/pipeline",
        json={
            "ref": "main",
            "job": {
                "name": "route-check",
                "image": "alpine:3.20",
                "script": ["echo route-check"],
            },
        },
    )

    assert resp.status_code == 201
    assert resp.json()["project_id"] == project_id


@pytest.mark.asyncio
async def test_project_tags_list_from_bare_repo(client, test_user, test_token):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "tag-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    project = create_resp.json()

    repo_path = os.path.join(
        os.environ["GITLAB_EMULATOR_DATA_DIR"],
        "repos",
        "testuser",
        "tag-project.git",
    )
    proc = await asyncio.create_subprocess_exec(
        "git",
        "--git-dir",
        repo_path,
        "tag",
        "v1.0.0",
        "main",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    assert proc.returncode == 0

    resp = await client.get(
        f"{API}/projects/{project['id']}/repository/tags",
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    tags = resp.json()
    assert len(tags) == 1
    assert tags[0]["name"] == "v1.0.0"
    assert len(tags[0]["target"]) == 40
    assert tags[0]["commit"]["web_url"].endswith(f"/-/commit/{tags[0]['target']}")
    assert tags[0]["commit"]["extended_trailers"] == {}
    assert tags[0]["created_at"] is None


@pytest.mark.asyncio
async def test_project_tags_list_by_url_encoded_path(client, test_user, test_token):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "tag-path-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201

    repo_path = os.path.join(
        os.environ["GITLAB_EMULATOR_DATA_DIR"],
        "repos",
        "testuser",
        "tag-path-project.git",
    )
    proc = await asyncio.create_subprocess_exec(
        "git",
        "--git-dir",
        repo_path,
        "tag",
        "v2.0.0",
        "main",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    assert proc.returncode == 0

    resp = await client.get(
        f"{API}/projects/testuser%2Ftag-path-project/repository/tags",
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    tags = resp.json()
    assert len(tags) == 1
    assert tags[0]["name"] == "v2.0.0"


@pytest.mark.asyncio
async def test_project_tag_get_create_and_delete(client, test_user, test_token):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "tag-crud-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201
    project_id = create_resp.json()["id"]

    create_tag = await client.post(
        f"{API}/projects/{project_id}/repository/tags",
        json={"tag_name": "v1.2.3", "ref": "main"},
        headers=auth_headers(test_token),
    )
    assert create_tag.status_code == 201
    tag = create_tag.json()
    assert tag["name"] == "v1.2.3"
    assert tag["target"] == tag["commit"]["id"]
    assert len(tag["target"]) == 40

    get_tag = await client.get(
        f"{API}/projects/{project_id}/repository/tags/v1.2.3",
        headers=auth_headers(test_token),
    )
    assert get_tag.status_code == 200
    assert get_tag.json()["name"] == "v1.2.3"

    list_tags = await client.get(
        f"{API}/projects/{project_id}/repository/tags",
        headers=auth_headers(test_token),
    )
    assert list_tags.status_code == 200
    assert [item["name"] for item in list_tags.json()] == ["v1.2.3"]

    delete_tag = await client.delete(
        f"{API}/projects/{project_id}/repository/tags/v1.2.3",
        headers=auth_headers(test_token),
    )
    assert delete_tag.status_code == 200
    assert delete_tag.json()["tag_name"] == "v1.2.3"

    missing_tag = await client.get(
        f"{API}/projects/{project_id}/repository/tags/v1.2.3",
        headers=auth_headers(test_token),
    )
    assert missing_tag.status_code == 404


@pytest.mark.asyncio
async def test_project_tag_create_accepts_encoded_project_path(
    client, test_user, test_token
):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "tag-crud-path-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201

    create_tag = await client.post(
        f"{API}/projects/testuser%2Ftag-crud-path-project/repository/tags",
        params={"tag_name": "v2.3.4", "ref": "main"},
        headers=auth_headers(test_token),
    )
    assert create_tag.status_code == 201
    assert create_tag.json()["name"] == "v2.3.4"

    get_tag = await client.get(
        f"{API}/projects/testuser%2Ftag-crud-path-project/repository/tags/v2.3.4",
        headers=auth_headers(test_token),
    )
    assert get_tag.status_code == 200
    assert get_tag.json()["name"] == "v2.3.4"


@pytest.mark.asyncio
async def test_project_tag_create_rejects_duplicate(client, test_user, test_token):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "tag-duplicate-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201
    project_id = create_resp.json()["id"]

    first = await client.post(
        f"{API}/projects/{project_id}/repository/tags",
        json={"tag_name": "v1.0.0", "ref": "main"},
        headers=auth_headers(test_token),
    )
    assert first.status_code == 201

    duplicate = await client.post(
        f"{API}/projects/{project_id}/repository/tags",
        json={"tag_name": "v1.0.0", "ref": "main"},
        headers=auth_headers(test_token),
    )

    assert duplicate.status_code == 400


@pytest.mark.asyncio
async def test_project_tag_create_requires_auth(client, test_user, test_token):
    create_resp = await client.post(
        f"{API}/projects",
        json={"name": "tag-auth-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201
    project_id = create_resp.json()["id"]

    resp = await client.post(
        f"{API}/projects/{project_id}/repository/tags",
        json={"tag_name": "v1.0.0", "ref": "main"},
    )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_project_requires_owner(client, db_session, test_user, test_token):
    maintainer, maintainer_token = await _create_user_and_token(
        db_session, "project-delete-maintainer"
    )
    project = await client.post(
        f"{API}/projects",
        json={"name": "delete-project-owner-gate"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]
    member = await client.post(
        f"{API}/projects/{project_id}/members",
        json={"user_id": maintainer.id, "access_level": 40},
        headers=auth_headers(test_token),
    )
    assert member.status_code == 201

    denied = await client.delete(
        f"{API}/projects/{project_id}",
        headers=auth_headers(maintainer_token),
    )
    assert denied.status_code == 403

    allowed = await client.delete(
        f"{API}/projects/{project_id}",
        headers=auth_headers(test_token),
    )
    assert allowed.status_code == 202
