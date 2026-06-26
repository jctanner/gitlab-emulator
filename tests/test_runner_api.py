"""GitLab Runner coordinator API tests."""

from sqlalchemy import select

from app.models.ci import CiRunner
from tests.conftest import API, auth_headers


RUNNER_TOKEN = "glrt-emulator-runner-token"
REGISTRATION_TOKEN = "runner-registration-token"


async def test_runner_registration_exchanges_registration_token(client, db_session):
    resp = await client.post(
        f"{API}/runners",
        headers={"RUNNER-TOKEN": REGISTRATION_TOKEN},
        json={
            "token": REGISTRATION_TOKEN,
            "description": "test-runner",
            "tag_list": "docker,linux",
            "run_untagged": False,
            "info": {"name": "persisted-runner", "version": "19.0.1", "executor": "docker"},
        },
    )

    assert resp.status_code == 201
    assert resp.json()["id"] == 1
    assert resp.json()["token"] == RUNNER_TOKEN

    result = await db_session.execute(select(CiRunner).where(CiRunner.token == RUNNER_TOKEN))
    runner = result.scalar_one()
    assert runner.description == "test-runner"
    assert runner.tags == ["docker", "linux"]
    assert runner.run_untagged is False
    assert runner.runner_name == "persisted-runner"
    assert runner.runner_version == "19.0.1"
    assert runner.runner_executor == "docker"
    assert runner.last_contact_at is not None


async def test_runner_registration_creates_distinct_runner_tokens(client):
    first = await client.post(
        f"{API}/runners",
        headers={"RUNNER-TOKEN": REGISTRATION_TOKEN},
        json={
            "token": REGISTRATION_TOKEN,
            "description": "docker-runner",
            "tag_list": "docker",
            "info": {"name": "docker-runner", "executor": "docker"},
        },
    )
    assert first.status_code == 201
    assert first.json()["token"] == RUNNER_TOKEN

    second = await client.post(
        f"{API}/runners",
        headers={"RUNNER-TOKEN": REGISTRATION_TOKEN},
        json={
            "token": REGISTRATION_TOKEN,
            "description": "k8s-runner",
            "tag_list": "k8s",
            "run_untagged": False,
            "info": {"name": "k8s-runner", "executor": "kubernetes"},
        },
    )
    assert second.status_code == 201
    assert second.json()["token"].startswith("glrt-")
    assert second.json()["token"] != RUNNER_TOKEN

    runners = await client.get(f"{API}/runners")
    assert runners.status_code == 200
    assert [runner["description"] for runner in runners.json()] == [
        "docker-runner",
        "k8s-runner",
    ]


async def test_runner_request_no_job_returns_204(client, db_session):
    resp = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={
            "token": RUNNER_TOKEN,
            "system_id": "runner-system-1",
            "info": {"name": "polling-runner", "version": "19.0.1", "executor": "docker"},
        },
    )

    assert resp.status_code == 204
    assert "X-GitLab-Last-Update" in resp.headers

    result = await db_session.execute(select(CiRunner).where(CiRunner.token == RUNNER_TOKEN))
    runner = result.scalar_one()
    assert runner.last_contact_at is not None
    assert runner.last_poll_at is not None
    assert runner.system_id == "runner-system-1"
    assert runner.runner_name == "polling-runner"


async def test_runner_verify_updates_persisted_runner(client, db_session):
    register = await client.post(
        f"{API}/runners",
        headers={"RUNNER-TOKEN": REGISTRATION_TOKEN},
        json={"token": REGISTRATION_TOKEN, "description": "verify-runner"},
    )
    assert register.status_code == 201

    verify = await client.post(
        f"{API}/runners/verify",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN, "system_id": "verify-system"},
    )
    assert verify.status_code == 200

    result = await db_session.execute(select(CiRunner).where(CiRunner.token == RUNNER_TOKEN))
    runner = result.scalar_one()
    assert runner.last_verify_at is not None
    assert runner.system_id == "verify-system"


async def test_runner_inspection_endpoints(client, test_token):
    register = await client.post(
        f"{API}/runners",
        headers={"RUNNER-TOKEN": REGISTRATION_TOKEN},
        json={
            "token": REGISTRATION_TOKEN,
            "description": "inspect-runner",
            "tag_list": "docker,vm",
            "info": {"name": "inspect-runner", "version": "19.0.1", "executor": "docker"},
        },
    )
    assert register.status_code == 201
    runner_id = register.json()["id"]

    runners = await client.get(f"{API}/runners")
    assert runners.status_code == 200
    assert runners.json()[0]["description"] == "inspect-runner"
    assert runners.json()[0]["tag_list"] == ["docker", "vm"]
    assert "token" not in runners.json()[0]

    runner = await client.get(f"{API}/runners/{runner_id}")
    assert runner.status_code == 200
    assert runner.json()["token"] == RUNNER_TOKEN
    assert runner.json()["version"] == "19.0.1"
    assert runner.json()["executor"] == "docker"

    project = await client.post(
        f"{API}/user/repos",
        json={"name": "runner-inspection", "auto_init": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    pipeline = await client.post(
        f"{API}/projects/{project.json()['id']}/pipeline",
        json={
            "ref": "main",
            "job": {"name": "inspect_job", "script": ["echo inspect"]},
        },
    )
    assert pipeline.status_code == 201
    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN, "info": {"name": "inspect-runner"}},
    )
    assert request.status_code == 201

    jobs = await client.get(f"{API}/runners/{runner_id}/jobs")
    assert jobs.status_code == 200
    assert jobs.json()[0]["name"] == "inspect_job"
    assert jobs.json()[0]["runner"]["description"] == "inspect-runner"


async def test_debug_smoke_queue_admin_routes_are_removed(client):
    enqueue = await client.post(
        f"{API}/admin/runner/jobs",
        json={"script": ["echo removed"]},
    )
    assert enqueue.status_code == 404

    inspect = await client.get(f"{API}/admin/runner/jobs/1")
    assert inspect.status_code == 404

    poll = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert poll.status_code == 204


async def test_project_cache_upload_head_and_download(client):
    cache = b"fake cache zip"

    upload = await client.put(
        f"{API}/projects/1/cache/pip-cache",
        content=cache,
        headers={"Content-Type": "application/zip"},
    )
    assert upload.status_code == 201

    head = await client.head(f"{API}/projects/1/cache/pip-cache")
    assert head.status_code == 200
    assert head.headers["content-length"] == str(len(cache))
    assert head.headers["content-type"] == "application/zip"

    download = await client.get(f"{API}/projects/1/cache/pip-cache")
    assert download.status_code == 200
    assert download.content == cache
    assert download.headers["x-gitlab-cache-key"] == "pip-cache"


async def test_project_cache_download_uses_fallback_keys(client):
    cache = b"fallback cache zip"

    upload = await client.put(
        f"{API}/projects/1/cache/fallback-cache",
        content=cache,
        headers={"Content-Type": "application/zip"},
    )
    assert upload.status_code == 201
    assert upload.json()["key"] == "fallback-cache"

    head = await client.head(
        f"{API}/projects/1/cache/missing-primary?fallback_keys=missing-secondary,fallback-cache"
    )
    assert head.status_code == 200
    assert head.headers["x-gitlab-cache-key"] == "fallback-cache"

    download = await client.get(
        f"{API}/projects/1/cache/missing-primary?fallback_keys=missing-secondary,fallback-cache"
    )
    assert download.status_code == 200
    assert download.headers["x-gitlab-cache-key"] == "fallback-cache"
    assert download.content == cache


async def test_project_cache_missing_returns_404(client):
    resp = await client.get(f"{API}/projects/1/cache/missing")
    assert resp.status_code == 404
