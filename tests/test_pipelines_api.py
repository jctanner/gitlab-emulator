"""Minimal GitLab pipeline API tests."""

import base64
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from sqlalchemy import select

from app.models.ci import (
    CiSecretAccessEvent,
    CiVariable,
    Pipeline,
    PipelineJob,
    PipelineSchedule,
)
from app.models.group import Group
from app.services.pipeline_schedules import (
    compute_next_run_at,
    run_due_pipeline_schedules,
)
from app.services.delayed_jobs import promote_due_delayed_jobs
from tests.conftest import API, auth_headers


RUNNER_TOKEN = "glrt-emulator-runner-token"


def test_compute_next_run_at_supports_timezone_and_steps():
    after = datetime(2026, 6, 28, 7, 56, 10, tzinfo=timezone.utc)

    assert compute_next_run_at("*/5 * * * *", "UTC", after=after) == datetime(
        2026, 6, 28, 8, 0
    )
    assert compute_next_run_at("57 7 * * *", "UTC", after=after) == datetime(
        2026, 6, 28, 7, 57
    )
    assert compute_next_run_at(
        "0 3 * * *",
        "America/New_York",
        after=datetime(2026, 6, 28, 0, 0, tzinfo=timezone.utc),
    ) == datetime(2026, 6, 28, 7, 0)


async def _create_project(client, test_token):
    resp = await client.post(
        f"{API}/user/repos",
        json={"name": "ci-repo", "auto_init": True},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    return resp.json()


async def test_create_pipeline_with_one_job(client, test_token):
    project = await _create_project(client, test_token)

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={
            "ref": "main",
            "job": {
                "name": "smoke",
                "image": "alpine:3.20",
                "script": ["echo persisted"],
            },
        },
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 201
    pipeline = resp.json()
    assert pipeline["project_id"] == project["id"]
    assert pipeline["status"] == "pending"
    assert pipeline["ref"] == "main"

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs.status_code == 200
    assert jobs.json()[0]["name"] == "smoke"
    assert jobs.json()[0]["status"] == "pending"


async def test_pipeline_security_warnings_are_stored_and_diagnosed(client, test_token):
    project = await _create_project(client, test_token)
    settings = await client.put(
        f"{API}/projects/{project['id']}/ci/security_settings",
        headers=auth_headers(test_token),
        json={"ci_strict_security_mode": False},
    )
    assert settings.status_code == 200

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        headers=auth_headers(test_token),
        json={
            "ref": "main",
            "variables": [{"key": "CI_COMMIT_SHA", "value": "override"}],
            "job": {
                "name": "security_probe",
                "image": "alpine:latest",
                "script": ["echo security"],
            },
        },
    )
    assert resp.status_code == 201
    pipeline = resp.json()
    warning_types = {warning["type"] for warning in pipeline["security_warnings"]}
    assert warning_types == {
        "mutable_image_ref",
        "predefined_variable_override",
    }
    assert all(
        warning["strict_mode"] is False for warning in pipeline["security_warnings"]
    )

    diagnostics = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/diagnostics"
    )
    assert diagnostics.status_code == 200
    assert diagnostics.json()["security_warnings"] == pipeline["security_warnings"]


async def test_strict_security_mode_blocks_unsafe_pipeline(client, test_token):
    project = await _create_project(client, test_token)
    settings = await client.put(
        f"{API}/projects/{project['id']}/ci/security_settings",
        headers=auth_headers(test_token),
        json={"ci_strict_security_mode": True},
    )
    assert settings.status_code == 200

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={
            "ref": "main",
            "job": {
                "name": "strict_probe",
                "image": "alpine:latest",
                "script": ["echo blocked"],
            },
        },
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 400
    assert "strict security mode" in resp.text
    assert "mutable image" in resp.text


async def test_pipeline_variable_security_gate_blocks_and_allows_owner(
    client, db_session, test_token
):
    from tests.test_projects_api import _create_user_and_token

    project = await _create_project(client, test_token)
    maintainer, maintainer_token = await _create_user_and_token(
        db_session, "pipeline-maintainer"
    )
    developer, developer_token = await _create_user_and_token(
        db_session, "pipeline-developer"
    )
    for user, level in ((maintainer, 40), (developer, 30)):
        member = await client.post(
            f"{API}/projects/{project['id']}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201

    no_one = await client.put(
        f"{API}/projects/{project['id']}/ci/security_settings",
        headers=auth_headers(test_token),
        json={"ci_pipeline_variables_minimum_override_role": "no_one_allowed"},
    )
    assert no_one.status_code == 200

    blocked = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        headers=auth_headers(test_token),
        json={
            "ref": "main",
            "variables": [{"key": "CUSTOM", "value": "blocked"}],
            "job": {"name": "vars", "script": ["echo vars"]},
        },
    )
    assert blocked.status_code == 400
    assert "Pipeline variables are not allowed" in blocked.text

    owner_only = await client.put(
        f"{API}/projects/{project['id']}/ci/security_settings",
        headers=auth_headers(test_token),
        json={"ci_pipeline_variables_minimum_override_role": "owner"},
    )
    assert owner_only.status_code == 200

    anonymous = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={
            "ref": "main",
            "variables": [{"key": "CUSTOM", "value": "anonymous"}],
            "job": {"name": "vars", "script": ["echo vars"]},
        },
    )
    assert anonymous.status_code == 401

    developer_denied = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        headers=auth_headers(developer_token),
        json={
            "ref": "main",
            "variables": [{"key": "CUSTOM", "value": "developer"}],
            "job": {"name": "vars", "script": ["echo vars"]},
        },
    )
    assert developer_denied.status_code == 400

    allowed = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        headers=auth_headers(test_token),
        json={
            "ref": "main",
            "variables": [{"key": "CUSTOM", "value": "owner"}],
            "job": {"name": "vars", "script": ["echo vars"]},
        },
    )
    assert allowed.status_code == 201

    maintainer_only = await client.put(
        f"{API}/projects/{project['id']}/ci/security_settings",
        headers=auth_headers(test_token),
        json={"ci_pipeline_variables_minimum_override_role": "maintainer"},
    )
    assert maintainer_only.status_code == 200

    maintainer_allowed = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        headers=auth_headers(maintainer_token),
        json={
            "ref": "main",
            "variables": [{"key": "CUSTOM", "value": "maintainer"}],
            "job": {"name": "vars", "script": ["echo vars"]},
        },
    )
    assert maintainer_allowed.status_code == 201


async def test_ci_management_actions_require_project_roles(
    client, db_session, test_token
):
    from tests.test_projects_api import _create_user_and_token

    project = await _create_project(client, test_token)
    reporter, reporter_token = await _create_user_and_token(
        db_session, "ci-action-reporter"
    )
    developer, developer_token = await _create_user_and_token(
        db_session, "ci-action-developer"
    )
    maintainer, maintainer_token = await _create_user_and_token(
        db_session, "ci-action-maintainer"
    )
    for user, level in ((reporter, 20), (developer, 30), (maintainer, 40)):
        member = await client.post(
            f"{API}/projects/{project['id']}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201

    pipeline_create_denied = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={
            "ref": "main",
            "job": {"name": "reporter_role_gate", "script": ["echo denied"]},
        },
        headers=auth_headers(reporter_token),
    )
    assert pipeline_create_denied.status_code == 403

    pipeline_create_allowed = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={
            "ref": "main",
            "job": {"name": "developer_role_gate", "script": ["echo allowed"]},
        },
        headers=auth_headers(developer_token),
    )
    assert pipeline_create_allowed.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={
            "ref": "main",
            "job": {"name": "role_gate", "script": ["echo role gate"]},
        },
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    cancel_denied = await client.post(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/cancel",
        headers=auth_headers(reporter_token),
    )
    assert cancel_denied.status_code == 403

    cancel_allowed = await client.post(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/cancel",
        headers=auth_headers(developer_token),
    )
    assert cancel_allowed.status_code == 200

    trigger_denied = await client.post(
        f"{API}/projects/{project['id']}/triggers",
        json={"description": "developer trigger"},
        headers=auth_headers(developer_token),
    )
    assert trigger_denied.status_code == 403

    trigger_allowed = await client.post(
        f"{API}/projects/{project['id']}/triggers",
        json={"description": "maintainer trigger"},
        headers=auth_headers(maintainer_token),
    )
    assert trigger_allowed.status_code == 201

    schedule_denied = await client.post(
        f"{API}/projects/{project['id']}/pipeline_schedules",
        json={"description": "reporter schedule", "ref": "main"},
        headers=auth_headers(reporter_token),
    )
    assert schedule_denied.status_code == 403

    schedule_allowed = await client.post(
        f"{API}/projects/{project['id']}/pipeline_schedules",
        json={"description": "developer schedule", "ref": "main"},
        headers=auth_headers(developer_token),
    )
    assert schedule_allowed.status_code == 201


async def test_private_project_pipeline_reads_require_reporter_access(
    client, db_session, test_token
):
    from tests.test_projects_api import _create_user_and_token

    reporter, reporter_token = await _create_user_and_token(
        db_session, "private-pipeline-reporter"
    )
    _outsider, outsider_token = await _create_user_and_token(
        db_session, "private-pipeline-outsider"
    )
    project = await client.post(
        f"{API}/projects",
        json={"name": "private-pipeline-project", "visibility": "private"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]
    pipeline_resp = await client.post(
        f"{API}/projects/{project_id}/pipeline",
        json={
            "ref": "main",
            "job": {"name": "private_job", "script": ["echo private"]},
        },
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()
    jobs = await client.get(
        f"{API}/projects/{project_id}/pipelines/{pipeline['id']}/jobs",
        headers=auth_headers(test_token),
    )
    assert jobs.status_code == 200
    job_id = jobs.json()[0]["id"]

    outsider_list = await client.get(
        f"{API}/projects/{project_id}/pipelines",
        headers=auth_headers(outsider_token),
    )
    assert outsider_list.status_code == 404

    member = await client.post(
        f"{API}/projects/{project_id}/members",
        json={"user_id": reporter.id, "access_level": 20},
        headers=auth_headers(test_token),
    )
    assert member.status_code == 201

    allowed_list = await client.get(
        f"{API}/projects/{project_id}/pipelines",
        headers=auth_headers(reporter_token),
    )
    assert allowed_list.status_code == 200
    assert [item["id"] for item in allowed_list.json()] == [pipeline["id"]]

    allowed_pipeline = await client.get(
        f"{API}/projects/{project_id}/pipelines/{pipeline['id']}",
        headers=auth_headers(reporter_token),
    )
    assert allowed_pipeline.status_code == 200
    assert allowed_pipeline.json()["id"] == pipeline["id"]

    allowed_jobs = await client.get(
        f"{API}/projects/{project_id}/jobs",
        headers=auth_headers(reporter_token),
    )
    assert allowed_jobs.status_code == 200
    assert [job["name"] for job in allowed_jobs.json()] == ["private_job"]

    allowed_trace = await client.get(
        f"{API}/projects/{project_id}/jobs/{job_id}/trace",
        headers=auth_headers(reporter_token),
    )
    assert allowed_trace.status_code == 200


async def test_cancel_pipeline_marks_pending_jobs_canceled(client, test_token):
    project = await _create_project(client, test_token)
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={
            "ref": "main",
            "job": {
                "name": "cancel_me",
                "image": "alpine:3.20",
                "script": ["echo cancel"],
            },
        },
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    cancel = await client.post(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/cancel",
        headers=auth_headers(test_token),
    )
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "canceled"

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs.status_code == 200
    assert jobs.json()[0]["status"] == "canceled"


async def test_retry_job_requeues_failed_job_for_runner(client, test_token):
    project = await _create_project(client, test_token)
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={
            "ref": "main",
            "job": {
                "name": "retry_me",
                "image": "alpine:3.20",
                "script": ["exit 1"],
            },
        },
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    payload = request.json()
    job_id = payload["id"]
    job_token = payload["token"]

    trace = await client.patch(
        f"{API}/jobs/{job_id}/trace?debug_trace=false",
        headers={"JOB-TOKEN": job_token, "Content-Range": "0-6"},
        content=b"failed",
    )
    assert trace.status_code == 202

    update = await client.put(
        f"{API}/jobs/{job_id}",
        headers={"JOB-TOKEN": job_token},
        json={"token": job_token, "state": "failed", "exit_code": 1},
    )
    assert update.status_code == 200

    retry = await client.post(
        f"{API}/projects/{project['id']}/jobs/{job_id}/retry",
        headers=auth_headers(test_token),
    )
    assert retry.status_code == 200
    retried = retry.json()
    assert retried["id"] == job_id
    assert retried["status"] == "pending"

    raw_trace = await client.get(f"{API}/projects/{project['id']}/jobs/{job_id}/trace")
    assert raw_trace.status_code == 200
    assert raw_trace.text == ""

    pipeline_after_retry = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}"
    )
    assert pipeline_after_retry.json()["status"] == "pending"

    next_request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert next_request.status_code == 201
    assert next_request.json()["id"] == job_id
    assert next_request.json()["token"] != job_token


async def test_job_retry_keyword_auto_requeues_failed_job(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
retry_me:
  retry:
    max: 1
    when: script_failure
  script:
    - exit 1
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add retry keyword ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    first_request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert first_request.status_code == 201
    first_payload = first_request.json()
    first_update = await client.put(
        f"{API}/jobs/{first_payload['id']}",
        headers={"JOB-TOKEN": first_payload["token"]},
        json={
            "token": first_payload["token"],
            "state": "failed",
            "failure_reason": "script_failure",
            "exit_code": 1,
        },
    )
    assert first_update.status_code == 200

    jobs_after_first_failure = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs_after_first_failure.status_code == 200
    retried_job = jobs_after_first_failure.json()[0]
    assert retried_job["status"] == "pending"
    assert retried_job["retry_attempt"] == 1

    second_request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert second_request.status_code == 201
    second_payload = second_request.json()
    assert second_payload["id"] == first_payload["id"]
    assert second_payload["token"] != first_payload["token"]

    second_update = await client.put(
        f"{API}/jobs/{second_payload['id']}",
        headers={"JOB-TOKEN": second_payload["token"]},
        json={
            "token": second_payload["token"],
            "state": "failed",
            "failure_reason": "script_failure",
            "exit_code": 1,
        },
    )
    assert second_update.status_code == 200
    jobs_after_second_failure = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs_after_second_failure.status_code == 200
    terminal_job = jobs_after_second_failure.json()[0]
    assert terminal_job["status"] == "failed"
    assert terminal_job["retry_attempt"] == 1


async def test_resource_group_serializes_runner_assignment(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
deploy_one:
  resource_group: production
  script:
    - echo one

deploy_two:
  resource_group: production
  script:
    - echo two
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add resource group ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    first_request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert first_request.status_code == 201
    first_payload = first_request.json()
    assert first_payload["job_info"]["name"] == "deploy_one"

    blocked_request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert blocked_request.status_code == 204

    diagnostics = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/diagnostics",
        headers=auth_headers(test_token),
    )
    assert diagnostics.status_code == 200
    deploy_two = next(
        job for job in diagnostics.json()["jobs"] if job["job_name"] == "deploy_two"
    )
    assert deploy_two["blocked"] is True
    assert deploy_two["blockers"][0]["type"] == "resource_group"
    assert deploy_two["blockers"][0]["job"] == "deploy_one"

    update = await client.put(
        f"{API}/jobs/{first_payload['id']}",
        headers={"JOB-TOKEN": first_payload["token"]},
        json={"token": first_payload["token"], "state": "success"},
    )
    assert update.status_code == 200

    second_request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert second_request.status_code == 201
    assert second_request.json()["job_info"]["name"] == "deploy_two"


async def test_new_same_ref_pipeline_cancels_interruptible_jobs(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
interruptible_job:
  interruptible: true
  script:
    - echo interruptible
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add interruptible ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    first_pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert first_pipeline_resp.status_code == 201
    first_pipeline = first_pipeline_resp.json()

    first_request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert first_request.status_code == 201
    first_payload = first_request.json()
    assert first_payload["job_info"]["name"] == "interruptible_job"

    second_pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert second_pipeline_resp.status_code == 201
    second_pipeline = second_pipeline_resp.json()

    first_jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{first_pipeline['id']}/jobs",
        headers=auth_headers(test_token),
    )
    assert first_jobs.status_code == 200
    assert first_jobs.json()[0]["status"] == "canceled"

    first_pipeline_after = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{first_pipeline['id']}",
        headers=auth_headers(test_token),
    )
    assert first_pipeline_after.status_code == 200
    assert first_pipeline_after.json()["status"] == "canceled"

    second_request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert second_request.status_code == 201
    assert second_request.json()["id"] != first_payload["id"]
    assert second_request.json()["job_info"]["pipeline_id"] == second_pipeline["id"]


async def test_pipeline_diagnostics_marks_stale_running_job(
    client, test_token, db_session
):
    project = await _create_project(client, test_token)
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={
            "ref": "main",
            "job": {
                "name": "stale_me",
                "image": "alpine:3.20",
                "script": ["sleep 600"],
            },
        },
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN, "info": {"name": "stale-runner"}},
    )
    assert request.status_code == 201
    job_id = request.json()["id"]

    result = await db_session.execute(
        select(PipelineJob).where(PipelineJob.id == job_id)
    )
    job = result.scalar_one()
    job.started_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    await db_session.commit()

    diagnostics = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/diagnostics"
    )
    assert diagnostics.status_code == 200
    job_diagnostic = diagnostics.json()["jobs"][0]
    assert job_diagnostic["status"] == "running"
    assert job_diagnostic["stale"] is True
    assert job_diagnostic["blocked"] is True
    assert job_diagnostic["blockers"][0]["type"] == "stale_running_job"
    assert job_diagnostic["recovery"]["operator_requeue"] is True
    assert job_diagnostic["recovery"]["gitlab_compatible_flow"] == "cancel_then_retry"


async def test_retry_pipeline_requeues_failed_and_skipped_jobs(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages: [build, test]
compile:
  stage: build
  script:
    - exit 1
unit:
  stage: test
  script:
    - echo skipped
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add retry ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    first_payload = request.json()
    update = await client.put(
        f"{API}/jobs/{first_payload['id']}",
        headers={"JOB-TOKEN": first_payload["token"]},
        json={"token": first_payload["token"], "state": "failed", "exit_code": 1},
    )
    assert update.status_code == 200

    retry = await client.post(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/retry",
        headers=auth_headers(test_token),
    )
    assert retry.status_code == 200
    assert retry.json()["status"] == "pending"

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs.status_code == 200
    assert {job["name"]: job["status"] for job in jobs.json()} == {
        "compile": "pending",
        "unit": "pending",
    }


async def test_pipeline_and_job_routes_accept_encoded_project_path(client, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "ci-path-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]
    project_ref = "testuser%2Fci-path-project"
    ci_yaml = """
stages: [build]
path_smoke:
  stage: build
  script:
    - echo path smoke
"""
    write = await client.post(
        f"{API}/projects/{project_ref}/repository/files/.gitlab-ci.yml",
        json={
            "branch": "main",
            "commit_message": "add ci",
            "content": ci_yaml,
        },
        headers=auth_headers(test_token),
    )
    assert write.status_code == 201

    created = await client.post(
        f"{API}/projects/{project_ref}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert created.status_code == 201
    pipeline = created.json()
    assert pipeline["project_id"] == project_id

    listed = await client.get(f"{API}/projects/{project_ref}/pipelines")
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == pipeline["id"]

    got = await client.get(f"{API}/projects/{project_ref}/pipelines/{pipeline['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == pipeline["id"]

    latest = await client.get(
        f"{API}/projects/{project_ref}/pipelines/latest",
        params={"ref": "main"},
    )
    assert latest.status_code == 200
    assert latest.json()["id"] == pipeline["id"]

    jobs = await client.get(
        f"{API}/projects/{project_ref}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs.status_code == 200
    job = jobs.json()[0]
    assert job["name"] == "path_smoke"

    project_jobs = await client.get(f"{API}/projects/{project_ref}/jobs")
    assert project_jobs.status_code == 200
    assert project_jobs.json()[0]["id"] == job["id"]

    got_job = await client.get(f"{API}/projects/{project_ref}/jobs/{job['id']}")
    assert got_job.status_code == 200
    assert got_job.json()["name"] == "path_smoke"

    trace = await client.get(f"{API}/projects/{project_ref}/jobs/{job['id']}/trace")
    assert trace.status_code == 200
    assert trace.text == ""


async def test_download_job_artifacts_by_encoded_project_path_and_ref(
    client, test_token
):
    project = await client.post(
        f"{API}/projects",
        json={"name": "ci-artifact-path-project", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]
    project_ref = "testuser%2Fci-artifact-path-project"

    pipeline_resp = await client.post(
        f"{API}/projects/{project_ref}/pipeline",
        json={
            "ref": "main",
            "job": {
                "name": "artifact_job",
                "image": "alpine:3.20",
                "script": ["echo artifact"],
                "tags": ["client-artifact"],
                "artifacts_paths": ["out/result.txt"],
            },
        },
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={
            "token": RUNNER_TOKEN,
            "info": {
                "name": "artifact-test-runner",
                "config": {"tag_list": "client-artifact"},
            },
        },
    )
    assert request.status_code == 201
    payload = request.json()
    assert payload["job_info"]["project_id"] == project_id
    job_id = payload["id"]
    job_token = payload["token"]

    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("out/result.txt", "artifact content\n")
    archive_bytes = archive_buffer.getvalue()
    upload = await client.post(
        f"{API}/jobs/{job_id}/artifacts?artifact_format=zip&artifact_type=archive",
        headers={"JOB-TOKEN": job_token, "Content-Type": "application/zip"},
        content=archive_bytes,
    )
    assert upload.status_code == 201

    update = await client.put(
        f"{API}/jobs/{job_id}",
        headers={"JOB-TOKEN": job_token},
        json={"token": job_token, "state": "success", "exit_code": 0},
    )
    assert update.status_code == 200

    download = await client.get(
        f"{API}/projects/{project_ref}/jobs/artifacts/main/download",
        params={"job": "artifact_job"},
    )
    assert download.status_code == 200
    assert download.content == archive_bytes


async def test_runner_executes_persisted_pipeline_job(client, test_token):
    project = await _create_project(client, test_token)
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={
            "ref": "main",
            "job": {
                "name": "smoke",
                "image": "alpine:3.20",
                "script": ["echo persisted"],
                "cache": [
                    {
                        "key": "pip-cache",
                        "paths": [".cache/pip"],
                        "policy": "pull-push",
                        "when": "on_success",
                        "fallback_keys": ["pip-fallback"],
                    }
                ],
                "artifacts_paths": ["out/result.txt"],
            },
        },
        headers=auth_headers(test_token),
    )
    pipeline = pipeline_resp.json()

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={
            "token": RUNNER_TOKEN,
            "info": {"name": "test-runner", "executor": "docker"},
        },
    )
    assert request.status_code == 201
    payload = request.json()
    assert payload["job_info"]["pipeline_id"] == pipeline["id"]
    assert payload["job_info"]["project_id"] == project["id"]
    assert payload["allow_git_fetch"] is True
    assert payload["git_info"]["repo_url"].startswith(
        "http://gitlab-ci-token:gljt-persisted-"
    )
    assert payload["git_info"]["repo_url"].endswith("@testserver/testuser/ci-repo.git")
    assert payload["inputs"] == []
    variables = {item["key"]: item["value"] for item in payload["variables"]}
    assert variables["GIT_STRATEGY"] == "fetch"
    assert variables["CI_REPOSITORY_URL"] == payload["git_info"]["repo_url"]
    assert variables["CI_COMMIT_SHA"] == pipeline["sha"]
    assert variables["CI_COMMIT_REF_NAME"] == "main"
    assert payload["cache"] == [
        {
            "key": "pip-cache",
            "untracked": False,
            "unprotect": False,
            "policy": "pull-push",
            "paths": [".cache/pip"],
            "when": "on_success",
            "fallback_keys": ["pip-fallback"],
        }
    ]
    assert payload["artifacts"] == [
        {
            "name": "artifacts",
            "untracked": False,
            "paths": ["out/result.txt"],
            "exclude": [],
            "when": "on_success",
            "artifact_type": "archive",
            "artifact_format": "zip",
            "expire_in": "",
        }
    ]

    job_id = payload["id"]
    job_token = payload["token"]

    trace = await client.patch(
        f"{API}/jobs/{job_id}/trace?debug_trace=false",
        headers={"JOB-TOKEN": job_token, "Content-Range": "0-8"},
        content=b"persisted",
    )
    assert trace.status_code == 202

    update = await client.put(
        f"{API}/jobs/{job_id}",
        headers={"JOB-TOKEN": job_token},
        json={
            "token": job_token,
            "state": "success",
            "output": {"checksum": "crc32:test", "bytesize": 9},
            "exit_code": 0,
        },
    )
    assert update.status_code == 200
    assert update.headers["Job-Status"] == "success"

    pipeline_after = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}"
    )
    assert pipeline_after.json()["status"] == "success"

    jobs = await client.get(f"{API}/projects/{project['id']}/jobs")
    assert jobs.status_code == 200
    assert jobs.json()[0]["status"] == "success"
    assert jobs.json()[0]["cache"][0]["key"] == "pip-cache"
    assert jobs.json()[0]["runner"]["description"] == "test-runner"

    raw_trace = await client.get(f"{API}/projects/{project['id']}/jobs/{job_id}/trace")
    assert raw_trace.status_code == 200
    assert raw_trace.text == "persisted"

    archive = b"fake artifact zip"
    artifact_upload = await client.post(
        f"{API}/jobs/{job_id}/artifacts?artifact_format=zip&artifact_type=archive",
        headers={"JOB-TOKEN": job_token, "Content-Type": "application/zip"},
        content=archive,
    )
    assert artifact_upload.status_code == 201

    job_after_artifact = await client.get(
        f"{API}/projects/{project['id']}/jobs/{job_id}"
    )
    assert job_after_artifact.status_code == 200
    artifacts = job_after_artifact.json()["artifacts"]
    assert artifacts[0]["filename"] == f"job-{job_id}-artifacts.zip"
    assert artifacts[0]["size"] == len(archive)

    artifact_download = await client.get(
        f"{API}/projects/{project['id']}/jobs/{job_id}/artifacts"
    )
    assert artifact_download.status_code == 200
    assert artifact_download.content == archive


async def test_artifact_metadata_reaches_runner_payload(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
variables:
  ARTIFACT_NAME: "$CI_COMMIT_REF_NAME-artifacts"
  OUTPUT_DIR: out
  EXCLUDE_DIR: tmp

artifact_metadata:
  image: alpine:3.20
  script:
    - echo metadata
  artifacts:
    name: "$ARTIFACT_NAME"
    paths:
      - "$OUTPUT_DIR/"
    exclude:
      - "$OUTPUT_DIR/$EXCLUDE_DIR/"
    when: always
    expire_in: "$ARTIFACT_TTL"
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add artifact metadata ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={
            "ref": "main",
            "variables": [{"key": "ARTIFACT_TTL", "value": "1 week"}],
        },
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    artifact = request.json()["artifacts"][0]
    assert artifact["name"] == "main-artifacts"
    assert artifact["paths"] == ["out/"]
    assert artifact["exclude"] == ["out/tmp/"]
    assert artifact["when"] == "always"
    assert artifact["expire_in"] == "1 week"


async def test_services_reach_api_and_runner_payload(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
variables:
  POSTGRES_VERSION: "16"

service_job:
  services:
    - "postgres:$POSTGRES_VERSION"
    - name: mysql:8
      alias: db mysql
      command: ["--default-authentication-plugin=mysql_native_password"]
      entrypoint:
        - docker-entrypoint.sh
      pull_policy: [if-not-present]
      variables:
        MYSQL_DATABASE: app
  script:
    - echo services
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add service containers ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    expected_services = [
        {"name": "postgres:16"},
        {
            "name": "mysql:8",
            "alias": "db mysql",
            "command": ["--default-authentication-plugin=mysql_native_password"],
            "entrypoint": ["docker-entrypoint.sh"],
            "pull_policy": ["if-not-present"],
            "variables": [
                {
                    "key": "MYSQL_DATABASE",
                    "value": "app",
                    "public": True,
                    "file": False,
                    "masked": False,
                    "raw": False,
                }
            ],
        },
    ]

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs",
        headers=auth_headers(test_token),
    )
    assert jobs.status_code == 200
    assert jobs.json()[0]["services"] == expected_services

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    assert request.json()["services"] == expected_services


async def test_image_metadata_reaches_api_and_runner_payload(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
variables:
  ENTRYPOINT: /bin/sh

image_job:
  image:
    name: alpine:3.20
    entrypoint:
      - "$ENTRYPOINT"
      - -lc
    pull_policy: [if-not-present]
  script:
    - echo image
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add image metadata ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs",
        headers=auth_headers(test_token),
    )
    assert jobs.status_code == 200
    job = jobs.json()[0]
    assert job["image"] == "alpine:3.20"
    assert job["image_config"] == {
        "entrypoint": ["/bin/sh", "-lc"],
        "pull_policy": ["if-not-present"],
    }

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    assert request.json()["image"] == {
        "name": "alpine:3.20",
        "entrypoint": ["/bin/sh", "-lc"],
        "pull_policy": ["if-not-present"],
    }


async def test_job_runtime_metadata_reaches_api_and_runner_payload(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
metadata_job:
  image: alpine:3.20
  retry:
    max: 2
    when: runner_system_failure
  timeout: 45 minutes
  interruptible: true
  resource_group: production
  coverage: '/Coverage: \\d+\\.\\d+%/'
  script:
    - echo metadata
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add runtime metadata ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    jobs_resp = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs",
        headers=auth_headers(test_token),
    )
    assert jobs_resp.status_code == 200
    job = jobs_resp.json()[0]
    assert job["retry"] == {"max": 2, "when": ["runner_system_failure"]}
    assert job["retry_attempt"] == 0
    assert job["timeout"] == 2700
    assert job["interruptible"] is True
    assert job["resource_group"] == "production"
    assert job["coverage"] is None
    assert job["coverage_regex"] == "/Coverage: \\d+\\.\\d+%/"

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    payload = request.json()
    assert payload["runner_info"]["timeout"] == 2700
    assert payload["steps"][0]["timeout"] == 2700

    trace = await client.patch(
        f"{API}/jobs/{payload['id']}/trace?debug_trace=false",
        headers={"JOB-TOKEN": payload["token"], "Content-Range": "0-14"},
        content=b"Coverage: 87.5%",
    )
    assert trace.status_code == 202
    update = await client.put(
        f"{API}/jobs/{payload['id']}",
        json={"token": payload["token"], "state": "success"},
    )
    assert update.status_code == 200

    completed_job_resp = await client.get(
        f"{API}/projects/{project['id']}/jobs/{payload['id']}",
        headers=auth_headers(test_token),
    )
    assert completed_job_resp.status_code == 200
    assert completed_job_resp.json()["coverage"] == "87.5"


async def test_compound_timeout_reaches_runner_payload(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
compound_timeout:
  image: alpine:3.20
  timeout: 1 hour 30 minutes
  script:
    - echo timeout
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add compound timeout ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs",
        headers=auth_headers(test_token),
    )
    assert jobs.status_code == 200
    assert jobs.json()[0]["timeout"] == 5400

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    payload = request.json()
    assert payload["runner_info"]["timeout"] == 5400
    assert payload["steps"][0]["timeout"] == 5400


async def test_default_runtime_metadata_reaches_runner_payload(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
default:
  retry: 1
  timeout: 1 hour
  interruptible: true

defaulted:
  image: alpine:3.20
  script:
    - echo defaulted
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add default runtime ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs",
        headers=auth_headers(test_token),
    )
    assert jobs.status_code == 200
    job = jobs.json()[0]
    assert job["retry"] == {"max": 1, "when": []}
    assert job["timeout"] == 3600
    assert job["interruptible"] is True

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    payload = request.json()
    assert payload["runner_info"]["timeout"] == 3600
    assert payload["steps"][0]["timeout"] == 3600

    update = await client.put(
        f"{API}/jobs/{payload['id']}",
        headers={"JOB-TOKEN": payload["token"]},
        json={
            "token": payload["token"],
            "state": "failed",
            "failure_reason": "script_failure",
            "exit_code": 1,
        },
    )
    assert update.status_code == 200

    retried_jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs",
        headers=auth_headers(test_token),
    )
    assert retried_jobs.status_code == 200
    assert retried_jobs.json()[0]["status"] == "pending"
    assert retried_jobs.json()[0]["retry_attempt"] == 1


async def test_cache_variables_expand_in_runner_payload(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
variables:
  CACHE_POLICY: push

cache_probe:
  variables:
    CACHE_DIR: .cache
    CACHE_WHEN: always
    LOCKFILE: uv.lock
  rules:
    - variables:
        CACHE_PREFIX: "$CI_COMMIT_REF_NAME"
  cache:
    key:
      prefix: "$CACHE_PREFIX"
      files:
        - "$LOCKFILE"
    paths:
      - "$CACHE_DIR/"
    policy: "$CACHE_POLICY"
    when: "$CACHE_WHEN"
    unprotect: true
    fallback_keys:
      - "$CI_COMMIT_REF_NAME-fallback"
  script:
    - echo cache
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add cache variable ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    assert request.json()["cache"] == [
        {
            "key": "main-uv.lock",
            "untracked": False,
            "unprotect": True,
            "policy": "push",
            "paths": [".cache/"],
            "when": "always",
            "fallback_keys": ["main-fallback"],
        }
    ]


async def test_expired_artifact_upload_is_not_downloadable(client, test_token):
    project = await _create_project(client, test_token)
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={
            "ref": "main",
            "job": {
                "name": "expiring_artifact",
                "image": "alpine:3.20",
                "script": ["echo expiring"],
                "artifacts_paths": ["out/result.txt"],
                "artifacts": {
                    "name": "expiring",
                    "paths": ["out/result.txt"],
                    "expire_in": "0 seconds",
                },
            },
        },
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    payload = request.json()
    job_id = payload["id"]
    job_token = payload["token"]
    assert payload["artifacts"][0]["expire_in"] == "0 seconds"

    artifact_upload = await client.post(
        f"{API}/jobs/{job_id}/artifacts?artifact_format=gzip&artifact_type=metadata",
        headers={"JOB-TOKEN": job_token, "Content-Type": "application/gzip"},
        content=b"expired artifact",
    )
    assert artifact_upload.status_code == 201

    job_after_artifact = await client.get(
        f"{API}/projects/{project['id']}/jobs/{job_id}"
    )
    artifact = job_after_artifact.json()["artifacts"][0]
    assert artifact["file_type"] == "metadata"
    assert artifact["file_format"] == "gzip"
    assert artifact["expire_at"] is not None

    artifact_download = await client.get(
        f"{API}/projects/{project['id']}/jobs/{job_id}/artifacts"
    )
    assert artifact_download.status_code == 404


async def test_create_pipeline_from_gitlab_ci_yaml(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages:
  - build
  - test
image: alpine:3.20
variables:
  GLOBAL: one
before_script:
  - echo before

unit:
  stage: test
  script:
    - echo test

compile:
  stage: build
  variables:
    LOCAL: two
  script:
    - echo build
  artifacts:
    paths:
      - out/report.txt
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    commit_sha = write.json()["commit"]["sha"]

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    pipeline = resp.json()
    assert pipeline["sha"] == commit_sha

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs.status_code == 200
    data = jobs.json()
    assert [job["name"] for job in data] == ["compile", "unit"]
    assert [job["stage"] for job in data] == ["build", "test"]

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    payload = request.json()
    assert payload["job_info"]["name"] == "compile"
    assert payload["steps"][0]["script"] == ["echo before", "echo build"]
    assert payload["artifacts"][0]["paths"] == ["out/report.txt"]
    assert {
        "key": "GLOBAL",
        "value": "one",
        "public": True,
        "file": False,
        "masked": False,
        "raw": False,
    } in payload["variables"]
    assert {
        "key": "LOCAL",
        "value": "two",
        "public": True,
        "file": False,
        "masked": False,
        "raw": False,
    } in payload["variables"]


async def test_create_pipeline_from_gitlab_ci_yaml_for_tag_ref(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages: [test]

tag_only:
  script:
    - echo tag $CI_COMMIT_TAG
  only: [tags]

branch_only:
  script:
    - echo branch $CI_COMMIT_BRANCH
  only: [branches]

skip_tag:
  script:
    - echo skip tag
  except: [tags]
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add tag ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    tag = await client.post(
        f"{API}/projects/{project['id']}/repository/tags",
        headers=auth_headers(test_token),
        json={"tag_name": "v1.2.3", "ref": "main"},
    )
    assert tag.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "v1.2.3"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    pipeline = resp.json()
    assert pipeline["sha"] == tag.json()["commit"]["id"]

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs.status_code == 200
    assert [job["name"] for job in jobs.json()] == ["tag_only"]

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    payload = request.json()
    assert payload["job_info"]["name"] == "tag_only"
    variables = {item["key"]: item["value"] for item in payload["variables"]}
    assert variables["CI_COMMIT_REF_NAME"] == "v1.2.3"
    assert variables["CI_COMMIT_TAG"] == "v1.2.3"
    assert "CI_COMMIT_BRANCH" not in variables


async def test_create_pipeline_rejects_unsupported_gitlab_ci_execution_keyword(
    client, test_token
):
    project = await _create_project(client, test_token)
    ci_yaml = """
deploy_downstream:
  trigger:
    project: group/downstream
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add unsupported ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 400
    assert "unsupported GitLab CI keyword" in resp.text
    assert "trigger" in resp.text


async def test_create_pipeline_accepts_rules_changes_compare_to_option(
    client, test_token
):
    project = await _create_project(client, test_token)
    ci_yaml = """
compare_to_job:
  script:
    - echo compare-to
  rules:
    - changes:
        compare_to: refs/heads/main
        paths:
          - .gitlab-ci.yml
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add compare_to rules option",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{resp.json()['id']}/jobs",
        headers=auth_headers(test_token),
    )
    assert jobs.status_code == 200
    assert jobs.json()[0]["name"] == "compare_to_job"


async def test_create_pipeline_rejects_unsupported_cache_option(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
cache_probe:
  cache:
    paths:
      - .cache/
    policy: invalid
  script:
    - echo cache
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add unsupported cache ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 400
    assert "cache policy is not supported" in resp.text
    assert "invalid" in resp.text


async def test_create_pipeline_schedules_delayed_gitlab_ci_job(
    client, db_session, test_token
):
    project = await _create_project(client, test_token)
    ci_yaml = """
delayed_job:
  script:
    - echo delayed
  when: delayed
  start_in: 10 minutes
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add delayed ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    pipeline = resp.json()
    assert pipeline["status"] == "pending"

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs.status_code == 200
    job = jobs.json()[0]
    assert job["name"] == "delayed_job"
    assert job["status"] == "scheduled"
    assert job["when"] == "delayed"
    assert job["scheduled_at"] is not None

    waiting = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert waiting.status_code == 204

    diagnostics = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/diagnostics"
    )
    assert diagnostics.status_code == 200
    job_diagnostic = diagnostics.json()["jobs"][0]
    assert job_diagnostic["status"] == "scheduled"
    assert job_diagnostic["blocked"] is True
    assert job_diagnostic["blockers"][0]["type"] == "delayed"

    result = await db_session.execute(
        select(PipelineJob).where(PipelineJob.id == job["id"])
    )
    persisted_job = result.scalar_one()
    persisted_job.scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()

    ready = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert ready.status_code == 201
    body = ready.json()
    assert body["job_info"]["name"] == "delayed_job"
    assert body["steps"][0]["when"] == "on_success"


async def test_due_delayed_jobs_are_promoted_without_runner_poll(
    client, db_session, test_token
):
    project = await _create_project(client, test_token)
    ci_yaml = """
delayed_job:
  script:
    - echo delayed
  when: delayed
  start_in: 10 minutes
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add delayed ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    pipeline = resp.json()
    job = (
        await db_session.execute(
            select(PipelineJob).where(PipelineJob.pipeline_id == pipeline["id"])
        )
    ).scalar_one()
    job.scheduled_at = datetime(2026, 6, 28, 8, 0)
    await db_session.commit()

    stats = await promote_due_delayed_jobs(
        db_session,
        now=datetime(2026, 6, 28, 8, 1, tzinfo=timezone.utc),
    )
    assert stats.checked == 1
    assert stats.promoted == 1

    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.scheduled_at is None
    assert job.queued_at == datetime(2026, 6, 28, 8, 1)


async def test_create_pipeline_rejects_unknown_gitlab_ci_when(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
unknown_when:
  script:
    - echo invalid
  rules:
    - when: eventually
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add unknown when ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 400
    assert "when value is not supported" in resp.text
    assert "eventually" in resp.text


async def test_create_pipeline_rejects_workflow_rules_skip(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
workflow:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "trigger"'
    - when: never

job:
  script:
    - echo skipped
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add workflow ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 400
    assert "workflow rules skipped pipeline" in resp.text


async def test_pipeline_variables_merge_with_yaml_and_job_precedence(
    client, test_token
):
    project = await _create_project(client, test_token)
    ci_yaml = """
variables:
  FROM_YAML: yaml
  FROM_PIPELINE: yaml-override
  SHARED: yaml
  CI_COMMIT_REF_NAME: yaml-ref

vars:
  variables:
    SHARED: job
    JOB_ONLY: job
  script:
    - echo variables
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci variables",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        headers=auth_headers(test_token),
        json={
            "ref": "main",
            "variables": [
                {"key": "FROM_PIPELINE", "value": "pipeline"},
                {"key": "PIPELINE_ONLY", "value": "pipeline"},
                {"key": "SHARED", "value": "pipeline"},
                {"key": "CI_COMMIT_REF_NAME", "value": "pipeline-ref"},
            ],
        },
    )
    assert resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    variables = {item["key"]: item["value"] for item in request.json()["variables"]}

    assert variables["PIPELINE_ONLY"] == "pipeline"
    assert variables["FROM_PIPELINE"] == "yaml-override"
    assert variables["FROM_YAML"] == "yaml"
    assert variables["JOB_ONLY"] == "job"
    assert variables["SHARED"] == "job"
    assert variables["CI_COMMIT_REF_NAME"] == "yaml-ref"
    assert variables["CI_PROJECT_PATH"] == "testuser/ci-repo"


async def test_project_variables_reach_runner_payload_with_precedence(
    client, test_token
):
    project = await _create_project(client, test_token)
    ci_yaml = """
variables:
  FROM_PROJECT: yaml
  FROM_PIPELINE: yaml

project_probe:
  variables:
    SHARED: job
  script:
    - echo project variables
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add project variable ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    project_variable = await client.post(
        f"{API}/projects/{project['id']}/variables",
        headers=auth_headers(test_token),
        json={"key": "PROJECT_ONLY", "value": "project"},
    )
    assert project_variable.status_code == 201
    overridden = await client.post(
        f"{API}/projects/{project['id']}/variables",
        headers=auth_headers(test_token),
        json={"key": "FROM_PROJECT", "value": "project"},
    )
    assert overridden.status_code == 201
    shared = await client.post(
        f"{API}/projects/{project['id']}/variables",
        headers=auth_headers(test_token),
        json={"key": "SHARED", "value": "project"},
    )
    assert shared.status_code == 201
    scoped = await client.post(
        f"{API}/projects/{project['id']}/variables",
        headers=auth_headers(test_token),
        json={
            "key": "SCOPED_OUT",
            "value": "production-only",
            "environment_scope": "production",
        },
    )
    assert scoped.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        headers=auth_headers(test_token),
        json={
            "ref": "main",
            "variables": [
                {"key": "FROM_PIPELINE", "value": "pipeline"},
                {"key": "PIPELINE_ONLY", "value": "pipeline"},
                {"key": "SHARED", "value": "pipeline"},
            ],
        },
    )
    assert pipeline_resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    variables = {item["key"]: item["value"] for item in request.json()["variables"]}

    assert variables["PROJECT_ONLY"] == "project"
    assert variables["PIPELINE_ONLY"] == "pipeline"
    assert variables["FROM_PROJECT"] == "yaml"
    assert variables["FROM_PIPELINE"] == "yaml"
    assert variables["SHARED"] == "job"
    assert "SCOPED_OUT" not in variables


async def test_protected_project_variables_require_protected_ref(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
protected_probe:
  script:
    - echo protected variables
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add protected variable ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    protected_var = await client.post(
        f"{API}/projects/{project['id']}/variables",
        headers=auth_headers(test_token),
        json={"key": "PROTECTED_ONLY", "value": "protected", "protected": True},
    )
    assert protected_var.status_code == 201

    unprotected_pipeline = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert unprotected_pipeline.status_code == 201
    unprotected_request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert unprotected_request.status_code == 201
    unprotected_variables = {
        item["key"]: item["value"] for item in unprotected_request.json()["variables"]
    }
    assert "PROTECTED_ONLY" not in unprotected_variables
    finish = await client.put(
        f"{API}/jobs/{unprotected_request.json()['id']}",
        headers={"JOB-TOKEN": unprotected_request.json()["token"]},
        json={"token": unprotected_request.json()["token"], "state": "success"},
    )
    assert finish.status_code == 200

    protect = await client.post(
        f"{API}/projects/{project['id']}/protected_branches",
        headers=auth_headers(test_token),
        json={"name": "main"},
    )
    assert protect.status_code == 201

    protected_pipeline = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert protected_pipeline.status_code == 201
    protected_request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert protected_request.status_code == 201
    protected_variables = {
        item["key"]: item["value"] for item in protected_request.json()["variables"]
    }
    assert protected_variables["PROTECTED_ONLY"] == "protected"


async def test_project_variable_environment_scope_matches_job_environment(
    client, test_token
):
    project = await _create_project(client, test_token)
    ci_yaml = """
production_job:
  environment: production
  script:
    - echo production

staging_job:
  environment:
    name: staging
  script:
    - echo staging

review_job:
  environment: review/app
  script:
    - echo review
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add environment variable ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    for payload in [
        {"key": "ENV_VALUE", "value": "default"},
        {"key": "ENV_VALUE", "value": "production", "environment_scope": "production"},
        {"key": "ENV_VALUE", "value": "review", "environment_scope": "review/*"},
    ]:
        variable = await client.post(
            f"{API}/projects/{project['id']}/variables",
            headers=auth_headers(test_token),
            json=payload,
        )
        assert variable.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201

    values_by_job = {}
    for _ in range(3):
        request = await client.post(
            f"{API}/jobs/request",
            headers={"RUNNER-TOKEN": RUNNER_TOKEN},
            json={"token": RUNNER_TOKEN},
        )
        assert request.status_code == 201
        payload = request.json()
        variables = {item["key"]: item["value"] for item in payload["variables"]}
        values_by_job[payload["job_info"]["name"]] = variables["ENV_VALUE"]

    assert values_by_job == {
        "production_job": "production",
        "staging_job": "default",
        "review_job": "review",
    }


async def test_job_secrets_resolve_to_runner_payload_and_access_events(
    client, test_token, db_session
):
    group = await client.post(
        f"{API}/groups",
        json={"path": "secret-ci", "name": "Secret CI"},
        headers=auth_headers(test_token),
    )
    assert group.status_code == 201
    project = await client.post(
        f"{API}/projects",
        json={
            "name": "secret-project",
            "namespace_path": "secret-ci",
            "initialize_with_readme": True,
        },
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    ci_yaml = """
secret_probe:
  environment: production
  secrets:
    DB_PASSWORD:
      gitlab_secrets_manager:
        name: DATABASE_PASSWORD
    API_TOKEN:
      gitlab_secrets_manager:
        name: GROUP_TOKEN
      file: false
  script:
    - echo secrets
"""
    write = await client.post(
        f"{API}/projects/{project_id}/repository/files/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "commit_message": "add secrets ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "encoding": "base64",
            "branch": "main",
        },
    )
    assert write.status_code == 201

    group_secret = await client.post(
        f"{API}/groups/{group.json()['id']}/secrets",
        headers=auth_headers(test_token),
        json={"name": "GROUP_TOKEN", "value": "group-secret"},
    )
    assert group_secret.status_code == 201
    project_secret = await client.post(
        f"{API}/projects/{project_id}/secrets",
        headers=auth_headers(test_token),
        json={"name": "DATABASE_PASSWORD", "value": "project-secret"},
    )
    assert project_secret.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project_id}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()
    jobs = await client.get(
        f"{API}/projects/{project_id}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs.status_code == 200
    job_payload = jobs.json()[0]
    secret_metadata = {item["key"]: item for item in job_payload["secret_metadata"]}
    assert secret_metadata["DB_PASSWORD"] == {
        "key": "DB_PASSWORD",
        "name": "DATABASE_PASSWORD",
        "mode": "file",
        "file": True,
        "scope_type": "project",
        "scope_id": project_id,
        "environment_scope": "*",
        "branch_scope": "*",
        "protected": False,
    }
    assert secret_metadata["API_TOKEN"] == {
        "key": "API_TOKEN",
        "name": "GROUP_TOKEN",
        "mode": "env",
        "file": False,
        "scope_type": "group",
        "scope_id": group.json()["id"],
        "environment_scope": "*",
        "branch_scope": "*",
        "protected": False,
    }
    serialized_job = json.dumps(job_payload)
    assert "project-secret" not in serialized_job
    assert "group-secret" not in serialized_job

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    variables = {item["key"]: item for item in request.json()["variables"]}
    assert variables["DB_PASSWORD"] == {
        "key": "DB_PASSWORD",
        "value": "project-secret",
        "public": False,
        "file": True,
        "masked": True,
        "raw": True,
    }
    assert variables["API_TOKEN"] == {
        "key": "API_TOKEN",
        "value": "group-secret",
        "public": False,
        "file": False,
        "masked": True,
        "raw": True,
    }

    access_events = (
        (
            await db_session.execute(
                select(CiSecretAccessEvent).where(
                    CiSecretAccessEvent.pipeline_id == pipeline["id"]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(access_events) == 2
    assert {event.environment for event in access_events} == {"production"}
    assert {event.job_id for event in access_events} == {request.json()["id"]}


async def test_job_secret_must_exist_and_be_eligible(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
secret_probe:
  secrets:
    DB_PASSWORD:
      gitlab_secrets_manager:
        name: DATABASE_PASSWORD
  script:
    - echo missing
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add missing secret ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    missing = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert missing.status_code == 400
    assert "DATABASE_PASSWORD" in missing.text

    secret = await client.post(
        f"{API}/projects/{project['id']}/secrets",
        headers=auth_headers(test_token),
        json={
            "name": "DATABASE_PASSWORD",
            "value": "protected-secret",
            "protected": True,
        },
    )
    assert secret.status_code == 201

    unprotected = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert unprotected.status_code == 400

    protect = await client.post(
        f"{API}/projects/{project['id']}/protected_branches",
        headers=auth_headers(test_token),
        json={"name": "main"},
    )
    assert protect.status_code == 201

    protected = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert protected.status_code == 201


async def test_instance_group_and_project_variable_precedence(
    client,
    test_token,
    db_session,
):
    parent = await client.post(
        f"{API}/groups",
        json={"path": "ci-parent", "name": "CI Parent"},
        headers=auth_headers(test_token),
    )
    assert parent.status_code == 201
    child = await client.post(
        f"{API}/groups",
        json={
            "path": "child",
            "name": "CI Child",
            "parent_id": parent.json()["id"],
        },
        headers=auth_headers(test_token),
    )
    assert child.status_code == 201
    project = await client.post(
        f"{API}/projects",
        json={
            "name": "scoped-ci",
            "namespace_path": "ci-parent/child",
            "initialize_with_readme": True,
        },
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    ci_yaml = """
precedence_probe:
  script:
    - echo precedence
"""
    write = await client.post(
        f"{API}/projects/{project.json()['id']}/repository/files/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "commit_message": "add precedence ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "encoding": "base64",
            "branch": "main",
        },
    )
    assert write.status_code == 201

    groups = (
        (
            await db_session.execute(
                select(Group).where(Group.login.in_(["ci-parent", "ci-parent/child"]))
            )
        )
        .scalars()
        .all()
    )
    group_ids = {group.login: group.id for group in groups}
    db_session.add_all(
        [
            CiVariable(
                scope_type="instance",
                scope_id=None,
                key="ORDERED",
                value="instance",
            ),
            CiVariable(
                scope_type="instance",
                scope_id=None,
                key="INSTANCE_ONLY",
                value="instance",
            ),
            CiVariable(
                scope_type="group",
                scope_id=group_ids["ci-parent"],
                key="ORDERED",
                value="parent",
            ),
            CiVariable(
                scope_type="group",
                scope_id=group_ids["ci-parent"],
                key="PARENT_ONLY",
                value="parent",
            ),
            CiVariable(
                scope_type="group",
                scope_id=group_ids["ci-parent/child"],
                key="ORDERED",
                value="child",
            ),
            CiVariable(
                scope_type="group",
                scope_id=group_ids["ci-parent/child"],
                key="CHILD_ONLY",
                value="child",
            ),
            CiVariable(
                scope_type="project",
                scope_id=project.json()["id"],
                key="ORDERED",
                value="project",
            ),
            CiVariable(
                scope_type="project",
                scope_id=project.json()["id"],
                key="PROJECT_ONLY",
                value="project",
            ),
        ]
    )
    await db_session.commit()

    pipeline_resp = await client.post(
        f"{API}/projects/{project.json()['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201, pipeline_resp.text

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    variables = {item["key"]: item["value"] for item in request.json()["variables"]}

    assert variables["ORDERED"] == "project"
    assert variables["INSTANCE_ONLY"] == "instance"
    assert variables["PARENT_ONLY"] == "parent"
    assert variables["CHILD_ONLY"] == "child"
    assert variables["PROJECT_ONLY"] == "project"


async def test_variable_metadata_reaches_runner_payload(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
variables:
  YAML_RAW:
    value: "$PIPELINE_FILE-literal"
    expand: false
  YAML_FILE:
    value: yaml-file-content
    variable_type: file
  YAML_MASKED:
    value: yaml-hidden
    masked: true

metadata_probe:
  variables:
    JOB_FILE:
      value: job-file-content
      file: true
  script:
    - echo metadata
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci variable metadata",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        headers=auth_headers(test_token),
        json={
            "ref": "main",
            "variables": [
                {
                    "key": "PIPELINE_FILE",
                    "value": "pipeline-file-content",
                    "variable_type": "file",
                },
                {
                    "key": "PIPELINE_RAW",
                    "value": "$YAML_FILE-literal",
                    "raw": True,
                },
            ],
        },
    )
    assert pipeline_resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    variables = {item["key"]: item for item in request.json()["variables"]}
    assert variables["PIPELINE_FILE"]["file"] is True
    assert variables["PIPELINE_FILE"]["value"] == "pipeline-file-content"
    assert variables["PIPELINE_RAW"]["raw"] is True
    assert variables["YAML_RAW"]["raw"] is True
    assert variables["YAML_RAW"]["value"] == "$PIPELINE_FILE-literal"
    assert variables["YAML_FILE"]["file"] is True
    assert variables["YAML_MASKED"]["masked"] is True
    assert variables["YAML_MASKED"]["public"] is False
    assert variables["JOB_FILE"]["file"] is True


async def test_project_variable_metadata_reaches_runner_payload(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
metadata_probe:
  script:
    - echo project metadata
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add project variable metadata ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    file_var = await client.post(
        f"{API}/projects/{project['id']}/variables",
        headers=auth_headers(test_token),
        json={
            "key": "PROJECT_FILE",
            "value": "project-file-content",
            "variable_type": "file",
        },
    )
    assert file_var.status_code == 201
    masked_var = await client.post(
        f"{API}/projects/{project['id']}/variables",
        headers=auth_headers(test_token),
        json={"key": "PROJECT_MASKED", "value": "hidden", "masked": True},
    )
    assert masked_var.status_code == 201
    raw_var = await client.post(
        f"{API}/projects/{project['id']}/variables",
        headers=auth_headers(test_token),
        json={"key": "PROJECT_RAW", "value": "$PROJECT_FILE", "raw": True},
    )
    assert raw_var.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    variables = {item["key"]: item for item in request.json()["variables"]}

    assert variables["PROJECT_FILE"]["file"] is True
    assert variables["PROJECT_FILE"]["value"] == "project-file-content"
    assert variables["PROJECT_MASKED"]["masked"] is True
    assert variables["PROJECT_MASKED"]["public"] is False
    assert variables["PROJECT_RAW"]["raw"] is True


async def test_masked_project_variables_are_redacted_from_trace(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
redaction_probe:
  script:
    - echo project trace redaction
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add project variable redaction ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    masked_var = await client.post(
        f"{API}/projects/{project['id']}/variables",
        headers=auth_headers(test_token),
        json={
            "key": "PROJECT_MASKED",
            "value": "super-secret-token",
            "masked": True,
        },
    )
    assert masked_var.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    payload = request.json()

    trace = await client.patch(
        f"{API}/jobs/{payload['id']}/trace?debug_trace=false",
        headers={"JOB-TOKEN": payload["token"], "Content-Range": "0-36"},
        content=b"before super-secret-token after",
    )
    assert trace.status_code == 202

    raw_trace = await client.get(
        f"{API}/projects/{project['id']}/jobs/{payload['id']}/trace"
    )
    assert raw_trace.status_code == 200
    assert raw_trace.text == "before [MASKED] after"
    assert "super-secret-token" not in raw_trace.text


async def test_job_secrets_are_redacted_from_trace(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
secret_redaction:
  secrets:
    DB_PASSWORD:
      gitlab_secrets_manager:
        name: DATABASE_PASSWORD
  script:
    - echo secret redaction
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add secret redaction ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    secret = await client.post(
        f"{API}/projects/{project['id']}/secrets",
        headers=auth_headers(test_token),
        json={"name": "DATABASE_PASSWORD", "value": "database-secret-value"},
    )
    assert secret.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    payload = request.json()

    trace = await client.patch(
        f"{API}/jobs/{payload['id']}/trace?debug_trace=false",
        headers={"JOB-TOKEN": payload["token"], "Content-Range": "0-34"},
        content=b"before database-secret-value after",
    )
    assert trace.status_code == 202

    raw_trace = await client.get(
        f"{API}/projects/{project['id']}/jobs/{payload['id']}/trace"
    )
    assert raw_trace.status_code == 200
    assert raw_trace.text == "before [MASKED] after"
    assert "database-secret-value" not in raw_trace.text


async def test_pipeline_trigger_creates_trigger_source_pipeline(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
trigger_probe:
  image: alpine:3.20
  rules:
    - if: '$CI_PIPELINE_SOURCE == "trigger"'
  script:
    - echo trigger $TRIGGER_VAR
api_only:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "api"'
  script:
    - echo api
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add trigger ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    trigger_resp = await client.post(
        f"{API}/projects/{project['id']}/triggers",
        json={"description": "external system"},
        headers=auth_headers(test_token),
    )
    assert trigger_resp.status_code == 201
    trigger = trigger_resp.json()
    assert trigger["description"] == "external system"
    assert trigger["token"].startswith("glptt-")

    list_resp = await client.get(
        f"{API}/projects/{project['id']}/triggers",
        headers=auth_headers(test_token),
    )
    assert list_resp.status_code == 200
    assert [item["id"] for item in list_resp.json()] == [trigger["id"]]

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/trigger/pipeline",
        data={
            "token": trigger["token"],
            "ref": "main",
            "variables[TRIGGER_VAR]": "from-trigger",
        },
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()
    assert pipeline["source"] == "trigger"
    assert pipeline["ref"] == "main"

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={
            "token": RUNNER_TOKEN,
            "info": {"name": "test-runner", "executor": "docker"},
        },
    )
    assert request.status_code == 201
    payload = request.json()
    assert payload["job_info"]["pipeline_id"] == pipeline["id"]
    assert payload["job_info"]["name"] == "trigger_probe"
    variables = {item["key"]: item["value"] for item in payload["variables"]}
    assert variables["TRIGGER_VAR"] == "from-trigger"

    delete_resp = await client.delete(
        f"{API}/projects/{project['id']}/triggers/{trigger['id']}",
        headers=auth_headers(test_token),
    )
    assert delete_resp.status_code == 204


async def test_pipeline_schedule_crud_and_play_creates_schedule_source_pipeline(
    client,
    test_token,
):
    project = await _create_project(client, test_token)
    ci_yaml = """
scheduled_probe:
  image: alpine:3.20
  rules:
    - if: '$CI_PIPELINE_SOURCE == "schedule"'
  script:
    - echo schedule $SCHEDULE_VAR
api_only:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "api"'
  script:
    - echo api
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add schedule ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    create_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline_schedules",
        json={
            "description": "nightly",
            "ref": "main",
            "cron": "0 3 * * *",
            "cron_timezone": "UTC",
            "active": True,
            "variables": [{"key": "SCHEDULE_VAR", "value": "from-schedule"}],
        },
        headers=auth_headers(test_token),
    )
    assert create_resp.status_code == 201
    schedule = create_resp.json()
    assert schedule["description"] == "nightly"
    assert schedule["variables"][0]["key"] == "SCHEDULE_VAR"
    assert schedule["next_run_at"] is not None

    update_resp = await client.put(
        f"{API}/projects/{project['id']}/pipeline_schedules/{schedule['id']}",
        json={"description": "nightly updated", "active": False},
        headers=auth_headers(test_token),
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["description"] == "nightly updated"
    assert update_resp.json()["active"] is False
    assert update_resp.json()["next_run_at"] is None

    play_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline_schedules/{schedule['id']}/play",
        headers=auth_headers(test_token),
    )
    assert play_resp.status_code == 201
    pipeline = play_resp.json()
    assert pipeline["source"] == "schedule"

    get_resp = await client.get(
        f"{API}/projects/{project['id']}/pipeline_schedules/{schedule['id']}",
        headers=auth_headers(test_token),
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["last_pipeline"]["id"] == pipeline["id"]

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={
            "token": RUNNER_TOKEN,
            "info": {"name": "test-runner", "executor": "docker"},
        },
    )
    assert request.status_code == 201
    payload = request.json()
    assert payload["job_info"]["pipeline_id"] == pipeline["id"]
    assert payload["job_info"]["name"] == "scheduled_probe"
    variables = {item["key"]: item["value"] for item in payload["variables"]}
    assert variables["SCHEDULE_VAR"] == "from-schedule"

    list_resp = await client.get(
        f"{API}/projects/{project['id']}/pipeline_schedules",
        headers=auth_headers(test_token),
    )
    assert list_resp.status_code == 200
    assert [item["id"] for item in list_resp.json()] == [schedule["id"]]

    delete_resp = await client.delete(
        f"{API}/projects/{project['id']}/pipeline_schedules/{schedule['id']}",
        headers=auth_headers(test_token),
    )
    assert delete_resp.status_code == 204


async def test_due_pipeline_schedule_materializes_pipeline_once(
    client,
    test_token,
    db_session,
):
    project = await _create_project(client, test_token)
    ci_yaml = """
scheduled_probe:
  image: alpine:3.20
  rules:
    - if: '$CI_PIPELINE_SOURCE == "schedule"'
  script:
    - echo scheduled
api_only:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "api"'
  script:
    - echo api
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add schedule ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    active_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline_schedules",
        json={
            "description": "due active",
            "ref": "main",
            "cron": "*/5 * * * *",
            "cron_timezone": "UTC",
            "active": True,
            "variables": [{"key": "SCHEDULE_VAR", "value": "auto"}],
        },
        headers=auth_headers(test_token),
    )
    assert active_resp.status_code == 201
    inactive_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline_schedules",
        json={
            "description": "due inactive",
            "ref": "main",
            "cron": "*/5 * * * *",
            "cron_timezone": "UTC",
            "active": False,
        },
        headers=auth_headers(test_token),
    )
    assert inactive_resp.status_code == 201

    due_at = datetime(2026, 6, 28, 8, 0)
    future_at = datetime(2026, 6, 28, 8, 5)
    active_schedule = (
        await db_session.execute(
            select(PipelineSchedule).where(
                PipelineSchedule.id == active_resp.json()["id"]
            )
        )
    ).scalar_one()
    inactive_schedule = (
        await db_session.execute(
            select(PipelineSchedule).where(
                PipelineSchedule.id == inactive_resp.json()["id"]
            )
        )
    ).scalar_one()
    active_schedule.next_run_at = due_at
    inactive_schedule.next_run_at = due_at
    await db_session.commit()

    stats = await run_due_pipeline_schedules(
        db_session,
        now=datetime(2026, 6, 28, 8, 0, 30, tzinfo=timezone.utc),
    )
    assert stats.checked == 1
    assert stats.created == 1
    assert stats.failed == 0

    pipelines = (
        await db_session.execute(
            select(Pipeline).where(Pipeline.project_id == project["id"])
        )
    ).scalars().all()
    assert len(pipelines) == 1
    assert pipelines[0].source == "schedule"

    await db_session.refresh(active_schedule)
    await db_session.refresh(inactive_schedule)
    assert active_schedule.last_pipeline_id == pipelines[0].id
    assert active_schedule.next_run_at == future_at
    assert inactive_schedule.last_pipeline_id is None

    repeat_stats = await run_due_pipeline_schedules(
        db_session,
        now=datetime(2026, 6, 28, 8, 1, tzinfo=timezone.utc),
    )
    assert repeat_stats.checked == 0
    assert repeat_stats.created == 0


async def test_create_pipeline_from_gitlab_ci_yaml_with_extends(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages: [build]

.base:
  image: python:3.12-alpine
  stage: build
  variables:
    BASE: one
  before_script:
    - echo before
  script:
    - echo inherited
  tags:
    - docker
  artifacts:
    paths:
      - inherited.txt

compile:
  extends: .base
  variables:
    LOCAL: two
  script:
    - echo compile
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci extends",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN, "info": {"config": {"tag_list": ["docker"]}}},
    )
    assert request.status_code == 201
    payload = request.json()
    assert payload["job_info"]["name"] == "compile"
    assert payload["image"]["name"] == "python:3.12-alpine"
    assert payload["steps"][0]["script"] == ["echo before", "echo compile"]
    assert payload["artifacts"][0]["paths"] == ["inherited.txt"]
    assert {
        "key": "BASE",
        "value": "one",
        "public": True,
        "file": False,
        "masked": False,
        "raw": False,
    } in payload["variables"]
    assert {
        "key": "LOCAL",
        "value": "two",
        "public": True,
        "file": False,
        "masked": False,
        "raw": False,
    } in payload["variables"]


async def test_extends_default_and_inherit_reach_runner_payload(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages: [test]

variables:
  GLOBAL_KEEP: keep
  GLOBAL_DROP: drop

default:
  image: python:3.12-alpine
  before_script:
    - echo default-before
  tags:
    - docker
  cache:
    key: default-cache
    paths:
      - vendor/

.base:
  variables:
    BASE: one

compile:
  extends: .base
  inherit:
    variables:
      - GLOBAL_KEEP
  variables:
    LOCAL: two
  script:
    - echo compile
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci extends default",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN, "info": {"config": {"tag_list": ["docker"]}}},
    )
    assert request.status_code == 201
    payload = request.json()
    variables = {item["key"]: item["value"] for item in payload["variables"]}
    assert payload["image"]["name"] == "python:3.12-alpine"
    assert payload["steps"][0]["script"] == ["echo default-before", "echo compile"]
    assert payload["cache"][0]["key"] == "default-cache"
    assert variables["GLOBAL_KEEP"] == "keep"
    assert "GLOBAL_DROP" not in variables
    assert variables["BASE"] == "one"
    assert variables["LOCAL"] == "two"


async def test_create_pipeline_from_gitlab_ci_yaml_with_local_include(
    client, test_token
):
    project = await _create_project(client, test_token)
    include_yaml = """
.base:
  image: python:3.12-alpine
  before_script:
    - echo included before
  variables:
    INCLUDED: included
  script:
    - echo inherited

included_job:
  stage: build
  script:
    - echo included
"""
    include_write = await client.post(
        f"{API}/projects/{project['id']}/repository/files/.gitlab%2Fci%2Fbuild.yml",
        headers=auth_headers(test_token),
        json={
            "branch": "main",
            "commit_message": "add include",
            "content": include_yaml,
        },
    )
    assert include_write.status_code == 201

    ci_yaml = """
include:
  - local: .gitlab/ci/build.yml
stages: [build, test]

root_job:
  stage: test
  extends: .base
  script:
    - echo root
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci include",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    pipeline = resp.json()

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs.status_code == 200
    assert [job["name"] for job in jobs.json()] == ["included_job", "root_job"]

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    assert request.json()["job_info"]["name"] == "included_job"

    finish = await client.put(
        f"{API}/jobs/{request.json()['id']}",
        headers={"JOB-TOKEN": request.json()["token"]},
        json={"token": request.json()["token"], "state": "success", "exit_code": 0},
    )
    assert finish.status_code == 200

    second = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert second.status_code == 201
    payload = second.json()
    assert payload["job_info"]["name"] == "root_job"
    assert payload["image"]["name"] == "python:3.12-alpine"
    assert payload["steps"][0]["script"] == ["echo included before", "echo root"]
    assert {
        "key": "INCLUDED",
        "value": "included",
        "public": True,
        "file": False,
        "masked": False,
        "raw": False,
    } in payload["variables"]


async def test_gitlab_ci_local_include_root_config_wins(client, test_token):
    project = await _create_project(client, test_token)
    include_yaml = """
stages: [build]
image: alpine:3.19
conflict:
  stage: build
  script:
    - echo included
"""
    include_write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/shared.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add shared ci",
            "content": base64.b64encode(include_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert include_write.status_code == 201

    ci_yaml = """
include:
  local: shared.yml
stages: [test]
image: alpine:3.20
conflict:
  stage: test
  script:
    - echo root
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci include root override",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    payload = request.json()
    assert payload["job_info"]["name"] == "conflict"
    assert payload["job_info"]["stage"] == "test"
    assert payload["image"]["name"] == "alpine:3.20"
    assert payload["steps"][0]["script"] == ["echo root"]


async def test_gitlab_ci_supports_nested_local_includes(client, test_token):
    project = await _create_project(client, test_token)
    base_yaml = """
.base:
  image: python:3.12-alpine
  variables:
    NESTED: nested
  before_script:
    - echo nested
"""
    base_write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/base.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add base include",
            "content": base64.b64encode(base_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert base_write.status_code == 201

    child_yaml = """
include:
  local: base.yml

nested_job:
  extends: .base
  script:
    - echo nested job
"""
    child_write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/child.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add child include",
            "content": base64.b64encode(child_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert child_write.status_code == 201

    ci_yaml = """
include:
  local: child.yml
"""
    root_write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add nested ci include",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert root_write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    payload = request.json()
    assert payload["job_info"]["name"] == "nested_job"
    assert payload["image"]["name"] == "python:3.12-alpine"
    assert payload["steps"][0]["script"] == ["echo nested", "echo nested job"]
    assert {
        "key": "NESTED",
        "value": "nested",
        "public": True,
        "file": False,
        "masked": False,
        "raw": False,
    } in payload["variables"]


async def test_gitlab_ci_supports_project_includes(client, test_token):
    project = await _create_project(client, test_token)
    template_resp = await client.post(
        f"{API}/projects",
        json={"name": "ci-template", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert template_resp.status_code == 201
    template = template_resp.json()

    template_yaml = """
.template:
  image: python:3.12-alpine
  variables:
    FROM_PROJECT: template
  before_script:
    - echo project include

template_job:
  script:
    - echo from template
"""
    template_write = await client.post(
        f"{API}/projects/{template['id']}/repository/files/templates%2Fpython.yml",
        headers=auth_headers(test_token),
        json={
            "branch": "main",
            "commit_message": "add project include template",
            "content": template_yaml,
        },
    )
    assert template_write.status_code == 201

    ci_yaml = """
include:
  project: testuser/ci-template
  ref: main
  file: templates/python.yml

root_job:
  extends: .template
  script:
    - echo root project include
"""
    root_write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add project ci include",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert root_write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201
    pipeline = resp.json()

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs.status_code == 200
    assert [job["name"] for job in jobs.json()] == ["root_job", "template_job"]

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    payload = request.json()
    assert payload["job_info"]["name"] == "root_job"
    assert payload["image"]["name"] == "python:3.12-alpine"
    assert payload["steps"][0]["script"] == [
        "echo project include",
        "echo root project include",
    ]
    assert {
        "key": "FROM_PROJECT",
        "value": "template",
        "public": True,
        "file": False,
        "masked": False,
        "raw": False,
    } in payload["variables"]


async def test_gitlab_ci_supports_remote_includes(client, test_token, monkeypatch):
    from app.api import pipelines

    async def fake_remote_include(url: str) -> str:
        assert url == "http://localhost/ci/remote.yml"
        return """
.remote:
  image: python:3.12-alpine
  variables:
    FROM_REMOTE: remote
  before_script:
    - echo remote before
"""

    monkeypatch.setattr(pipelines, "_fetch_remote_include", fake_remote_include)
    project = await _create_project(client, test_token)
    ci_yaml = """
include:
  remote: http://localhost/ci/remote.yml

remote_job:
  extends: .remote
  script:
    - echo remote job
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add remote include",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    payload = request.json()
    assert payload["job_info"]["name"] == "remote_job"
    assert payload["image"]["name"] == "python:3.12-alpine"
    assert payload["steps"][0]["script"] == ["echo remote before", "echo remote job"]
    assert {
        "key": "FROM_REMOTE",
        "value": "remote",
        "public": True,
        "file": False,
        "masked": False,
        "raw": False,
    } in payload["variables"]


async def test_gitlab_ci_supports_list_valued_remote_includes(
    client, test_token, monkeypatch
):
    from app.api import pipelines

    async def fake_remote_include(url: str) -> str:
        includes = {
            "http://localhost/ci/base.yml": """
.remote-base:
  image: python:3.12-alpine
  before_script:
    - echo remote base
""",
            "http://localhost/ci/vars.yml": """
.remote-vars:
  variables:
    FROM_REMOTE_LIST: remote-list
""",
        }
        return includes[url]

    monkeypatch.setattr(pipelines, "_fetch_remote_include", fake_remote_include)
    project = await _create_project(client, test_token)
    ci_yaml = """
include:
  remote:
    - http://localhost/ci/base.yml
    - http://localhost/ci/vars.yml

remote_list_job:
  extends:
    - .remote-base
    - .remote-vars
  script:
    - echo remote list
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add remote list include",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    payload = request.json()
    assert payload["job_info"]["name"] == "remote_list_job"
    assert payload["image"]["name"] == "python:3.12-alpine"
    assert payload["steps"][0]["script"] == ["echo remote base", "echo remote list"]
    assert {
        "key": "FROM_REMOTE_LIST",
        "value": "remote-list",
        "public": True,
        "file": False,
        "masked": False,
        "raw": False,
    } in payload["variables"]


async def test_gitlab_ci_rejects_disallowed_remote_include_host(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
include:
  remote: https://example.com/ci.yml

job:
  script:
    - echo test
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add disallowed remote include",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 400
    assert "remote include host is not allowed" in resp.text


async def test_gitlab_ci_supports_template_includes(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
include:
  template: Bash.gitlab-ci.yml

template_job:
  extends: .bash-template
  script:
    - echo template job
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add template include",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    payload = request.json()
    assert payload["job_info"]["name"] == "template_job"
    assert payload["steps"][0]["script"] == [
        "echo bash template before",
        "echo template job",
    ]


async def test_gitlab_ci_supports_list_valued_template_includes(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
include:
  template:
    - Bash.gitlab-ci.yml
    - Jobs/Build.gitlab-ci.yml

template_list_job:
  extends:
    - .bash-template
    - .build-template
  script:
    - echo template list
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add template list include",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 201

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    payload = request.json()
    assert payload["job_info"]["name"] == "template_list_job"
    assert payload["job_info"]["stage"] == "build"
    assert payload["image"]["name"] == "alpine:3.20"
    assert payload["steps"][0]["script"] == [
        "echo bash template before",
        "echo template list",
    ]


async def test_raw_file_endpoint_serves_repository_content(client, test_token):
    project = await _create_project(client, test_token)
    content = "remote template content\n"
    write = await client.post(
        f"{API}/projects/{project['id']}/repository/files/templates%2Fremote.yml",
        headers=auth_headers(test_token),
        json={
            "branch": "main",
            "commit_message": "add remote raw file",
            "content": content,
        },
    )
    assert write.status_code == 201

    resp = await client.get("/ui/testuser/ci-repo/raw/main/templates/remote.yml")
    assert resp.status_code == 200
    assert resp.text == content


async def test_gitlab_ci_rejects_circular_local_includes(client, test_token):
    project = await _create_project(client, test_token)
    first = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/first.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add first include",
            "content": base64.b64encode(b"include:\n  local: second.yml\n").decode(),
            "branch": "main",
        },
    )
    assert first.status_code == 201
    second = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/second.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add second include",
            "content": base64.b64encode(b"include:\n  local: first.yml\n").decode(),
            "branch": "main",
        },
    )
    assert second.status_code == 201
    root = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add circular ci include",
            "content": base64.b64encode(b"include:\n  local: first.yml\n").decode(),
            "branch": "main",
        },
    )
    assert root.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 400
    assert "Circular CI include detected: first.yml" in resp.text


async def test_missing_gitlab_ci_local_include_rejects_pipeline(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
include:
  - local: missing.yml

job:
  script:
    - echo job
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add missing include",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert resp.status_code == 400
    assert "CI include not found: missing.yml" in resp.text


async def test_runner_respects_stage_gating(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages:
  - build
  - test

compile:
  stage: build
  script:
    - echo build

unit:
  stage: test
  script:
    - echo test
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201

    first = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert first.status_code == 201
    first_payload = first.json()
    assert first_payload["job_info"]["name"] == "compile"

    blocked = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert blocked.status_code == 204

    finish = await client.put(
        f"{API}/jobs/{first_payload['id']}",
        headers={"JOB-TOKEN": first_payload["token"]},
        json={"token": first_payload["token"], "state": "success", "exit_code": 0},
    )
    assert finish.status_code == 200

    second = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert second.status_code == 201
    assert second.json()["job_info"]["name"] == "unit"


async def test_runner_allows_same_stage_jobs_before_stage_completes(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages:
  - build

compile_a:
  stage: build
  script:
    - echo a

compile_b:
  stage: build
  script:
    - echo b
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201

    first = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    second = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert {first.json()["job_info"]["name"], second.json()["job_info"]["name"]} == {
        "compile_a",
        "compile_b",
    }


async def test_failed_stage_skips_later_pending_jobs(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages:
  - build
  - test

compile:
  stage: build
  script:
    - exit 1

unit:
  stage: test
  script:
    - echo test
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    pipeline = pipeline_resp.json()

    first = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    first_payload = first.json()

    fail = await client.put(
        f"{API}/jobs/{first_payload['id']}",
        headers={"JOB-TOKEN": first_payload["token"]},
        json={"token": first_payload["token"], "state": "failed", "exit_code": 1},
    )
    assert fail.status_code == 200

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs.status_code == 200
    statuses = {job["name"]: job["status"] for job in jobs.json()}
    assert statuses == {"compile": "failed", "unit": "skipped"}

    blocked = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert blocked.status_code == 204


async def test_when_always_job_runs_after_failed_stage(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages:
  - build
  - cleanup
  - test

compile:
  stage: build
  script:
    - exit 1

cleanup:
  stage: cleanup
  when: always
  script:
    - echo cleanup

unit:
  stage: test
  script:
    - echo test
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add when always ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    pipeline = pipeline_resp.json()

    first = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    first_payload = first.json()
    assert first_payload["job_info"]["name"] == "compile"

    fail = await client.put(
        f"{API}/jobs/{first_payload['id']}",
        headers={"JOB-TOKEN": first_payload["token"]},
        json={"token": first_payload["token"], "state": "failed", "exit_code": 1},
    )
    assert fail.status_code == 200

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs.status_code == 200
    by_name = {job["name"]: job for job in jobs.json()}
    assert by_name["compile"]["status"] == "failed"
    assert by_name["cleanup"]["status"] == "pending"
    assert by_name["cleanup"]["when"] == "always"
    assert by_name["unit"]["status"] == "skipped"

    pipeline_after_failure = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}"
    )
    assert pipeline_after_failure.json()["status"] == "pending"

    cleanup = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert cleanup.status_code == 201
    cleanup_payload = cleanup.json()
    assert cleanup_payload["job_info"]["name"] == "cleanup"
    assert cleanup_payload["steps"][0]["when"] == "always"

    success = await client.put(
        f"{API}/jobs/{cleanup_payload['id']}",
        headers={"JOB-TOKEN": cleanup_payload["token"]},
        json={"token": cleanup_payload["token"], "state": "success"},
    )
    assert success.status_code == 200

    pipeline_done = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}"
    )
    assert pipeline_done.json()["status"] == "failed"


async def test_when_on_failure_job_is_skipped_after_success(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages:
  - build
  - cleanup

compile:
  stage: build
  script:
    - echo build

notify_failure:
  stage: cleanup
  when: on_failure
  script:
    - echo failed
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add when on failure ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    pipeline = pipeline_resp.json()

    first = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    first_payload = first.json()
    assert first_payload["job_info"]["name"] == "compile"

    success = await client.put(
        f"{API}/jobs/{first_payload['id']}",
        headers={"JOB-TOKEN": first_payload["token"]},
        json={"token": first_payload["token"], "state": "success"},
    )
    assert success.status_code == 200

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    by_name = {job["name"]: job for job in jobs.json()}
    assert by_name["compile"]["status"] == "success"
    assert by_name["notify_failure"]["status"] == "skipped"
    assert by_name["notify_failure"]["when"] == "on_failure"

    blocked = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert blocked.status_code == 204

    pipeline_done = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}"
    )
    assert pipeline_done.json()["status"] == "success"


async def test_when_on_failure_job_runs_after_failed_stage(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages:
  - build
  - cleanup
  - test

compile:
  stage: build
  script:
    - exit 1

notify_failure:
  stage: cleanup
  when: on_failure
  script:
    - echo failed

unit:
  stage: test
  script:
    - echo test
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add when on failure ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    pipeline = pipeline_resp.json()

    first = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    first_payload = first.json()
    assert first_payload["job_info"]["name"] == "compile"

    fail = await client.put(
        f"{API}/jobs/{first_payload['id']}",
        headers={"JOB-TOKEN": first_payload["token"]},
        json={"token": first_payload["token"], "state": "failed", "exit_code": 1},
    )
    assert fail.status_code == 200

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    by_name = {job["name"]: job for job in jobs.json()}
    assert by_name["compile"]["status"] == "failed"
    assert by_name["notify_failure"]["status"] == "pending"
    assert by_name["notify_failure"]["when"] == "on_failure"
    assert by_name["unit"]["status"] == "skipped"

    notify = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert notify.status_code == 201
    notify_payload = notify.json()
    assert notify_payload["job_info"]["name"] == "notify_failure"
    assert notify_payload["steps"][0]["when"] == "on_failure"

    success = await client.put(
        f"{API}/jobs/{notify_payload['id']}",
        headers={"JOB-TOKEN": notify_payload["token"]},
        json={"token": notify_payload["token"], "state": "success"},
    )
    assert success.status_code == 200

    pipeline_done = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}"
    )
    assert pipeline_done.json()["status"] == "failed"


async def test_allowed_failure_does_not_block_later_jobs(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages:
  - build
  - test

optional_compile:
  stage: build
  allow_failure: true
  script:
    - exit 1

unit:
  stage: test
  script:
    - echo test
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    first = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert first.status_code == 201
    first_payload = first.json()
    assert first_payload["job_info"]["name"] == "optional_compile"
    assert first_payload["steps"][0]["allow_failure"] is True

    fail = await client.put(
        f"{API}/jobs/{first_payload['id']}",
        headers={"JOB-TOKEN": first_payload["token"]},
        json={"token": first_payload["token"], "state": "failed", "exit_code": 1},
    )
    assert fail.status_code == 200

    second = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert second.status_code == 201
    second_payload = second.json()
    assert second_payload["job_info"]["name"] == "unit"
    assert second_payload["steps"][0]["allow_failure"] is False

    complete = await client.put(
        f"{API}/jobs/{second_payload['id']}",
        headers={"JOB-TOKEN": second_payload["token"]},
        json={"token": second_payload["token"], "state": "success", "exit_code": 0},
    )
    assert complete.status_code == 200

    pipeline_after = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}"
    )
    assert pipeline_after.status_code == 200
    assert pipeline_after.json()["status"] == "success"

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs.status_code == 200
    by_name = {job["name"]: job for job in jobs.json()}
    assert by_name["optional_compile"]["status"] == "failed"
    assert by_name["optional_compile"]["allow_failure"] is True
    assert by_name["unit"]["status"] == "success"


async def test_allow_failure_exit_codes_rejects_pipeline(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
optional_compile:
  allow_failure:
    exit_codes:
      - 137
  script:
    - exit 137
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add unsupported allow failure ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 400
    assert "allow_failure exit_codes is not supported" in pipeline_resp.text


async def test_runner_allows_needs_to_bypass_incomplete_same_stage_peer(
    client, test_token
):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages:
  - build
  - test

compile_a:
  stage: build
  script:
    - echo a

compile_b:
  stage: build
  script:
    - echo b

unit:
  stage: test
  needs:
    - compile_a
  script:
    - echo test
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    first = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    second = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assigned = {first.json()["job_info"]["name"], second.json()["job_info"]["name"]}
    assert assigned == {"compile_a", "compile_b"}

    compile_a = (
        first.json()
        if first.json()["job_info"]["name"] == "compile_a"
        else second.json()
    )
    finish = await client.put(
        f"{API}/jobs/{compile_a['id']}",
        headers={"JOB-TOKEN": compile_a["token"]},
        json={"token": compile_a["token"], "state": "success", "exit_code": 0},
    )
    assert finish.status_code == 200

    next_job = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert next_job.status_code == 201
    assert next_job.json()["job_info"]["name"] == "unit"

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    unit = next(job for job in jobs.json() if job["name"] == "unit")
    assert unit["needs"] == ["compile_a"]


async def test_optional_missing_needs_do_not_block_job(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages:
  - test

unit:
  stage: test
  needs:
    - job: missing_compile
      optional: true
  script:
    - echo test
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    assert request.json()["job_info"]["name"] == "unit"

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    unit = jobs.json()[0]
    assert unit["needs"] == ["missing_compile"]


async def test_missing_required_needs_reject_pipeline(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages:
  - test

unit:
  stage: test
  needs:
    - job: missing_compile
  script:
    - echo test
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 400
    assert "needs missing job" in pipeline_resp.text


async def test_duplicate_needs_reject_pipeline(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
compile:
  script:
    - echo build

unit:
  needs:
    - compile
    - job: compile
  script:
    - echo test
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 400
    assert "duplicate needs" in pipeline_resp.text


async def test_self_needs_reject_pipeline(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
unit:
  needs:
    - unit
  script:
    - echo test
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 400
    assert "cannot need itself" in pipeline_resp.text


async def test_future_stage_needs_reject_pipeline(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages:
  - build
  - test

early:
  stage: build
  needs:
    - late
  script:
    - echo early

late:
  stage: test
  script:
    - echo late
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 400
    assert "future-stage job" in pipeline_resp.text


async def test_needs_parallel_matrix_rejects_pipeline(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
compile:
  script:
    - echo compile

unit:
  needs:
    - job: compile
      parallel:
        matrix:
          - OS: linux
  script:
    - echo test
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add unsupported needs parallel ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 400
    assert "needs parallel matrix is not supported" in pipeline_resp.text


async def test_same_stage_needs_are_allowed(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages:
  - test

first:
  stage: test
  script:
    - echo first

second:
  stage: test
  needs:
    - first
  script:
    - echo second
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201


async def test_needs_artifacts_populates_runner_dependencies(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages:
  - build
  - test

compile:
  stage: build
  script:
    - mkdir -p out
    - echo artifact > out/result.txt
  artifacts:
    paths:
      - out/result.txt

consume_false:
  stage: test
  needs:
    - job: compile
      artifacts: false
  script:
    - echo no artifact dependency

consume_true:
  stage: test
  needs:
    - job: compile
      artifacts: true
  script:
    - echo artifact dependency
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201

    compile = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert compile.status_code == 201
    compile_payload = compile.json()
    assert compile_payload["job_info"]["name"] == "compile"

    archive = b"fake artifact zip"
    upload = await client.post(
        f"{API}/jobs/{compile_payload['id']}/artifacts?artifact_format=zip&artifact_type=archive",
        headers={
            "JOB-TOKEN": compile_payload["token"],
            "Content-Type": "application/zip",
        },
        content=archive,
    )
    assert upload.status_code == 201

    download = await client.get(
        f"{API}/jobs/{compile_payload['id']}/artifacts",
        headers={"JOB-TOKEN": compile_payload["token"]},
    )
    assert download.status_code == 200
    assert download.content == archive

    forbidden = await client.get(
        f"{API}/jobs/{compile_payload['id']}/artifacts",
        headers={"JOB-TOKEN": "wrong"},
    )
    assert forbidden.status_code == 403

    finish = await client.put(
        f"{API}/jobs/{compile_payload['id']}",
        headers={"JOB-TOKEN": compile_payload["token"]},
        json={"token": compile_payload["token"], "state": "success", "exit_code": 0},
    )
    assert finish.status_code == 200

    first_downstream = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    second_downstream = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert first_downstream.status_code == 201
    assert second_downstream.status_code == 201
    downstream = {
        first_downstream.json()["job_info"]["name"]: first_downstream.json(),
        second_downstream.json()["job_info"]["name"]: second_downstream.json(),
    }
    assert downstream["consume_false"]["dependencies"] == []
    assert downstream["consume_true"]["dependencies"] == [
        {
            "id": compile_payload["id"],
            "token": compile_payload["token"],
            "name": "compile",
            "artifacts_file": {
                "filename": f"job-{compile_payload['id']}-artifacts.zip",
                "size": len(archive),
            },
        }
    ]


async def test_needs_artifacts_dependencies_follow_needs_order(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages:
  - build
  - test

compile_a:
  stage: build
  script:
    - echo a
  artifacts:
    paths:
      - a.txt

compile_b:
  stage: build
  script:
    - echo b
  artifacts:
    paths:
      - b.txt

consume:
  stage: test
  needs:
    - job: compile_b
      artifacts: true
    - job: compile_a
      artifacts: true
  script:
    - echo consume
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201

    first = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    second = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    compile_jobs = {
        first.json()["job_info"]["name"]: first.json(),
        second.json()["job_info"]["name"]: second.json(),
    }

    for name, payload in compile_jobs.items():
        archive = f"artifact {name}".encode()
        upload = await client.post(
            f"{API}/jobs/{payload['id']}/artifacts?artifact_format=zip&artifact_type=archive",
            headers={"JOB-TOKEN": payload["token"], "Content-Type": "application/zip"},
            content=archive,
        )
        assert upload.status_code == 201
        finish = await client.put(
            f"{API}/jobs/{payload['id']}",
            headers={"JOB-TOKEN": payload["token"]},
            json={"token": payload["token"], "state": "success", "exit_code": 0},
        )
        assert finish.status_code == 200

    consume = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert consume.status_code == 201
    assert consume.json()["job_info"]["name"] == "consume"
    assert [item["name"] for item in consume.json()["dependencies"]] == [
        "compile_b",
        "compile_a",
    ]


async def test_dependencies_populate_runner_artifact_payload(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages:
  - build
  - test

compile_a:
  stage: build
  script:
    - echo a
  artifacts:
    paths:
      - a.txt

compile_b:
  stage: build
  script:
    - echo b
  artifacts:
    paths:
      - b.txt

consume:
  stage: test
  dependencies:
    - compile_b
  script:
    - echo consume
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add dependencies ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs",
        headers=auth_headers(test_token),
    )
    assert jobs.status_code == 200
    consume_job = next(job for job in jobs.json() if job["name"] == "consume")
    assert consume_job["dependencies"] == ["compile_b"]
    assert consume_job["needs"] == []

    first = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    second = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    compile_jobs = {
        first.json()["job_info"]["name"]: first.json(),
        second.json()["job_info"]["name"]: second.json(),
    }

    for name, payload in compile_jobs.items():
        archive = f"artifact {name}".encode()
        upload = await client.post(
            f"{API}/jobs/{payload['id']}/artifacts?artifact_format=zip&artifact_type=archive",
            headers={"JOB-TOKEN": payload["token"], "Content-Type": "application/zip"},
            content=archive,
        )
        assert upload.status_code == 201
        finish = await client.put(
            f"{API}/jobs/{payload['id']}",
            headers={"JOB-TOKEN": payload["token"]},
            json={"token": payload["token"], "state": "success", "exit_code": 0},
        )
        assert finish.status_code == 200

    consume = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert consume.status_code == 201
    dependency_names = [item["name"] for item in consume.json()["dependencies"]]
    assert dependency_names == ["compile_b"]


async def test_empty_dependencies_disable_default_artifact_payload(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages: [build, test]

compile:
  stage: build
  script:
    - echo build
  artifacts:
    paths:
      - build.txt

consume:
  stage: test
  dependencies: []
  script:
    - echo consume
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add empty dependencies ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201

    compile = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert compile.status_code == 201
    compile_payload = compile.json()
    upload = await client.post(
        f"{API}/jobs/{compile_payload['id']}/artifacts?artifact_format=zip&artifact_type=archive",
        headers={
            "JOB-TOKEN": compile_payload["token"],
            "Content-Type": "application/zip",
        },
        content=b"artifact",
    )
    assert upload.status_code == 201
    finish = await client.put(
        f"{API}/jobs/{compile_payload['id']}",
        headers={"JOB-TOKEN": compile_payload["token"]},
        json={"token": compile_payload["token"], "state": "success", "exit_code": 0},
    )
    assert finish.status_code == 200

    consume = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert consume.status_code == 201
    assert consume.json()["dependencies"] == []


async def test_default_artifact_dependencies_include_prior_stages(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
stages: [build, test]

compile:
  stage: build
  script:
    - echo build
  artifacts:
    paths:
      - build.txt

consume:
  stage: test
  script:
    - echo consume
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add default dependency ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201

    compile = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert compile.status_code == 201
    compile_payload = compile.json()
    upload = await client.post(
        f"{API}/jobs/{compile_payload['id']}/artifacts?artifact_format=zip&artifact_type=archive",
        headers={
            "JOB-TOKEN": compile_payload["token"],
            "Content-Type": "application/zip",
        },
        content=b"artifact",
    )
    assert upload.status_code == 201
    finish = await client.put(
        f"{API}/jobs/{compile_payload['id']}",
        headers={"JOB-TOKEN": compile_payload["token"]},
        json={"token": compile_payload["token"], "state": "success", "exit_code": 0},
    )
    assert finish.status_code == 200

    consume = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert consume.status_code == 201
    assert [item["name"] for item in consume.json()["dependencies"]] == ["compile"]


async def test_invalid_dependencies_reject_pipeline(client, test_token):
    project = await _create_project(client, test_token)
    for ci_yaml, expected in [
        (
            """
stages: [build, test]
consume:
  stage: test
  dependencies:
    - missing
  script: echo consume
""",
            "dependencies missing job",
        ),
        (
            """
stages: [build, test]
compile:
  stage: build
  dependencies:
    - consume
  script: echo compile
consume:
  stage: test
  script: echo consume
""",
            "dependencies must be from earlier stages",
        ),
    ]:
        write = await client.put(
            f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
            headers=auth_headers(test_token),
            json={
                "message": "add invalid dependencies ci",
                "content": base64.b64encode(ci_yaml.encode()).decode(),
                "branch": "main",
            },
        )
        assert write.status_code in {200, 201}
        pipeline_resp = await client.post(
            f"{API}/projects/{project['id']}/pipeline",
            json={"ref": "main"},
            headers=auth_headers(test_token),
        )
        assert pipeline_resp.status_code == 400
        assert expected in pipeline_resp.text


async def test_pipeline_ref_filters_jobs_from_gitlab_ci_yaml(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
main_only:
  script:
    - echo main
  only: [main]

skip_main:
  script:
    - echo skip
  except: [main]

fallback_rule:
  script:
    - echo fallback
  rules:
    - if: '$CI_COMMIT_REF_NAME == "main"'
      when: never
    - when: on_success
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs.status_code == 200
    assert [job["name"] for job in jobs.json()] == ["main_only"]


async def test_pipeline_legacy_ref_glob_filters_jobs_from_gitlab_ci_yaml(
    client, test_token
):
    project = await _create_project(client, test_token)
    ci_yaml = """
release_glob:
  script:
    - echo release glob
  only: ["release/*"]

feature_glob:
  script:
    - echo feature glob
  only: ["feature/*"]

skip_release_glob:
  script:
    - echo skip release glob
  except: ["release/*"]

skip_feature_glob:
  script:
    - echo skip feature glob
  except: ["feature/*"]
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add glob ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201

    branch = await client.post(
        f"{API}/projects/{project['id']}/repository/branches",
        headers=auth_headers(test_token),
        json={"branch": "release/1.0", "ref": "main"},
    )
    assert branch.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "release/1.0"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs.status_code == 200
    assert [job["name"] for job in jobs.json()] == [
        "release_glob",
        "skip_feature_glob",
    ]


async def test_pipeline_rules_if_exists_changes_and_manual_from_gitlab_ci_yaml(
    client, test_token
):
    project = await _create_project(client, test_token)
    ci_yaml = """
variables:
  DEPLOY_TARGET: prod
  MAIN_PATTERN: /^main$/

rules_if:
  script:
    - echo if
  rules:
    - if: '$DEPLOY_TARGET == "prod" && $CI_COMMIT_REF_NAME =~ /^main$/'

rules_regex_variable:
  script:
    - echo regex variable
  rules:
    - if: '$CI_COMMIT_REF_NAME =~ $MAIN_PATTERN'

rules_not:
  script:
    - echo not
  rules:
    - if: '!$SKIP_DEPLOY'

rules_exists:
  script:
    - echo exists
  rules:
    - exists:
        - src/*.py

rules_changes:
  script:
    - echo changes
  rules:
    - changes:
        - docs/**

manual_review:
  script:
    - echo manual
  rules:
    - when: manual

never_job:
  script:
    - echo never
  rules:
    - if: '$DEPLOY_TARGET == "prod"'
      when: never
"""
    write_ci = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write_ci.status_code == 201
    write_src = await client.post(
        f"{API}/projects/{project['id']}/repository/files/{quote('src/app.py', safe='')}",
        headers=auth_headers(test_token),
        json={
            "commit_message": "add src",
            "content": "print('hello')\n",
            "branch": "main",
        },
    )
    assert write_src.status_code == 201
    write_docs = await client.post(
        f"{API}/projects/{project['id']}/repository/files/{quote('docs/readme.md', safe='')}",
        headers=auth_headers(test_token),
        json={
            "commit_message": "add docs",
            "content": "# docs\n",
            "branch": "main",
        },
    )
    assert write_docs.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs.status_code == 200
    by_name = {job["name"]: job for job in jobs.json()}
    assert sorted(by_name) == [
        "manual_review",
        "rules_changes",
        "rules_exists",
        "rules_if",
        "rules_not",
        "rules_regex_variable",
    ]
    assert by_name["manual_review"]["status"] == "manual"
    assert by_name["rules_if"]["status"] == "pending"


async def test_manual_job_play_requeues_job_for_runner(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
manual_review:
  script:
    - echo manual
  rules:
    - when: manual
"""
    write_ci = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add manual ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write_ci.status_code == 201

    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs.status_code == 200
    job = jobs.json()[0]
    assert job["name"] == "manual_review"
    assert job["status"] == "manual"

    no_job = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert no_job.status_code == 204

    played = await client.post(
        f"{API}/projects/{project['id']}/jobs/{job['id']}/play",
        headers=auth_headers(test_token),
    )
    assert played.status_code == 200
    assert played.json()["status"] == "pending"

    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    assert request.json()["id"] == job["id"]
    assert request.json()["job_info"]["name"] == "manual_review"


async def test_non_manual_job_play_is_rejected(client, test_token):
    project = await _create_project(client, test_token)
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={
            "ref": "main",
            "job": {
                "name": "regular",
                "image": "alpine:3.20",
                "script": ["echo regular"],
            },
        },
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    assert jobs.status_code == 200
    job = jobs.json()[0]
    assert job["status"] == "pending"

    played = await client.post(
        f"{API}/projects/{project['id']}/jobs/{job['id']}/play",
        headers=auth_headers(test_token),
    )
    assert played.status_code == 400
    assert played.json()["message"] == "Job is not playable"


async def test_runner_matches_tagged_jobs_to_runner_tags(client, test_token):
    project = await _create_project(client, test_token)
    ci_yaml = """
tagged:
  script:
    - echo tagged
  tags:
    - docker
    - linux
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    mismatch = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={
            "token": RUNNER_TOKEN,
            "info": {"config": {"tag_list": ["docker"]}},
        },
    )
    assert mismatch.status_code == 204

    match = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={
            "token": RUNNER_TOKEN,
            "info": {"config": {"tag_list": ["docker", "linux", "vm"]}},
        },
    )
    assert match.status_code == 201
    assert match.json()["job_info"]["name"] == "tagged"

    jobs = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/jobs"
    )
    tagged = jobs.json()[0]
    assert tagged["tag_list"] == ["docker", "linux"]


async def test_runner_uses_persisted_registration_tags(client, test_token):
    register = await client.post(
        f"{API}/runners",
        headers={"RUNNER-TOKEN": "runner-registration-token"},
        json={
            "token": "runner-registration-token",
            "description": "tagged-runner",
            "tag_list": "docker,linux,vm",
            "run_untagged": False,
        },
    )
    assert register.status_code == 201

    project = await _create_project(client, test_token)
    ci_yaml = """
tagged:
  script:
    - echo tagged
  tags:
    - docker
    - linux
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201

    match = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert match.status_code == 201
    assert match.json()["job_info"]["name"] == "tagged"


async def test_pipeline_diagnostics_explain_scheduler_state(client, test_token):
    register = await client.post(
        f"{API}/runners",
        headers={"RUNNER-TOKEN": "runner-registration-token"},
        json={
            "token": "runner-registration-token",
            "description": "diagnostic-runner",
            "tag_list": "docker",
            "run_untagged": False,
        },
    )
    assert register.status_code == 201

    project = await _create_project(client, test_token)
    ci_yaml = """
stages: [build, test]
compile:
  stage: build
  script:
    - echo compile
test:
  stage: test
  script:
    - echo test
tagged:
  stage: build
  script:
    - echo tagged
  tags:
    - docker
    - linux
"""
    write = await client.put(
        f"{API}/repos/testuser/ci-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "add diagnostic ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201
    pipeline = pipeline_resp.json()

    diagnostics = await client.get(
        f"{API}/projects/{project['id']}/pipelines/{pipeline['id']}/diagnostics"
    )
    assert diagnostics.status_code == 200
    body = diagnostics.json()
    assert body["runner"]["description"] == "diagnostic-runner"
    by_name = {job["job_name"]: job for job in body["jobs"]}

    assert by_name["compile"]["blocked"] is True
    assert by_name["compile"]["blockers"][0]["type"] == "run_untagged"
    assert by_name["tagged"]["blocked"] is True
    assert by_name["tagged"]["blockers"][0]["type"] == "runner_tags"
    assert by_name["tagged"]["blockers"][0]["missing_tags"] == ["linux"]
    assert by_name["test"]["blocked"] is True
    assert any(blocker["type"] == "stage" for blocker in by_name["test"]["blockers"])


async def test_runner_can_decline_untagged_jobs(client, test_token):
    project = await _create_project(client, test_token)
    pipeline_resp = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={
            "ref": "main",
            "job": {
                "name": "untagged",
                "script": ["echo untagged"],
            },
        },
        headers=auth_headers(test_token),
    )
    assert pipeline_resp.status_code == 201

    blocked = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={
            "token": RUNNER_TOKEN,
            "info": {"config": {"run_untagged": False}},
        },
    )
    assert blocked.status_code == 204

    allowed = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={
            "token": RUNNER_TOKEN,
            "info": {"config": {"run_untagged": True}},
        },
    )
    assert allowed.status_code == 201
    assert allowed.json()["job_info"]["name"] == "untagged"
