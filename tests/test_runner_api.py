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
            "info": {
                "name": "persisted-runner",
                "version": "19.0.1",
                "executor": "docker",
                "features": {
                    "cache": True,
                    "fallback_cache_keys": True,
                    "raw_variables": True,
                },
                "config": {
                    "tag_list": ["docker", "linux"],
                    "run_untagged": False,
                },
            },
        },
    )

    assert resp.status_code == 201
    assert resp.json()["id"] == 1
    assert resp.json()["token"] == RUNNER_TOKEN

    result = await db_session.execute(
        select(CiRunner).where(CiRunner.token == RUNNER_TOKEN)
    )
    runner = result.scalar_one()
    assert runner.description == "test-runner"
    assert runner.tags == ["docker", "linux"]
    assert runner.run_untagged is False
    assert runner.runner_name == "persisted-runner"
    assert runner.runner_version == "19.0.1"
    assert runner.runner_executor == "docker"
    assert runner.runner_features == {
        "cache": True,
        "fallback_cache_keys": True,
        "raw_variables": True,
    }
    assert runner.runner_config == {
        "tag_list": ["docker", "linux"],
        "run_untagged": False,
    }
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


async def test_runner_list_pagination_headers(client):
    for description in ["docker-runner", "k8s-runner", "shell-runner"]:
        register = await client.post(
            f"{API}/runners",
            headers={"RUNNER-TOKEN": REGISTRATION_TOKEN},
            json={"token": REGISTRATION_TOKEN, "description": description},
        )
        assert register.status_code == 201

    runners = await client.get(f"{API}/runners", params={"page": 2, "per_page": 1})

    assert runners.status_code == 200
    assert [runner["description"] for runner in runners.json()] == ["k8s-runner"]
    assert runners.headers["X-Total"] == "3"
    assert runners.headers["X-Total-Pages"] == "3"
    assert runners.headers["X-Page"] == "2"
    assert runners.headers["X-Per-Page"] == "1"
    assert runners.headers["X-Prev-Page"] == "1"
    assert runners.headers["X-Next-Page"] == "3"
    assert 'rel="prev"' in runners.headers["Link"]
    assert 'rel="next"' in runners.headers["Link"]


async def test_runner_request_no_job_returns_204(client, db_session):
    resp = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={
            "token": RUNNER_TOKEN,
            "system_id": "runner-system-1",
            "info": {
                "name": "polling-runner",
                "version": "19.0.1",
                "executor": "docker",
                "features": {
                    "artifacts": True,
                    "cache": True,
                    "fallback_cache_keys": True,
                },
                "config": {"run_untagged": True},
            },
        },
    )

    assert resp.status_code == 204
    assert "X-GitLab-Last-Update" in resp.headers

    result = await db_session.execute(
        select(CiRunner).where(CiRunner.token == RUNNER_TOKEN)
    )
    runner = result.scalar_one()
    assert runner.last_contact_at is not None
    assert runner.last_poll_at is not None
    assert runner.system_id == "runner-system-1"
    assert runner.runner_name == "polling-runner"
    assert runner.runner_features["fallback_cache_keys"] is True
    assert runner.runner_config == {"run_untagged": True}


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

    result = await db_session.execute(
        select(CiRunner).where(CiRunner.token == RUNNER_TOKEN)
    )
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
            "info": {
                "name": "inspect-runner",
                "version": "19.0.1",
                "executor": "docker",
                "features": {"cache": True, "fallback_cache_keys": True},
                "config": {"tag_list": ["docker", "vm"]},
            },
        },
    )
    assert register.status_code == 201
    runner_id = register.json()["id"]

    runners = await client.get(f"{API}/runners")
    assert runners.status_code == 200
    assert runners.json()[0]["description"] == "inspect-runner"
    assert runners.json()[0]["tag_list"] == ["docker", "vm"]
    assert runners.json()[0]["features"] == {
        "cache": True,
        "fallback_cache_keys": True,
    }
    assert runners.json()[0]["config"] == {"tag_list": ["docker", "vm"]}
    assert "token" not in runners.json()[0]

    runner = await client.get(f"{API}/runners/{runner_id}")
    assert runner.status_code == 200
    assert runner.json()["token"] == RUNNER_TOKEN
    assert runner.json()["version"] == "19.0.1"
    assert runner.json()["executor"] == "docker"
    assert runner.json()["features"]["fallback_cache_keys"] is True
    assert runner.json()["config"] == {"tag_list": ["docker", "vm"]}

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
        headers=auth_headers(test_token),
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
    assert jobs.headers["X-Total"] == "1"
    assert jobs.headers["X-Total-Pages"] == "1"
    assert jobs.headers["X-Page"] == "1"
    assert jobs.headers["X-Per-Page"] == "30"
    job = jobs.json()[0]
    assert job["name"] == "inspect_job"
    assert job["status"] == "running"
    assert job["ref"] == "main"
    assert job["environment"] is None
    assert job["duration"] is None
    assert isinstance(job["queued_duration"], int)
    assert job["queued_duration"] >= 0
    assert job["pipeline"]["id"] == pipeline.json()["id"]
    assert job["pipeline"]["project_id"] == project.json()["id"]
    assert job["commit"]["id"] == pipeline.json()["sha"]
    assert job["tag_list"] == []
    assert job["artifacts"] == []
    assert job["runner"]["description"] == "inspect-runner"
    assert job["runner"]["runner_type"] == "instance_type"
    assert job["web_url"].endswith(f"/testuser/runner-inspection/-/jobs/{job['id']}")

    failed = await client.put(
        f"{API}/jobs/{job['id']}",
        headers={"JOB-TOKEN": request.json()["token"]},
        json={
            "token": request.json()["token"],
            "state": "failed",
            "failure_reason": "runner_system_failure",
            "exit_code": 1,
        },
    )
    assert failed.status_code == 200

    failed_jobs = await client.get(f"{API}/runners/{runner_id}/jobs")
    assert failed_jobs.status_code == 200
    assert failed_jobs.json()[0]["status"] == "failed"
    assert failed_jobs.json()[0]["failure_reason"] == "runner_system_failure"


async def test_runner_jobs_pagination_headers(client, test_token):
    register = await client.post(
        f"{API}/runners",
        headers={"RUNNER-TOKEN": REGISTRATION_TOKEN},
        json={
            "token": REGISTRATION_TOKEN,
            "description": "jobs-page-runner",
            "info": {"name": "jobs-page-runner"},
        },
    )
    assert register.status_code == 201
    runner_id = register.json()["id"]

    project = await client.post(
        f"{API}/user/repos",
        json={"name": "runner-jobs-pagination", "auto_init": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201

    for index in range(3):
        pipeline = await client.post(
            f"{API}/projects/{project.json()['id']}/pipeline",
            json={
                "ref": "main",
                "job": {"name": f"paged_job_{index}", "script": ["echo paged"]},
            },
            headers=auth_headers(test_token),
        )
        assert pipeline.status_code == 201
        request = await client.post(
            f"{API}/jobs/request",
            headers={"RUNNER-TOKEN": RUNNER_TOKEN},
            json={"token": RUNNER_TOKEN, "info": {"name": "jobs-page-runner"}},
        )
        assert request.status_code == 201

    jobs = await client.get(
        f"{API}/runners/{runner_id}/jobs", params={"page": 2, "per_page": 1}
    )

    assert jobs.status_code == 200
    assert len(jobs.json()) == 1
    assert jobs.headers["X-Total"] == "3"
    assert jobs.headers["X-Total-Pages"] == "3"
    assert jobs.headers["X-Page"] == "2"
    assert jobs.headers["X-Per-Page"] == "1"
    assert jobs.headers["X-Prev-Page"] == "1"
    assert jobs.headers["X-Next-Page"] == "3"
    assert 'rel="prev"' in jobs.headers["Link"]
    assert 'rel="next"' in jobs.headers["Link"]


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


async def test_project_cache_keys_are_sanitized_like_gitlab_runner(client):
    cache = b"normalized cache zip"

    upload = await client.put(
        f"{API}/projects/1/cache/foo/bar/../cache/ ",
        content=cache,
        headers={"Content-Type": "application/zip"},
    )

    assert upload.status_code == 201
    assert upload.json()["key"] == "foo/cache"

    download = await client.get(f"{API}/projects/1/cache/foo/cache")
    assert download.status_code == 200
    assert download.content == cache
    assert download.headers["x-gitlab-cache-key"] == "foo/cache"


async def test_project_cache_fallback_keys_are_sanitized(client):
    cache = b"sanitized fallback cache zip"

    upload = await client.put(
        f"{API}/projects/1/cache/fallback_key",
        content=cache,
        headers={"Content-Type": "application/zip"},
    )
    assert upload.status_code == 201

    download = await client.get(
        f"{API}/projects/1/cache/missing?fallback_keys=missing-secondary,fallback_key/%20/%20%5C%20%20%5C"
    )

    assert download.status_code == 200
    assert download.content == cache
    assert download.headers["x-gitlab-cache-key"] == "fallback_key"


async def test_project_cache_rejects_unsanitizable_primary_key(client):
    upload = await client.put(
        f"{API}/projects/1/cache/%20",
        content=b"bad cache",
        headers={"Content-Type": "application/zip"},
    )

    assert upload.status_code == 400
    assert "could not be sanitized" in upload.text


async def test_project_cache_missing_returns_404(client):
    resp = await client.get(f"{API}/projects/1/cache/missing")
    assert resp.status_code == 404
