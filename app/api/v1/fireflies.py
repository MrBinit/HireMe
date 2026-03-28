"""Webhook routes for Fireflies transcript completion callbacks."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import (
    get_fireflies_service,
    get_webhook_event_queue_publisher_dep,
)
from app.core.settings import get_settings
from app.services.fireflies_service import FirefliesService
from app.services.fireflies_webhook_service import extract_fireflies_meeting_id
from app.services.webhook_event_queue import (
    NoopWebhookEventQueuePublisher,
    WebhookEventJob,
    WebhookEventQueueBackpressureError,
    WebhookEventQueuePublishError,
    WebhookEventQueuePublisher,
)

router = APIRouter(tags=["fireflies"])


@router.post("/fireflies/webhook")
async def fireflies_webhook_callback(
    request: Request,
    fireflies_service: FirefliesService = Depends(get_fireflies_service),
    webhook_queue_publisher: WebhookEventQueuePublisher = Depends(
        get_webhook_event_queue_publisher_dep
    ),
) -> dict[str, Any]:
    """Fast-ACK Fireflies webhook by enqueueing deferred transcript processing."""

    settings = get_settings()
    expected_secret = (settings.fireflies_webhook_secret or "").strip()
    provided_secret = (
        request.headers.get("x-fireflies-webhook-token")
        or request.headers.get("x-webhook-token")
        or request.query_params.get("token")
        or ""
    ).strip()
    if expected_secret and provided_secret != expected_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid Fireflies webhook token",
        )

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid webhook JSON payload",
        ) from exc

    meeting_id = extract_fireflies_meeting_id(payload)
    if not meeting_id:
        return {"status": "ignored", "reason": "meeting_id_missing"}

    if not fireflies_service.enabled:
        return {"status": "ignored", "reason": "fireflies_disabled", "meeting_id": meeting_id}
    if isinstance(webhook_queue_publisher, NoopWebhookEventQueuePublisher):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="fireflies webhook queue is not configured",
        )

    event_key = f"fireflies:transcript_ready:{meeting_id.strip()}"
    job = WebhookEventJob(
        event_type="fireflies_transcript_ready",
        event_key=event_key,
        payload={"meeting_id": meeting_id.strip()},
        queued_at=datetime.now(tz=timezone.utc),
    )
    try:
        await webhook_queue_publisher.publish(job)
    except WebhookEventQueueBackpressureError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except WebhookEventQueuePublishError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="failed to queue Fireflies webhook processing",
        ) from exc

    return {
        "status": "accepted",
        "meeting_id": meeting_id,
        "queued": True,
    }
