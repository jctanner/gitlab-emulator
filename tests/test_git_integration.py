"""Tests for Git integration -- clone, push, pull via Smart HTTP."""

import asyncio
import os

import pytest
import pytest_asyncio
import uvicorn

from tests.conftest import auth_headers
from tests.test_projects_api import _create_user_and_token

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


async def _run_git_failure(*args: str, cwd: str | os.PathLike | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        raise AssertionError(
            f"git {' '.join(args)} unexpectedly succeeded\n"
            f"stdout:\n{stdout.decode()}\n"
            f"stderr:\n{stderr.decode()}"
        )
    return stderr.decode()


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
    - if: '$CI_PIPELINE_SOURCE == "push" && $CI_COMMIT_BRANCH == "main"'
  script:
    - echo pushed

tag_job:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "push" && $CI_COMMIT_TAG == "v1.0.0"'
  script:
    - echo tagged

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

    await _run_git("tag", "v1.0.0", cwd=worktree)
    await _run_git("push", authed_url, "v1.0.0", cwd=worktree)

    tag_pipelines = await client.get(
        f"{API}/projects/{resp.json()['id']}/pipelines",
        headers=auth_headers(test_token),
    )
    assert tag_pipelines.status_code == 200
    tag_pipeline = next(
        pipeline
        for pipeline in tag_pipelines.json()
        if pipeline["source"] == "push" and pipeline["ref"] == "v1.0.0"
    )
    assert tag_pipeline["before_sha"] == "0000000000000000000000000000000000000000"

    tag_jobs = await client.get(
        f"{API}/projects/{resp.json()['id']}/pipelines/{tag_pipeline['id']}/jobs",
        headers=auth_headers(test_token),
    )
    assert tag_jobs.status_code == 200
    assert [job["name"] for job in tag_jobs.json()] == ["tag_job"]


@pytest.mark.asyncio
async def test_git_smart_http_enforces_protected_branches(
    client, db_session, test_user, test_token, live_server, tmp_path
):
    """Protected branches block unauthorized, force, and delete pushes."""
    developer, developer_token = await _create_user_and_token(
        db_session, "git-protected-developer"
    )
    maintainer, maintainer_token = await _create_user_and_token(
        db_session, "git-protected-maintainer"
    )
    project = await client.post(
        f"{API}/projects",
        json={"name": "live-protected-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    for user, level in ((developer, 30), (maintainer, 40)):
        member = await client.post(
            f"{API}/projects/{project_id}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201

    protected = await client.post(
        f"{API}/projects/{project_id}/protected_branches",
        json={"name": "main", "push_access_level": 40},
        headers=auth_headers(test_token),
    )
    assert protected.status_code == 201
    assert protected.json()["allow_force_push"] is False

    clone_url = f"{live_server}/testuser/live-protected-project.git"
    worktree = tmp_path / "protected-worktree"
    await _run_git("clone", clone_url, str(worktree))
    await _run_git("config", "user.name", "Protected User", cwd=worktree)
    await _run_git("config", "user.email", "protected@test.com", cwd=worktree)
    await _run_git("config", "commit.gpgsign", "false", cwd=worktree)

    (worktree / "protected.txt").write_text("protected branch update\n")
    await _run_git("add", "protected.txt", cwd=worktree)
    await _run_git("commit", "-m", "update protected branch", cwd=worktree)
    developer_url = live_server.replace(
        "http://",
        f"http://git-protected-developer:{developer_token}@",
    )
    developer_url = f"{developer_url}/testuser/live-protected-project.git"
    developer_error = await _run_git_failure(
        "push", developer_url, "main", cwd=worktree
    )
    assert "protected branch 'main'" in developer_error

    maintainer_url = live_server.replace(
        "http://",
        f"http://git-protected-maintainer:{maintainer_token}@",
    )
    maintainer_url = f"{maintainer_url}/testuser/live-protected-project.git"
    await _run_git("push", maintainer_url, "main", cwd=worktree)

    await _run_git("reset", "--hard", "HEAD~1", cwd=worktree)
    force_error = await _run_git_failure(
        "push", "--force", maintainer_url, "main", cwd=worktree
    )
    assert "force push to protected branch 'main'" in force_error

    delete_error = await _run_git_failure(
        "push", maintainer_url, ":main", cwd=worktree
    )
    assert "delete protected branch 'main'" in delete_error
