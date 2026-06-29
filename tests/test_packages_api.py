"""Tests for GitLab generic package registry endpoints."""

import pytest

from tests.conftest import API, auth_headers
from tests.test_projects_api import _create_user_and_token


@pytest.mark.asyncio
async def test_generic_package_upload_download_and_head(client, test_user, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "package-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]
    path = (
        f"{API}/projects/{project_id}/packages/generic/"
        "release-assets/v1.0.0/dist/tool.txt"
    )

    uploaded = await client.put(
        path,
        content=b"package payload\n",
        headers=auth_headers(test_token),
    )
    assert uploaded.status_code == 201
    assert uploaded.json()["package_name"] == "release-assets"
    assert uploaded.json()["package_version"] == "v1.0.0"
    assert uploaded.json()["file_name"] == "dist/tool.txt"
    assert uploaded.json()["size"] == len(b"package payload\n")

    downloaded = await client.get(path, headers=auth_headers(test_token))
    assert downloaded.status_code == 200
    assert downloaded.content == b"package payload\n"

    metadata = await client.head(path, headers=auth_headers(test_token))
    assert metadata.status_code == 200
    assert metadata.headers["content-length"] == str(len(b"package payload\n"))


@pytest.mark.asyncio
async def test_generic_package_upload_requires_developer(
    client, db_session, test_user, test_token
):
    reporter, reporter_token = await _create_user_and_token(
        db_session, "package-reporter"
    )
    project = await client.post(
        f"{API}/projects",
        json={"name": "package-role-gate", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]
    member = await client.post(
        f"{API}/projects/{project_id}/members",
        json={"user_id": reporter.id, "access_level": 20},
        headers=auth_headers(test_token),
    )
    assert member.status_code == 201

    denied = await client.put(
        f"{API}/projects/{project_id}/packages/generic/release-assets/v1/file.txt",
        content=b"nope",
        headers=auth_headers(reporter_token),
    )
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_generic_package_rejects_unsafe_paths(client, test_user, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "package-path-gate", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    denied = await client.put(
        f"{API}/projects/{project_id}/packages/generic/release-assets/v1/%2E%2E/bad.txt",
        content=b"bad",
        headers=auth_headers(test_token),
    )
    assert denied.status_code == 400
