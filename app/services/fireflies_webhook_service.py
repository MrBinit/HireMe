"""Deferred Fireflies webhook processing service."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.core.runtime_config import get_runtime_config
from app.services.application_service import ApplicationService
from app.services.email_sender import EmailSendError, EmailSender, InterviewParticipationThanksEmail
from app.services.fireflies_service import FirefliesApiError, FirefliesService

logger = logging.getLogger(__name__)


class FirefliesWebhookProcessor:
    """Processes transcript-ready Fireflies events outside request path."""

    def __init__(
        self,
        *,
        service: ApplicationService,
        email_sender: EmailSender,
        fireflies_service: FirefliesService,
    ) -> None:
        """Initialize processor dependencies."""

        self._service = service
        self._email_sender = email_sender
        self._fireflies_service = fireflies_service

    async def process_meeting_id(self, *, meeting_id: str) -> bool:
        """Resolve transcript by meeting id and persist candidate transcript fields."""

        try:
            match = await self._fireflies_service.get_transcript_by_id(transcript_id=meeting_id)
        except FirefliesApiError as exc:
            logger.warning(
                "fireflies webhook meeting_id=%s transcript query failed: %s",
                meeting_id,
                str(exc),
            )
            return False

        if match is None:
            logger.info("fireflies webhook meeting_id=%s transcript not ready", meeting_id)
            return False

        meeting_link = _normalize_link(match.meeting_link)
        if not meeting_link:
            logger.info("fireflies webhook meeting_id=%s meeting link missing", meeting_id)
            return False

        candidate = await self._service.get_by_confirmed_meeting_link(meeting_link=meeting_link)
        if candidate is None:
            logger.info(
                "fireflies webhook meeting_id=%s candidate not found for meeting_link",
                meeting_id,
            )
            return False

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
            summary_text = "; ".join(
                item.strip() for item in match.action_items[:3] if item.strip()
            )
        transcript_url = ""
        if isinstance(match.transcript_url, str) and match.transcript_url.strip():
            transcript_url = match.transcript_url.strip()
        elif isinstance(match.video_url, str) and match.video_url.strip():
            transcript_url = match.video_url.strip()
        has_transcript_content = bool(transcript_url or summary_text or match.action_items)
        if not has_transcript_content:
            logger.info("fireflies webhook meeting_id=%s transcript content missing", meeting_id)
            return False

        thank_you_state = (
            dict(fireflies_payload.get("thank_you_email"))
            if isinstance(fireflies_payload.get("thank_you_email"), dict)
            else {}
        )
        thank_you_status = str(thank_you_state.get("status") or "").strip().lower()
        if thank_you_status != "sent":
            try:
                await self._email_sender.send_interview_participation_thanks(
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
            updates["interview_schedule_status"] = (
                runtime_config.scheduling.fireflies.completed_schedule_status
            )

        updated = await self._service.update_admin_review(
            application_id=candidate.id,
            updates=updates,
        )
        if updated is None:
            logger.warning(
                "fireflies webhook meeting_id=%s candidate update failed application_id=%s",
                meeting_id,
                candidate.id,
            )
            return False
        logger.info(
            "fireflies webhook meeting_id=%s processed application_id=%s transcript_url=%s",
            meeting_id,
            candidate.id,
            bool(transcript_url),
        )
        return True


def extract_fireflies_meeting_id(payload: Any) -> str | None:
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
