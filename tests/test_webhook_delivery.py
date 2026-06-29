"""Webhook delivery compatibility tests."""

import json

import pytest

from app.models.repository import Repository
from app.models.webhook import Webhook
from app.services.webhook_service import deliver_webhook


class _FakeResponse:
    status_code = 200
    headers = {"x-receiver": "ok"}
    text = "accepted"


class _FakeAsyncClient:
    last_request: dict | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, content, headers):
        _FakeAsyncClient.last_request = {
            "url": url,
            "content": content,
            "headers": headers,
        }
        return _FakeResponse()


@pytest.mark.asyncio
async def test_deliver_webhook_uses_gitlab_event_headers(
    db_session,
    monkeypatch,
    test_user,
):
    monkeypatch.setattr(
        "app.services.webhook_service.httpx.AsyncClient",
        _FakeAsyncClient,
    )
    repository = Repository(
        owner_id=test_user.id,
        owner_type="User",
        name="hook-delivery",
        full_name="testuser/hook-delivery",
        disk_path="/tmp/hook-delivery.git",
    )
    db_session.add(repository)
    await db_session.flush()
    webhook = Webhook(
        repo_id=repository.id,
        url="https://receiver.example/hook",
        secret="hook-token",
        events=["pipeline_events"],
    )
    db_session.add(webhook)
    await db_session.commit()
    await db_session.refresh(webhook)

    delivery = await deliver_webhook(
        db_session,
        webhook,
        "pipeline_events",
        {"object_kind": "pipeline", "object_attributes": {"status": "success"}},
    )

    assert delivery.success is True
    assert delivery.status_code == 200
    assert delivery.event == "pipeline_events"
    assert delivery.request_headers["X-GitLab-Event"] == "Pipeline Hook"
    assert delivery.request_headers["X-Gitlab-Token"] == "hook-token"
    assert delivery.request_headers["X-Gitlab-Event-UUID"]
    assert (
        delivery.request_headers["X-GitLab-Delivery"]
        == delivery.request_headers["X-Gitlab-Event-UUID"]
    )
    assert delivery.request_headers["X-Hub-Signature-256"].startswith("sha256=")
    sent = _FakeAsyncClient.last_request
    assert sent is not None
    assert sent["headers"]["X-GitLab-Event"] == "Pipeline Hook"
    assert json.loads(sent["content"])["object_kind"] == "pipeline"
