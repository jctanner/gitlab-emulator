"""Runner trace upload protocol: PATCH /api/v4/jobs/:id/trace.

The official runner sends ``Content-Range: <start>-<end - 1>`` and keeps its
own sent offset. On 202 it advances to the end of the chunk; on 416 it moves
to the number after the dash in the ``Range`` response header
(gitlab-runner network/patch_response.go, NewOffset). GitLab answers
``Range: 0-<stored byte count>`` and writes each chunk at its start offset,
replacing what was stored from there (AppendBuildTraceService, Trace#append).
"""

import base64

from sqlalchemy import select

from tests.conftest import API, auth_headers

RUNNER_TOKEN = "glrt-emulator-runner-token"


async def _running_job(client, test_token, variables=None):
    project = (await client.post(
        f"{API}/user/repos",
        json={"name": "trace-repo", "auto_init": True},
        headers=auth_headers(test_token),
    )).json()
    ci_yaml = (
        "workflow:\n  rules:\n    - if: '$CI_PIPELINE_SOURCE == \"api\"'\n"
        "    - when: never\n\ntrace_probe:\n  script:\n    - echo hi\n"
    )
    write = await client.put(
        f"{API}/repos/testuser/trace-repo/contents/.gitlab-ci.yml",
        headers=auth_headers(test_token),
        json={
            "message": "ci",
            "content": base64.b64encode(ci_yaml.encode()).decode(),
            "branch": "main",
        },
    )
    assert write.status_code == 201
    for key, value in (variables or {}).items():
        created = await client.post(
            f"{API}/projects/{project['id']}/variables",
            headers=auth_headers(test_token),
            json={"key": key, "value": value, "masked": True},
        )
        assert created.status_code == 201
    pipeline = await client.post(
        f"{API}/projects/{project['id']}/pipeline",
        json={"ref": "main"},
        headers=auth_headers(test_token),
    )
    assert pipeline.status_code == 201
    request = await client.post(
        f"{API}/jobs/request",
        headers={"RUNNER-TOKEN": RUNNER_TOKEN},
        json={"token": RUNNER_TOKEN},
    )
    assert request.status_code == 201
    return project, request.json()


class _Runner:
    """Mirrors the runner's offset bookkeeping (network/trace.go)."""

    def __init__(self, client, job):
        self.client = client
        self.job = job
        self.output = b""
        self.sent = 0

    def write(self, data: bytes) -> None:
        self.output += data

    async def patch(self, start: int | None = None):
        start = self.sent if start is None else start
        chunk = self.output[start:]
        response = await self.client.patch(
            f"{API}/jobs/{self.job['id']}/trace?debug_trace=false",
            headers={
                "JOB-TOKEN": self.job["token"],
                "Content-Range": f"{start}-{start + len(chunk) - 1}",
            },
            content=chunk,
        )
        if response.status_code == 202:
            self.sent = start + len(chunk)
        elif response.status_code == 416:
            self.sent = int(response.headers["Range"].split("-", 1)[1])
        return response


async def _trace_text(client, project, job):
    response = await client.get(
        f"{API}/projects/{project['id']}/jobs/{job['id']}/trace"
    )
    assert response.status_code == 200
    return response.text


async def test_range_header_reports_stored_byte_count(client, test_token):
    _, job = await _running_job(client, test_token)
    runner = _Runner(client, job)
    runner.write(b"0123456789")
    response = await runner.patch()
    assert response.status_code == 202
    # GitLab: "0-<size>", which the runner reads as its next offset.
    assert response.headers["Range"] == "0-10"
    assert runner.sent == 10


async def test_lost_response_retry_overlaps_and_completes(client, test_token):
    """The 2061/4011 shape: a committed PATCH whose response was lost."""
    project, job = await _running_job(client, test_token)
    runner = _Runner(client, job)
    runner.write(b"first line\n")
    assert (await runner.patch()).status_code == 202
    confirmed = runner.sent

    runner.write(b"second line\n")
    # Committed by the emulator, but the runner never sees the response
    # (emulator OOM-killed, proxy answers 502): its offset stays put.
    lost = await runner.patch()
    assert lost.status_code == 202
    runner.sent = confirmed

    # More output arrives; the runner resends from its confirmed offset.
    runner.write(b"third line \xe2\x9c\x93\n")
    retry = await runner.patch()
    assert retry.status_code == 202
    assert retry.headers["Range"] == f"0-{len(runner.output)}"
    assert runner.sent == len(runner.output)
    assert await _trace_text(client, project, job) == runner.output.decode()


async def test_range_mismatch_resynchronises_the_runner(client, test_token):
    """A 416 must hand the runner an offset it can append at, not loop it."""
    project, job = await _running_job(client, test_token)
    runner = _Runner(client, job)
    runner.write(b"abcdef")
    assert (await runner.patch()).status_code == 202

    runner.write(b"ghij")
    ahead = await runner.patch(start=8)  # past the stored end
    assert ahead.status_code == 416
    assert ahead.headers["Range"] == "0-6"
    assert runner.sent == 6

    # The next PATCH starts where the coordinator said and is accepted.
    resumed = await runner.patch()
    assert resumed.status_code == 202
    assert await _trace_text(client, project, job) == "abcdefghij"


async def test_multibyte_character_split_across_patches(client, test_token):
    """Offsets are bytes: a character split by a chunk boundary survives."""
    project, job = await _running_job(client, test_token)
    runner = _Runner(client, job)
    text = "café ✓ 日本語 ok\n".encode()
    cut = text.index("✓".encode()) + 1  # inside the 3-byte check mark
    runner.output = text[:cut]
    first = await runner.patch()
    assert first.status_code == 202
    assert first.headers["Range"] == f"0-{cut}"

    runner.output = text
    second = await runner.patch()
    assert second.status_code == 202
    assert second.headers["Range"] == f"0-{len(text)}"
    assert await _trace_text(client, project, job) == text.decode()


async def test_masking_keeps_runner_offsets_and_stores_no_secret(
    client, test_token, db_session
):
    from app.models.ci import JobTrace

    project, job = await _running_job(
        client, test_token, variables={"TRACE_SECRET": "s3cr3t-value"}
    )
    runner = _Runner(client, job)
    runner.write(b"token=s3cr3t-value end\n")
    response = await runner.patch()
    assert response.status_code == 202
    # The runner's byte count, not the length after redaction.
    assert response.headers["Range"] == f"0-{len(runner.output)}"

    # A secret split across two chunks is masked once complete.
    runner.write(b"again s3cr3t-")
    assert (await runner.patch()).status_code == 202
    runner.write(b"value done\n")
    assert (await runner.patch()).status_code == 202

    text = await _trace_text(client, project, job)
    assert text == "token=[MASKED] end\nagain [MASKED] done\n"
    stored = (await db_session.execute(
        select(JobTrace).where(JobTrace.job_id == job["id"])
    )).scalar_one()
    await db_session.refresh(stored)
    assert b"s3cr3t" not in stored.raw
    assert len(stored.raw) == len(runner.output) == stored.size


async def test_legacy_trace_without_raw_bytes_resumes(client, test_token, db_session):
    """Rows stored before the raw column: the next PATCH continues them."""
    from app.models.ci import JobTrace

    project, job = await _running_job(client, test_token)
    runner = _Runner(client, job)
    runner.write(b"before upgrade\n")
    assert (await runner.patch()).status_code == 202
    stored = (await db_session.execute(
        select(JobTrace).where(JobTrace.job_id == job["id"])
    )).scalar_one()
    stored.raw = None
    await db_session.commit()

    runner.write(b"after upgrade\n")
    assert (await runner.patch()).status_code == 202
    assert await _trace_text(client, project, job) == runner.output.decode()


async def test_failed_job_reaches_terminal_status_after_trace(client, test_token):
    project, job = await _running_job(client, test_token)
    runner = _Runner(client, job)
    runner.write(b"$ false\nERROR: Job failed: exit code 1\n")
    assert (await runner.patch()).status_code == 202
    update = await client.put(
        f"{API}/jobs/{job['id']}",
        json={
            "token": job["token"],
            "state": "failed",
            "failure_reason": "script_failure",
            "exit_code": 1,
            "output": {"bytesize": len(runner.output)},
        },
    )
    assert update.status_code == 200
    detail = await client.get(
        f"{API}/projects/{project['id']}/jobs/{job['id']}",
        headers=auth_headers(test_token),
    )
    assert detail.json()["status"] == "failed"
    assert await _trace_text(client, project, job) == runner.output.decode()
