"""Tests for Fireflies webhook enqueue/ack behavior."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import (
    get_fireflies_service,
    get_webhook_event_queue_publisher_dep,
)
from app.api.v1.fireflies import router as fireflies_router
from app.core.settings import get_settings
from app.services.webhook_event_queue import WebhookEventJob, WebhookEventQueuePublisher


class _FakeFirefliesService:
    """Minimal Fireflies service stub exposing enabled toggle."""

    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled


class _CaptureWebhookQueuePublisher(WebhookEventQueuePublisher):
    """In-memory webhook queue publisher for endpoint tests."""

    def __init__(self) -> None:
        self.jobs: list[WebhookEventJob] = []

    async def publish(self, job: WebhookEventJob) -> None:
        self.jobs.append(job)


def _build_client(
    *,
    fireflies_enabled: bool = True,
) -> tuple[TestClient, _CaptureWebhookQueuePublisher]:
    """Create test app/client with dependency overrides."""

    app = FastAPI()
    app.include_router(fireflies_router, prefix="/api/v1")
    queue = _CaptureWebhookQueuePublisher()
    app.dependency_overrides[get_fireflies_service] = lambda: _FakeFirefliesService(
        enabled=fireflies_enabled
    )
    app.dependency_overrides[get_webhook_event_queue_publisher_dep] = lambda: queue
    return TestClient(app), queue


def test_fireflies_webhook_enqueues_durable_job(monkeypatch) -> None:
    """Valid webhook payload should be fast-acked and queued."""

    monkeypatch.setenv("FIREFLIES_WEBHOOK_SECRET", "hook-secret")
    get_settings.cache_clear()
    client, queue = _build_client(fireflies_enabled=True)

    response = client.post(
        "/api/v1/fireflies/webhook",
        headers={"x-fireflies-webhook-token": "hook-secret"},
        json={"meetingId": "meeting-123"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["queued"] is True
    assert body["meeting_id"] == "meeting-123"
    assert len(queue.jobs) == 1
    assert queue.jobs[0].event_type == "fireflies_transcript_ready"
    assert queue.jobs[0].event_key == "fireflies:transcript_ready:meeting-123"
    assert queue.jobs[0].payload["meeting_id"] == "meeting-123"


def test_fireflies_webhook_rejects_invalid_secret(monkeypatch) -> None:
    """Invalid webhook token should return unauthorized."""

    monkeypatch.setenv("FIREFLIES_WEBHOOK_SECRET", "expected-secret")
    get_settings.cache_clear()
    client, queue = _build_client(fireflies_enabled=True)

    response = client.post(
        "/api/v1/fireflies/webhook",
        headers={"x-fireflies-webhook-token": "wrong"},
        json={"meetingId": "meeting-xyz"},
    )

    assert response.status_code == 401
    assert queue.jobs == []


def test_fireflies_webhook_ignores_missing_meeting_id(monkeypatch) -> None:
    """Webhook should be ignored when payload lacks transcript/meeting id."""

    monkeypatch.setenv("FIREFLIES_WEBHOOK_SECRET", "hook-secret")
    get_settings.cache_clear()
    client, queue = _build_client(fireflies_enabled=True)

    response = client.post(
        "/api/v1/fireflies/webhook",
        headers={"x-fireflies-webhook-token": "hook-secret"},
        json={"foo": {"bar": "baz"}},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "meeting_id_missing"}
    assert queue.jobs == []
