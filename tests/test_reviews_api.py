"""Tests for PR review authorization."""

import pytest

from tests.conftest import API, auth_headers
from tests.test_projects_api import _create_user_and_token


async def _create_review_pr(client, token: str, repo_name: str) -> dict:
    repo = await client.post(
        f"{API}/user/repos",
        json={"name": repo_name},
        headers=auth_headers(token),
    )
    assert repo.status_code == 201
    issue = await client.post(
        f"{API}/repos/testuser/{repo_name}/issues",
        json={"title": "Review issue"},
        headers=auth_headers(token),
    )
    assert issue.status_code == 201
    pr = await client.post(
        f"{API}/repos/testuser/{repo_name}/pulls",
        json={"title": "Review PR", "head": "feature", "base": "main"},
        headers=auth_headers(token),
    )
    assert pr.status_code == 201
    return {"project": repo.json(), "pull_request": pr.json()}


@pytest.mark.asyncio
async def test_review_writes_require_project_roles(client, db_session, test_token):
    guest, guest_token = await _create_user_and_token(db_session, "review-guest")
    reporter, reporter_token = await _create_user_and_token(
        db_session, "review-reporter"
    )
    developer, developer_token = await _create_user_and_token(
        db_session, "review-developer"
    )
    fixture = await _create_review_pr(client, test_token, "review-role")
    project = fixture["project"]
    pr_number = fixture["pull_request"]["number"]
    for user, level in ((guest, 10), (reporter, 20), (developer, 30)):
        member = await client.post(
            f"{API}/projects/{project['id']}/members",
            json={"user_id": user.id, "access_level": level},
            headers=auth_headers(test_token),
        )
        assert member.status_code == 201

    denied_create = await client.post(
        f"{API}/repos/testuser/review-role/pulls/{pr_number}/reviews",
        json={"body": "guest denied"},
        headers=auth_headers(guest_token),
    )
    assert denied_create.status_code == 403

    created = await client.post(
        f"{API}/repos/testuser/review-role/pulls/{pr_number}/reviews",
        json={"body": "reporter review"},
        headers=auth_headers(reporter_token),
    )
    assert created.status_code == 201
    review_id = created.json()["id"]

    denied_submit = await client.put(
        f"{API}/repos/testuser/review-role/pulls/{pr_number}/reviews/{review_id}/events",
        json={"event": "COMMENT", "body": "guest submit"},
        headers=auth_headers(guest_token),
    )
    assert denied_submit.status_code == 403

    submitted = await client.put(
        f"{API}/repos/testuser/review-role/pulls/{pr_number}/reviews/{review_id}/events",
        json={"event": "COMMENT", "body": "reporter submit"},
        headers=auth_headers(reporter_token),
    )
    assert submitted.status_code == 200
    assert submitted.json()["state"] == "COMMENTED"

    denied_dismiss = await client.put(
        f"{API}/repos/testuser/review-role/pulls/{pr_number}/reviews/{review_id}/dismissals",
        json={"message": "reporter dismiss denied"},
        headers=auth_headers(reporter_token),
    )
    assert denied_dismiss.status_code == 403

    dismissed = await client.put(
        f"{API}/repos/testuser/review-role/pulls/{pr_number}/reviews/{review_id}/dismissals",
        json={"message": "developer dismiss"},
        headers=auth_headers(developer_token),
    )
    assert dismissed.status_code == 200
    assert dismissed.json()["state"] == "DISMISSED"
