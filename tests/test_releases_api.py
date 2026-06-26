"""Tests for GitLab-shaped release API endpoints."""

import pytest

from tests.conftest import API, auth_headers


@pytest.mark.asyncio
async def test_gitlab_project_release_crud(client, test_user, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "release-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    created = await client.post(
        f"{API}/projects/{project_id}/releases",
        json={
            "name": "Version 1.0",
            "tag_name": "v1.0.0",
            "ref": "main",
            "description": "Initial release",
        },
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201
    data = created.json()
    assert data["tag_name"] == "v1.0.0"
    assert data["name"] == "Version 1.0"
    assert data["description"] == "Initial release"
    assert data["assets"]["sources"][0]["format"] == "zip"

    tag = await client.get(
        f"{API}/projects/{project_id}/repository/tags/v1.0.0",
        headers=auth_headers(test_token),
    )
    assert tag.status_code == 200

    listed = await client.get(
        f"{API}/projects/{project_id}/releases",
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert [item["tag_name"] for item in listed.json()] == ["v1.0.0"]

    fetched = await client.get(
        f"{API}/projects/{project_id}/releases/v1.0.0",
        headers=auth_headers(test_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["tag_name"] == "v1.0.0"

    updated = await client.put(
        f"{API}/projects/{project_id}/releases/v1.0.0",
        json={"name": "Version 1.0.1", "description": "Updated release notes"},
        headers=auth_headers(test_token),
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Version 1.0.1"
    assert updated.json()["description"] == "Updated release notes"

    deleted = await client.delete(
        f"{API}/projects/{project_id}/releases/v1.0.0",
        headers=auth_headers(test_token),
    )
    assert deleted.status_code == 200
    assert deleted.json()["tag_name"] == "v1.0.0"

    missing = await client.get(
        f"{API}/projects/{project_id}/releases/v1.0.0",
        headers=auth_headers(test_token),
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_gitlab_project_release_accepts_url_encoded_project_path(
    client, test_user, test_token
):
    project = await client.post(
        f"{API}/projects",
        json={"name": "release-path-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201

    created = await client.post(
        f"{API}/projects/testuser%2Frelease-path-project/releases",
        json={"tag_name": "v2.0.0", "ref": "main", "description": "Path release"},
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201
    assert created.json()["tag_name"] == "v2.0.0"

    fetched = await client.get(
        f"{API}/projects/testuser%2Frelease-path-project/releases/v2.0.0",
        headers=auth_headers(test_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["description"] == "Path release"
