"""GitLab pagination header tests."""

import pytest

from tests.conftest import API, auth_headers


def _rels(link_header: str) -> dict[str, str]:
    rels: dict[str, str] = {}
    for part in link_header.split(","):
        url_part, _, rel_part = part.strip().partition(";")
        rel = rel_part.strip().removeprefix('rel="').removesuffix('"')
        rels[rel] = url_part.strip("<>")
    return rels


@pytest.mark.asyncio
async def test_project_list_pagination_headers_preserve_filters(client, test_token):
    for name in ("page-link-a", "page-link-b", "page-link-c"):
        created = await client.post(
            f"{API}/projects",
            json={"name": name},
            headers=auth_headers(test_token),
        )
        assert created.status_code == 201

    resp = await client.get(
        f"{API}/projects",
        params={"search": "page-link", "page": 2, "per_page": 1},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.headers["X-Total"] == "3"
    assert resp.headers["X-Total-Pages"] == "3"
    assert resp.headers["X-Page"] == "2"
    assert resp.headers["X-Per-Page"] == "1"
    assert resp.headers["X-Prev-Page"] == "1"
    assert resp.headers["X-Next-Page"] == "3"
    rels = _rels(resp.headers["Link"])
    assert "search=page-link" in rels["next"]
    assert "page=3" in rels["next"]
    assert "page=1" in rels["prev"]
    assert "page=3" in rels["last"]
    assert "page=1" in rels["first"]


@pytest.mark.asyncio
async def test_empty_paginated_list_reports_zero_totals(client, test_token):
    resp = await client.get(
        f"{API}/groups",
        params={"search": "no-such-group", "page": 1, "per_page": 10},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    assert resp.json() == []
    assert resp.headers["X-Total"] == "0"
    assert resp.headers["X-Total-Pages"] == "0"
    assert resp.headers["X-Prev-Page"] == ""
    assert resp.headers["X-Next-Page"] == ""
    assert "Link" not in resp.headers


@pytest.mark.asyncio
async def test_merge_request_pagination_headers(client, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "mr-pages", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]
    for branch in ("feature-a", "feature-b"):
        branch_resp = await client.post(
            f"{API}/projects/{project_id}/repository/branches",
            json={"branch": branch, "ref": "main"},
            headers=auth_headers(test_token),
        )
        assert branch_resp.status_code == 201
        mr = await client.post(
            f"{API}/projects/{project_id}/merge_requests",
            json={
                "title": f"MR {branch}",
                "source_branch": branch,
                "target_branch": "main",
            },
            headers=auth_headers(test_token),
        )
        assert mr.status_code == 201

    resp = await client.get(
        f"{API}/projects/{project_id}/merge_requests",
        params={"page": 1, "per_page": 1, "state": "all"},
        headers=auth_headers(test_token),
    )

    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.headers["X-Total"] == "2"
    assert resp.headers["X-Total-Pages"] == "2"
    assert resp.headers["X-Next-Page"] == "2"
    assert 'rel="next"' in resp.headers["Link"]


@pytest.mark.asyncio
async def test_pipeline_and_job_lists_are_paginated(client, test_token):
    project = await client.post(
        f"{API}/projects",
        json={"name": "pipeline-pages", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]
    for job_name in ("first", "second"):
        pipeline = await client.post(
            f"{API}/projects/{project_id}/pipeline",
            json={
                "ref": "main",
                "job": {"name": job_name, "script": [f"echo {job_name}"]},
            },
            headers=auth_headers(test_token),
        )
        assert pipeline.status_code == 201

    pipelines = await client.get(
        f"{API}/projects/{project_id}/pipelines",
        params={"page": 1, "per_page": 1},
        headers=auth_headers(test_token),
    )
    assert pipelines.status_code == 200
    assert len(pipelines.json()) == 1
    assert pipelines.headers["X-Total"] == "2"
    assert pipelines.headers["X-Total-Pages"] == "2"

    jobs = await client.get(
        f"{API}/projects/{project_id}/jobs",
        params={"page": 2, "per_page": 1},
        headers=auth_headers(test_token),
    )
    assert jobs.status_code == 200
    assert len(jobs.json()) == 1
    assert jobs.headers["X-Total"] == "2"
    assert jobs.headers["X-Prev-Page"] == "1"
