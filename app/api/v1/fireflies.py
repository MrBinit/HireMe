"""Webhook routes for Fireflies transcript completion callbacks."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import get_application_service_dep, get_email_sender, get_fireflies_service
from app.core.runtime_config import get_runtime_config
from app.core.settings import get_settings
from app.schemas.application import ApplicationRecord
from app.services.application_service import ApplicationService
from app.services.email_sender import EmailSendError, EmailSender, InterviewParticipationThanksEmail
from app.services.fireflies_service import FirefliesApiError, FirefliesService

router = APIRouter(tags=["fireflies"])


@router.post("/fireflies/webhook")
async def fireflies_webhook_callback(
    request: Request,
    service: ApplicationService = Depends(get_application_service_dep),
    email_sender: EmailSender = Depends(get_email_sender),
    fireflies_service: FirefliesService = Depends(get_fireflies_service),
) -> dict[str, Any]:
    """Handle Fireflies transcription-complete webhook and persist transcript metadata."""

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

    meeting_id = _extract_meeting_id(payload)
    if not meeting_id:
        return {"status": "ignored", "reason": "meeting_id_missing"}

    if not fireflies_service.enabled:
        return {"status": "ignored", "reason": "fireflies_disabled", "meeting_id": meeting_id}

    try:
        match = await fireflies_service.get_transcript_by_id(transcript_id=meeting_id)
    except FirefliesApiError as exc:
        return {
            "status": "accepted",
            "reason": "fireflies_query_failed",
            "meeting_id": meeting_id,
            "error": str(exc),
        }

    if match is None:
        return {"status": "accepted", "reason": "transcript_not_ready", "meeting_id": meeting_id}

    meeting_link = _normalize_link(match.meeting_link)
    if not meeting_link:
        return {"status": "accepted", "reason": "meeting_link_missing", "meeting_id": meeting_id}

    candidate = await _find_candidate_by_meeting_link(service=service, meeting_link=meeting_link)
    if candidate is None:
        return {"status": "accepted", "reason": "candidate_not_found", "meeting_id": meeting_id}

    now_utc = datetime.now(tz=timezone.utc)
    current_payload = candidate.interview_schedule_options or {}
    updated_payload = dict(current_payload) if isinstance(current_payload, dict) else {}
    fireflies_payload = (
        dict(updated_payload.get("fireflies"))
        if isinstance(updated_payload.get("fireflies"), dict)
        else {}
    )
    transcript_sync = (
        dict(fireflies_payload.get("transcript_sync"))
        if isinstance(fireflies_payload.get("transcript_sync"), dict)
        else {}
    )
    transcript_sync["status"] = "completed"
    transcript_sync["attempts"] = int(transcript_sync.get("attempts") or 0) + 1
    transcript_sync["last_checked_at"] = now_utc.isoformat()
    transcript_sync["last_error"] = None

    summary_text = ""
    if isinstance(match.summary_text, str):
        summary_text = match.summary_text.strip()
    if not summary_text and match.action_items:
        summary_text = "; ".join(item.strip() for item in match.action_items[:3] if item.strip())
    transcript_url = ""
    if isinstance(match.transcript_url, str) and match.transcript_url.strip():
        transcript_url = match.transcript_url.strip()
    elif isinstance(match.video_url, str) and match.video_url.strip():
        transcript_url = match.video_url.strip()
    has_transcript_content = bool(transcript_url or summary_text or match.action_items)
    if not has_transcript_content:
        return {
            "status": "accepted",
            "reason": "transcript_content_missing",
            "meeting_id": meeting_id,
        }

    thank_you_state = (
        dict(fireflies_payload.get("thank_you_email"))
        if isinstance(fireflies_payload.get("thank_you_email"), dict)
        else {}
    )
    thank_you_status = str(thank_you_state.get("status") or "").strip().lower()
    if thank_you_status != "sent":
        try:
            await email_sender.send_interview_participation_thanks(
                InterviewParticipationThanksEmail(
                    candidate_name=candidate.full_name,
                    candidate_email=str(candidate.email),
                    role_title=candidate.role_selection,
                )
            )
            thank_you_state.update(
                {
                    "status": "sent",
                    "sent_at": now_utc.isoformat(),
                    "last_error": None,
                }
            )
        except EmailSendError as exc:
            thank_you_state.update(
                {
                    "status": "failed",
                    "last_attempt_at": now_utc.isoformat(),
                    "last_error": str(exc)[:500],
                }
            )

    fireflies_payload.update(
        {
            "status": "completed",
            "completed_at": now_utc.isoformat(),
            "transcript_sync": transcript_sync,
            "thank_you_email": thank_you_state,
            "transcript": {
                "id": match.transcript_id,
                "title": match.title,
                "url": transcript_url or None,
                "video_url": match.video_url,
                "meeting_link": match.meeting_link,
                "occurred_at": (match.occurred_at.isoformat() if match.occurred_at else None),
                "summary": summary_text or None,
                "action_items": match.action_items,
                "keywords": match.keywords,
                "raw": match.raw,
            },
        }
    )
    updated_payload["fireflies"] = fireflies_payload

    updates: dict[str, Any] = {
        "interview_schedule_options": updated_payload,
        "interview_transcript_status": "completed",
        "interview_transcript_url": transcript_url or None,
        "interview_transcript_summary": summary_text or None,
        "interview_transcript_synced_at": now_utc,
    }

    runtime_config = get_runtime_config()
    if runtime_config.scheduling.fireflies.update_schedule_status_on_complete:
        updates["interview_schedule_status"] = runtime_config.scheduling.fireflies.completed_schedule_status

    updated = await service.update_admin_review(
        application_id=candidate.id,
        updates=updates,
    )
    if updated is None:
        return {"status": "accepted", "reason": "candidate_update_failed", "meeting_id": meeting_id}

    return {
        "status": "ok",
        "meeting_id": meeting_id,
        "application_id": str(candidate.id),
        "interview_transcript_url": transcript_url,
    }


async def _find_candidate_by_meeting_link(
    *,
    service: ApplicationService,
    meeting_link: str,
) -> ApplicationRecord | None:
    """Find one candidate whose confirmed meeting link matches Fireflies transcript link."""

    offset = 0
    limit = 100
    normalized_target = _normalize_link(meeting_link)
    if not normalized_target:
        return None

    while True:
        page = await service.list(offset=offset, limit=limit)
        for item in page.items:
            payload = item.interview_schedule_options
            if not isinstance(payload, dict):
                continue
            candidate_link = _normalize_link(payload.get("confirmed_meeting_link"))
            if candidate_link and candidate_link == normalized_target:
                return item
        offset += limit
        if offset >= page.total:
            break
    return None


def _extract_meeting_id(payload: Any) -> str | None:
    """Extract meeting/transcript id from webhook payload with tolerant key matching."""

    priority_keys = ("meetingId", "meeting_id", "transcriptId", "transcript_id")
    fallback_keys = ("id",)

    def search(node: Any, keys: tuple[str, ...], *, depth: int = 0) -> str | None:
        if depth > 8:
            return None
        if isinstance(node, dict):
            for key in keys:
                raw = node.get(key)
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
            for value in node.values():
                found = search(value, keys, depth=depth + 1)
                if found:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = search(value, keys, depth=depth + 1)
                if found:
                    return found
        return None

    return search(payload, priority_keys) or search(payload, fallback_keys)


def _normalize_link(value: Any) -> str | None:
    """Normalize meeting links for stable matching."""

    if not isinstance(value, str):
        return None
    normalized = value.strip().rstrip("/")
    return normalized.casefold() if normalized else None
