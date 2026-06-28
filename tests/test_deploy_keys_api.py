"""Tests for repository deploy key authorization."""

import pytest

from tests.conftest import API, auth_headers
from tests.test_projects_api import _create_user_and_token


DEPLOY_KEY = (
    "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7testdeploykey"
    " gitlab-emulator@test"
)


@pytest.mark.asyncio
async def test_deploy_key_writes_require_maintainer(
    client, db_session, test_user, test_token
):
    maintainer, maintainer_token = await _create_user_and_token(
        db_session, "deploy-key-maintainer"
    )
    developer, developer_token = await _create_user_and_token(
        db_session, "deploy-key-developer"
    )
    project = await client.post(
        f"{API}/user/repos",
        json={"name": "deploy-key-role-gate"},
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
        f"{API}/repos/testuser/deploy-key-role-gate/keys",
        json={"title": "developer", "key": DEPLOY_KEY},
        headers=auth_headers(developer_token),
    )
    assert denied.status_code == 403

    allowed = await client.post(
        f"{API}/repos/testuser/deploy-key-role-gate/keys",
        json={"title": "maintainer", "key": DEPLOY_KEY},
        headers=auth_headers(maintainer_token),
    )
    assert allowed.status_code == 201
    key_id = allowed.json()["id"]

    delete_denied = await client.delete(
        f"{API}/repos/testuser/deploy-key-role-gate/keys/{key_id}",
        headers=auth_headers(developer_token),
    )
    assert delete_denied.status_code == 403

    deleted = await client.delete(
        f"{API}/repos/testuser/deploy-key-role-gate/keys/{key_id}",
        headers=auth_headers(maintainer_token),
    )
    assert deleted.status_code == 204
