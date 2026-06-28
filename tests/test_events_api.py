"""Tests for event API endpoints."""

import pytest

from app.models.event import Event
from tests.conftest import auth_headers
from tests.test_projects_api import _create_user_and_token

API = "/api/v4"


@pytest.mark.asyncio
async def test_events_include_repository_metadata(
    client, db_session, test_user, test_token
):
    actor, _ = await _create_user_and_token(db_session, "event-actor")
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "event-repo", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project_resp.status_code == 201
    project = project_resp.json()

    event = Event(
        type="PushEvent",
        actor_id=actor.id,
        repo_id=project["id"],
        payload={"ref": "refs/heads/main"},
        public=True,
    )
    db_session.add(event)
    await db_session.commit()

    public_events = await client.get(f"{API}/events", headers=auth_headers(test_token))
    assert public_events.status_code == 200
    data = public_events.json()
    assert len(data) == 1
    assert data[0]["type"] == "PushEvent"
    assert data[0]["actor"]["login"] == actor.login
    assert data[0]["repo"]["id"] == project["id"]
    assert data[0]["repo"]["name"] == "testuser/event-repo"
    assert data[0]["repo"]["url"].endswith(f"{API}/repos/testuser/event-repo")
    assert data[0]["payload"] == {"ref": "refs/heads/main"}

    repo_events = await client.get(
        f"{API}/repos/testuser/event-repo/events",
        headers=auth_headers(test_token),
    )
    assert repo_events.status_code == 200
    assert repo_events.json()[0]["repo"]["name"] == "testuser/event-repo"


@pytest.mark.asyncio
async def test_received_events_lists_public_events_on_owned_repositories(
    client, db_session, test_user, test_token
):
    actor, _ = await _create_user_and_token(db_session, "received-event-actor")
    project_resp = await client.post(
        f"{API}/projects",
        json={"name": "received-event-repo", "initialize_with_readme": True},
        headers=auth_headers(test_token),
    )
    assert project_resp.status_code == 201
    project = project_resp.json()

    db_session.add_all(
        [
            Event(
                type="IssueCommentEvent",
                actor_id=actor.id,
                repo_id=project["id"],
                payload={"action": "created"},
                public=True,
            ),
            Event(
                type="PushEvent",
                actor_id=test_user.id,
                repo_id=project["id"],
                payload={"ref": "refs/heads/main"},
                public=True,
            ),
            Event(
                type="PrivateEvent",
                actor_id=actor.id,
                repo_id=project["id"],
                payload={},
                public=False,
            ),
        ]
    )
    await db_session.commit()

    received = await client.get(
        f"{API}/users/testuser/received_events",
        headers=auth_headers(test_token),
    )

    assert received.status_code == 200
    data = received.json()
    assert len(data) == 1
    assert data[0]["type"] == "IssueCommentEvent"
    assert data[0]["actor"]["login"] == actor.login
    assert data[0]["repo"]["name"] == "testuser/received-event-repo"


@pytest.mark.asyncio
async def test_received_events_missing_user_returns_404(client, test_token):
    received = await client.get(
        f"{API}/users/missing-user/received_events",
        headers=auth_headers(test_token),
    )
    assert received.status_code == 404
