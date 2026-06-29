"""Tests for Git integration -- clone, push, pull via Smart HTTP."""

import asyncio
import os

import pytest
import pytest_asyncio
import uvicorn

from tests.conftest import auth_headers

API = "/api/v4"


@pytest_asyncio.fixture
async def live_server(app, unused_tcp_port):
    """Run the test app on a real TCP port for subprocess git clients."""
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=unused_tcp_port,
        lifespan="off",
        log_level="warning",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{unused_tcp_port}"
    server.should_exit = True
    await task


async def _run_git(*args: str, cwd: str | os.PathLike | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed with {proc.returncode}\n"
            f"stdout:\n{stdout.decode()}\n"
            f"stderr:\n{stderr.decode()}"
        )
    return stdout.decode()


@pytest.mark.asyncio
async def test_clone_repo_with_init(client, test_user, test_token, test_repo_with_init, tmp_path):
    """Git clone of an initialized repo works via HTTP transport."""
    owner, repo_name, _ = test_repo_with_init

    # Get the info/refs endpoint to verify it works
    resp = await client.get(
        f"/{owner}/{repo_name}.git/info/refs?service=git-upload-pack",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200
    assert "application/x-git-upload-pack-advertisement" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_info_refs_receive_pack(client, test_user, test_token, test_repo_with_init):
    """info/refs for git-receive-pack requires auth."""
    owner, repo_name, _ = test_repo_with_init
    resp = await client.get(
        f"/{owner}/{repo_name}.git/info/refs?service=git-receive-pack",
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_info_refs_no_auth(client, test_user, test_token, test_repo_with_init):
    """info/refs without auth for public repo works for upload-pack."""
    owner, repo_name, _ = test_repo_with_init
    resp = await client.get(
        f"/{owner}/{repo_name}.git/info/refs?service=git-upload-pack",
    )
    # Public repo should allow unauthenticated read
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_info_refs_invalid_service(client, test_user, test_token, test_repo_with_init):
    """info/refs with invalid service returns 403."""
    owner, repo_name, _ = test_repo_with_init
    resp = await client.get(
        f"/{owner}/{repo_name}.git/info/refs?service=invalid-service",
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_upload_pack_endpoint(client, test_user, test_token, test_repo_with_init):
    """POST git-upload-pack endpoint responds."""
    owner, repo_name, _ = test_repo_with_init
    resp = await client.post(
        f"/{owner}/{repo_name}.git/git-upload-pack",
        content=b"0000",
        headers={
            **auth_headers(test_token),
            "content-type": "application/x-git-upload-pack-request",
        },
    )
    # Should respond (even if the pack negotiation fails with dummy data)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_nonexistent_repo_returns_404(client, test_user, test_token):
    """info/refs for nonexistent repo returns 404."""
    resp = await client.get(
        "/nobody/noexist.git/info/refs?service=git-upload-pack",
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_gitlab_project_api_project_supports_live_clone_push_fetch(
    client, test_user, test_token, live_server, tmp_path
):
    """A project created through GitLab APIs works with real git over HTTP."""
    resp = await client.post(
        f"{API}/projects",
        json={"name": "live-git-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201

    clone_url = f"{live_server}/testuser/live-git-project.git"
    worktree = tmp_path / "worktree"
    await _run_git("clone", clone_url, str(worktree))
    assert (worktree / "README.md").read_text() == "# live-git-project\n"

    await _run_git("config", "user.name", "Test User", cwd=worktree)
    await _run_git("config", "user.email", "test@test.com", cwd=worktree)
    await _run_git("config", "commit.gpgsign", "false", cwd=worktree)
    before_sha = await _run_git("rev-parse", "HEAD", cwd=worktree)
    (worktree / "feature.txt").write_text("created through live git\n")
    (worktree / ".gitlab-ci.yml").write_text(
        """
push_job:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "push"'
  script:
    - echo pushed

api_job:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "api"'
  script:
    - echo api
"""
    )
    await _run_git("add", "feature.txt", ".gitlab-ci.yml", cwd=worktree)
    await _run_git("commit", "-m", "add feature and ci", cwd=worktree)

    authed_url = f"{live_server.replace('http://', f'http://testuser:{test_token}@')}/testuser/live-git-project.git"
    await _run_git("push", authed_url, "main", cwd=worktree)

    fetched = tmp_path / "fetched"
    await _run_git("clone", clone_url, str(fetched))
    assert (fetched / "feature.txt").read_text() == "created through live git\n"

    pipelines = await client.get(
        f"{API}/projects/{resp.json()['id']}/pipelines",
        headers=auth_headers(test_token),
    )
    assert pipelines.status_code == 200
    push_pipeline = next(
        pipeline for pipeline in pipelines.json() if pipeline["source"] == "push"
    )
    assert push_pipeline["ref"] == "main"
    assert push_pipeline["before_sha"] == before_sha.strip()

    jobs = await client.get(
        f"{API}/projects/{resp.json()['id']}/pipelines/{push_pipeline['id']}/jobs",
        headers=auth_headers(test_token),
    )
    assert jobs.status_code == 200
    assert [job["name"] for job in jobs.json()] == ["push_job"]

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": "glrt-emulator-runner-token"},
        json={"token": "glrt-emulator-runner-token"},
    )
    assert request.status_code == 201
    assert request.json()["git_info"]["before_sha"] == before_sha.strip()
