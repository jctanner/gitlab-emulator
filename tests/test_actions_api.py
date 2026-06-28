"""Tests for Actions-compatible repository configuration APIs."""

import pytest

from tests.conftest import API, auth_headers
from tests.test_projects_api import _create_user_and_token


@pytest.mark.asyncio
async def test_actions_secret_and_variable_management_requires_maintainer(
    client, db_session, test_token
):
    developer, developer_token = await _create_user_and_token(
        db_session, "actions-developer"
    )
    maintainer, maintainer_token = await _create_user_and_token(
        db_session, "actions-maintainer"
    )
    repo = await client.post(
        f"{API}/user/repos",
        json={"name": "actions-config-role"},
        headers=auth_headers(test_token),
    )
    assert repo.status_code == 201
    project = repo.json()
    for user, level in ((developer, 30), (maintainer, 40)):
        member = await client.post(
            f"{API}/projects/{project['id']}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201

    denied_secret = await client.put(
        f"{API}/repos/testuser/actions-config-role/actions/secrets/TOKEN",
        json={"encrypted_value": "ignored"},
        headers=auth_headers(developer_token),
    )
    assert denied_secret.status_code == 403

    secret = await client.put(
        f"{API}/repos/testuser/actions-config-role/actions/secrets/TOKEN",
        json={"encrypted_value": "ignored"},
        headers=auth_headers(maintainer_token),
    )
    assert secret.status_code == 201

    denied_secret_list = await client.get(
        f"{API}/repos/testuser/actions-config-role/actions/secrets",
        headers=auth_headers(developer_token),
    )
    assert denied_secret_list.status_code == 403

    secret_list = await client.get(
        f"{API}/repos/testuser/actions-config-role/actions/secrets",
        headers=auth_headers(maintainer_token),
    )
    assert secret_list.status_code == 200
    assert secret_list.json()["total_count"] == 1

    denied_variable = await client.post(
        f"{API}/repos/testuser/actions-config-role/actions/variables",
        json={"name": "ENV_NAME", "value": "dev"},
        headers=auth_headers(developer_token),
    )
    assert denied_variable.status_code == 403

    variable = await client.post(
        f"{API}/repos/testuser/actions-config-role/actions/variables",
        json={"name": "ENV_NAME", "value": "prod"},
        headers=auth_headers(maintainer_token),
    )
    assert variable.status_code == 201

    denied_update = await client.patch(
        f"{API}/repos/testuser/actions-config-role/actions/variables/ENV_NAME",
        json={"value": "denied"},
        headers=auth_headers(developer_token),
    )
    assert denied_update.status_code == 403

    update = await client.patch(
        f"{API}/repos/testuser/actions-config-role/actions/variables/ENV_NAME",
        json={"value": "updated"},
        headers=auth_headers(maintainer_token),
    )
    assert update.status_code == 200
    assert update.json()["value"] == "updated"

    denied_delete = await client.delete(
        f"{API}/repos/testuser/actions-config-role/actions/variables/ENV_NAME",
        headers=auth_headers(developer_token),
    )
    assert denied_delete.status_code == 403

    delete_variable = await client.delete(
        f"{API}/repos/testuser/actions-config-role/actions/variables/ENV_NAME",
        headers=auth_headers(maintainer_token),
    )
    assert delete_variable.status_code == 204

    delete_secret = await client.delete(
        f"{API}/repos/testuser/actions-config-role/actions/secrets/TOKEN",
        headers=auth_headers(maintainer_token),
    )
    assert delete_secret.status_code == 204
