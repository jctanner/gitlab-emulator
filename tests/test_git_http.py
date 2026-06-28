"""Tests for the Git Smart HTTP protocol endpoints.

These tests verify that the info/refs, upload-pack, and receive-pack
endpoints respond correctly. Full git clone/push integration requires
a running server; these tests validate the HTTP-level behavior.
"""

import base64

import pytest

from tests.conftest import auth_headers
from tests.test_projects_api import _create_user_and_token

API = "/api/v4"
RUNNER_TOKEN = "glrt-emulator-runner-token"


@pytest.fixture
async def git_repo(client, test_user, test_token, tmp_path):
    """Create a repo with auto_init so it has a bare git directory."""
    resp = await client.post(
        f"{API}/user/repos",
        json={"name": "git-test", "auto_init": True},
        headers=auth_headers(test_token),
    )
    return resp.json()


@pytest.mark.asyncio
async def test_info_refs_upload_pack(client, test_user, test_token, git_repo):
    """GET /{owner}/{repo}.git/info/refs?service=git-upload-pack returns refs."""
    resp = await client.get(
        "/testuser/git-test.git/info/refs?service=git-upload-pack"
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-git-upload-pack-advertisement"
    # Response should contain pkt-line service announcement
    body = resp.content
    assert b"# service=git-upload-pack" in body


@pytest.mark.asyncio
async def test_info_refs_receive_pack_requires_auth(client, test_user, test_token, git_repo):
    """GET info/refs?service=git-receive-pack without auth returns 401."""
    resp = await client.get(
        "/testuser/git-test.git/info/refs?service=git-receive-pack"
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_info_refs_receive_pack_with_auth(client, test_user, test_token, git_repo):
    """GET info/refs?service=git-receive-pack with auth succeeds."""
    resp = await client.get(
        "/testuser/git-test.git/info/refs?service=git-receive-pack",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-git-receive-pack-advertisement"
    assert b"# service=git-receive-pack" in resp.content


@pytest.mark.asyncio
async def test_info_refs_invalid_service(client, test_user, test_token, git_repo):
    """GET info/refs with invalid service returns 403."""
    resp = await client.get(
        "/testuser/git-test.git/info/refs?service=invalid-service"
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_info_refs_without_git_suffix(client, test_user, test_token, git_repo):
    """GET /{owner}/{repo}/info/refs works without .git suffix."""
    resp = await client.get(
        "/testuser/git-test/info/refs?service=git-upload-pack"
    )
    assert resp.status_code == 200
    assert b"# service=git-upload-pack" in resp.content


@pytest.mark.asyncio
async def test_info_refs_nonexistent_repo(client):
    """GET info/refs for non-existent repo returns 404."""
    resp = await client.get(
        "/nobody/nothing.git/info/refs?service=git-upload-pack"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_upload_pack_endpoint(client, test_user, test_token, git_repo):
    """POST /{owner}/{repo}.git/git-upload-pack responds."""
    # Send a minimal (empty) request body — the git process will likely
    # fail or return an error, but we verify the endpoint responds with
    # the correct content type
    resp = await client.post(
        "/testuser/git-test.git/git-upload-pack",
        content=b"0000",
    )
    # The endpoint should respond (even if git process errors)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-git-upload-pack-result"


@pytest.mark.asyncio
async def test_upload_pack_allows_pipeline_job_token_for_private_repo(client, test_user, test_token):
    """A CI job token can fetch its own private project but cannot push."""
    repo_resp = await client.post(
        f"{API}/user/repos",
        json={"name": "private-ci", "auto_init": True, "private": True},
        headers=auth_headers(test_token),
    )
    assert repo_resp.status_code == 201
    project_id = repo_resp.json()["id"]

    anonymous = await client.get(
        "/testuser/private-ci.git/info/refs?service=git-upload-pack"
    )
    assert anonymous.status_code == 401
    assert "Basic" in anonymous.headers["www-authenticate"]

    pipeline_resp = await client.post(
        f"{API}/projects/{project_id}/pipeline",
        json={
            "ref": "main",
            "job": {
                "name": "checkout",
                "image": "alpine:3.20",
                "script": ["test -f README.md"],
            },
        },
    )
    assert pipeline_resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    job_token = request.json()["token"]
    basic = base64.b64encode(f"gitlab-ci-token:{job_token}".encode()).decode()

    fetch_refs = await client.get(
        "/testuser/private-ci.git/info/refs?service=git-upload-pack",
        headers={"Authorization": f"Basic {basic}"},
    )
    assert fetch_refs.status_code == 200
    assert b"# service=git-upload-pack" in fetch_refs.content

    push_refs = await client.get(
        "/testuser/private-ci.git/info/refs?service=git-receive-pack",
        headers={"Authorization": f"Basic {basic}"},
    )
    assert push_refs.status_code == 401


@pytest.mark.asyncio
async def test_private_git_http_uses_project_member_access_levels(
    client, db_session, test_user, test_token
):
    reporter, reporter_token = await _create_user_and_token(
        db_session, "git-http-reporter"
    )
    developer, developer_token = await _create_user_and_token(
        db_session, "git-http-developer"
    )
    guest, guest_token = await _create_user_and_token(db_session, "git-http-guest")
    repo_resp = await client.post(
        f"{API}/user/repos",
        json={"name": "private-members", "auto_init": True, "private": True},
        headers=auth_headers(test_token),
    )
    assert repo_resp.status_code == 201
    project_id = repo_resp.json()["id"]

    for user, level in ((reporter, 20), (developer, 30), (guest, 10)):
        member = await client.post(
            f"{API}/projects/{project_id}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201

    reporter_fetch = await client.get(
        "/testuser/private-members.git/info/refs?service=git-upload-pack",
        headers=auth_headers(reporter_token),
    )
    assert reporter_fetch.status_code == 200

    guest_fetch = await client.get(
        "/testuser/private-members.git/info/refs?service=git-upload-pack",
        headers=auth_headers(guest_token),
    )
    assert guest_fetch.status_code == 404

    reporter_push = await client.get(
        "/testuser/private-members.git/info/refs?service=git-receive-pack",
        headers=auth_headers(reporter_token),
    )
    assert reporter_push.status_code == 403

    developer_push = await client.get(
        "/testuser/private-members.git/info/refs?service=git-receive-pack",
        headers=auth_headers(developer_token),
    )
    assert developer_push.status_code == 200
    assert b"# service=git-receive-pack" in developer_push.content


@pytest.mark.asyncio
async def test_receive_pack_requires_auth(client, test_user, test_token, git_repo):
    """POST git-receive-pack without auth returns 401."""
    resp = await client.post(
        "/testuser/git-test.git/git-receive-pack",
        content=b"0000",
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_receive_pack_with_auth(client, test_user, test_token, git_repo):
    """POST git-receive-pack with auth responds."""
    resp = await client.post(
        "/testuser/git-test.git/git-receive-pack",
        content=b"0000",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-git-receive-pack-result"


@pytest.mark.asyncio
async def test_cache_headers(client, test_user, test_token, git_repo):
    """Git HTTP responses include proper cache-control headers."""
    resp = await client.get(
        "/testuser/git-test.git/info/refs?service=git-upload-pack"
    )
    assert resp.headers.get("cache-control") == "no-cache"
    assert resp.headers.get("pragma") == "no-cache"
