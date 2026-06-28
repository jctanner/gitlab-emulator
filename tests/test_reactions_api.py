"""Tests for issue and issue-comment reaction endpoints."""

import pytest

from tests.conftest import auth_headers
from tests.test_projects_api import _create_user_and_token

API = "/api/v4"


@pytest.mark.asyncio
async def test_reaction_writes_require_reporter(
    client, db_session, test_user, test_token
):
    reporter, reporter_token = await _create_user_and_token(
        db_session, "reaction-role-reporter"
    )
    outsider, outsider_token = await _create_user_and_token(
        db_session, "reaction-role-outsider"
    )
    project = await client.post(
        f"{API}/projects",
        json={"name": "reaction-role-project"},
        headers=auth_headers(test_token),
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    member = await client.post(
        f"{API}/projects/{project_id}/members",
        json={"user_id": reporter.id, "access_level": 20},
        headers=auth_headers(test_token),
    )
    assert member.status_code == 201

    issue = await client.post(
        f"{API}/projects/{project_id}/issues",
        json={"title": "reaction role issue"},
        headers=auth_headers(test_token),
    )
    assert issue.status_code == 201
    comment = await client.post(
        f"{API}/repos/testuser/reaction-role-project/issues/1/comments",
        json={"body": "reaction target"},
        headers=auth_headers(test_token),
    )
    assert comment.status_code == 201
    comment_id = comment.json()["id"]

    denied_issue_reaction = await client.post(
        f"{API}/repos/testuser/reaction-role-project/issues/1/reactions",
        json={"content": "+1"},
        headers=auth_headers(outsider_token),
    )
    assert denied_issue_reaction.status_code == 403

    allowed_issue_reaction = await client.post(
        f"{API}/repos/testuser/reaction-role-project/issues/1/reactions",
        json={"content": "+1"},
        headers=auth_headers(reporter_token),
    )
    assert allowed_issue_reaction.status_code == 201

    denied_comment_reaction = await client.post(
        f"{API}/repos/testuser/reaction-role-project/issues/comments/{comment_id}/reactions",
        json={"content": "rocket"},
        headers=auth_headers(outsider_token),
    )
    assert denied_comment_reaction.status_code == 403

    allowed_comment_reaction = await client.post(
        f"{API}/repos/testuser/reaction-role-project/issues/comments/{comment_id}/reactions",
        json={"content": "rocket"},
        headers=auth_headers(reporter_token),
    )
    assert allowed_comment_reaction.status_code == 201
