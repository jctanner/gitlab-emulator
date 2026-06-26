"""Tests for the Webhook REST API endpoints."""

import pytest

from tests.conftest import auth_headers

API = "/api/v4"


@pytest.mark.asyncio
async def test_create_webhook(client, test_user, test_token):
    """POST /repos/{owner}/{repo}/hooks creates a webhook."""
    await client.post(
        f"{API}/user/repos", json={"name": "hook-repo"}, headers=auth_headers(test_token)
    )
    resp = await client.post(
        f"{API}/repos/testuser/hook-repo/hooks",
        json={
            "config": {"url": "https://example.com/webhook", "content_type": "json"},
            "events": ["push", "pull_request"],
            "active": True,
        },
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["active"] is True
    assert data["config"]["url"] == "https://example.com/webhook"
    assert "push" in data["events"]


@pytest.mark.asyncio
async def test_list_webhooks(client, test_user, test_token):
    """GET /repos/{owner}/{repo}/hooks lists webhooks."""
    await client.post(
        f"{API}/user/repos", json={"name": "hook-list"}, headers=auth_headers(test_token)
    )
    await client.post(
        f"{API}/repos/testuser/hook-list/hooks",
        json={"config": {"url": "https://example.com/hook1"}},
        headers=auth_headers(test_token),
    )
    resp = await client.get(
        f"{API}/repos/testuser/hook-list/hooks", headers=auth_headers(test_token)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_get_webhook(client, test_user, test_token):
    """GET /repos/{owner}/{repo}/hooks/{id} returns a webhook."""
    await client.post(
        f"{API}/user/repos", json={"name": "hook-get"}, headers=auth_headers(test_token)
    )
    create = await client.post(
        f"{API}/repos/testuser/hook-get/hooks",
        json={"config": {"url": "https://example.com/hook"}},
        headers=auth_headers(test_token),
    )
    hook_id = create.json()["id"]
    resp = await client.get(
        f"{API}/repos/testuser/hook-get/hooks/{hook_id}",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == hook_id


@pytest.mark.asyncio
async def test_update_webhook(client, test_user, test_token):
    """PATCH /repos/{owner}/{repo}/hooks/{id} updates a webhook."""
    await client.post(
        f"{API}/user/repos", json={"name": "hook-upd"}, headers=auth_headers(test_token)
    )
    create = await client.post(
        f"{API}/repos/testuser/hook-upd/hooks",
        json={"config": {"url": "https://example.com/hook"}, "active": True},
        headers=auth_headers(test_token),
    )
    hook_id = create.json()["id"]
    resp = await client.patch(
        f"{API}/repos/testuser/hook-upd/hooks/{hook_id}",
        json={"active": False, "events": ["issues"]},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["active"] is False
    assert "issues" in data["events"]


@pytest.mark.asyncio
async def test_delete_webhook(client, test_user, test_token):
    """DELETE /repos/{owner}/{repo}/hooks/{id} removes a webhook."""
    await client.post(
        f"{API}/user/repos", json={"name": "hook-del"}, headers=auth_headers(test_token)
    )
    create = await client.post(
        f"{API}/repos/testuser/hook-del/hooks",
        json={"config": {"url": "https://example.com/hook"}},
        headers=auth_headers(test_token),
    )
    hook_id = create.json()["id"]
    resp = await client.delete(
        f"{API}/repos/testuser/hook-del/hooks/{hook_id}",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_webhook_config_shape(client, test_user, test_token):
    """Webhook config has url, content_type, insecure_ssl."""
    await client.post(
        f"{API}/user/repos", json={"name": "hook-cfg"}, headers=auth_headers(test_token)
    )
    resp = await client.post(
        f"{API}/repos/testuser/hook-cfg/hooks",
        json={"config": {"url": "https://example.com/hook", "content_type": "form"}},
        headers=auth_headers(test_token),
    )
    data = resp.json()
    assert "config" in data
    config = data["config"]
    assert "url" in config
    assert "content_type" in config
    assert "insecure_ssl" in config


@pytest.mark.asyncio
async def test_webhook_requires_url(client, test_user, test_token):
    """Webhook creation requires config.url."""
    await client.post(
        f"{API}/user/repos", json={"name": "hook-nourl"}, headers=auth_headers(test_token)
    )
    resp = await client.post(
        f"{API}/repos/testuser/hook-nourl/hooks",
        json={"config": {}},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_webhook_not_found(client, test_user, test_token):
    """GET webhook with invalid ID returns 404."""
    await client.post(
        f"{API}/user/repos", json={"name": "hook-nf"}, headers=auth_headers(test_token)
    )
    resp = await client.get(
        f"{API}/repos/testuser/hook-nf/hooks/99999",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_gitlab_project_hook_crud(client, test_user, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "gitlab-hook-project"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    created = await client.post(
        f"{API}/projects/{project_id}/hooks",
        json={
            "url": "https://example.com/gitlab-hook",
            "token": "secret",
            "push_events": True,
            "merge_requests_events": True,
            "enable_ssl_verification": False,
        },
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201
    hook = created.json()
    assert hook["project_id"] == project_id
    assert hook["url"] == "https://example.com/gitlab-hook"
    assert hook["push_events"] is True
    assert hook["merge_requests_events"] is True
    assert hook["enable_ssl_verification"] is False

    listed = await client.get(
        f"{API}/projects/{project_id}/hooks",
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [hook["id"]]

    fetched = await client.get(
        f"{API}/projects/{project_id}/hooks/{hook['id']}",
        headers=auth_headers(test_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["id"] == hook["id"]

    updated = await client.put(
        f"{API}/projects/{project_id}/hooks/{hook['id']}",
        json={"url": "https://example.com/updated", "push_events": False},
        headers=auth_headers(test_token),
    )
    assert updated.status_code == 200
    assert updated.json()["url"] == "https://example.com/updated"
    assert updated.json()["push_events"] is False

    deleted = await client.delete(
        f"{API}/projects/{project_id}/hooks/{hook['id']}",
        headers=auth_headers(test_token),
    )
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_gitlab_project_hook_accepts_encoded_project_path(
    client, test_user, test_token
):
    project = await client.post(
        f"{API}/projects",
        json={"name": "gitlab-hook-path"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201

    created = await client.post(
        f"{API}/projects/testuser%2Fgitlab-hook-path/hooks",
        json={"url": "https://example.com/path-hook", "job_events": True},
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201
    assert created.json()["job_events"] is True


@pytest.mark.asyncio
async def test_gitlab_group_hook_crud(client, test_user, test_token):
    group = await client.post(
        f"{API}/groups",
        json={"path": "hook-group", "name": "Hook Group"},
        headers=auth_headers(test_token),
    )
    assert group.status_code == 201
    group_id = group.json()["id"]

    created = await client.post(
        f"{API}/groups/{group_id}/hooks",
        json={"url": "https://example.com/group-hook", "pipeline_events": True},
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201
    hook = created.json()
    assert hook["group_id"] == group_id
    assert hook["pipeline_events"] is True

    fetched = await client.get(
        f"{API}/groups/{group_id}/hooks/{hook['id']}",
        headers=auth_headers(test_token),
    )
    assert fetched.status_code == 200
    assert fetched.json()["id"] == hook["id"]

    listed = await client.get(
        f"{API}/groups/hook-group/hooks",
        headers=auth_headers(test_token),
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [hook["id"]]

    deleted = await client.delete(
        f"{API}/groups/{group_id}/hooks/{hook['id']}",
        headers=auth_headers(test_token),
    )
    assert deleted.status_code == 204
