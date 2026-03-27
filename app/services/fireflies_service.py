"""Fireflies API integration for interview transcript + summary enrichment."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.runtime_config import SchedulingRuntimeConfig

logger = logging.getLogger(__name__)


class FirefliesApiError(RuntimeError):
    """Raised when a Fireflies API call fails."""


@dataclass(frozen=True)
class FirefliesTranscriptMatch:
    """Resolved Fireflies transcript payload selected for one interview."""

    transcript_id: str
    title: str | None
    transcript_url: str | None
    video_url: str | None
    meeting_link: str | None
    occurred_at: datetime | None
    summary_text: str | None
    action_items: list[str]
    keywords: list[str]
    raw: dict[str, Any]


class FirefliesService:
    """Async client for Fireflies GraphQL operations used by scheduling workers."""

    _TRANSCRIPTS_QUERY_CANDIDATES: tuple[str, ...] = (
        """
query SearchTranscripts($participants: [String!], $limit: Int!, $skip: Int!) {
  transcripts(participants: $participants, limit: $limit, skip: $skip) {
    id
    title
    date
    transcript_url
    video_url
    meeting_link
    participants
    host_email
    organizer_email
    meeting_info {
      summary_status
    }
    summary {
      short_summary
      overview
      gist
      bullet_gist
      action_items
      keywords
    }
  }
}
""",
        """
query SearchTranscripts($participants: [String!], $limit: Int!, $skip: Int!) {
  transcripts(participants: $participants, limit: $limit, skip: $skip) {
    id
    title
    date
    transcript_url
    video_url
    meeting_link
    participants
    host_email
    organizer_email
    summary {
      short_summary
      overview
      gist
      action_items
    }
  }
}
""",
        """
query SearchTranscripts($participants: [String!], $limit: Int!, $skip: Int!) {
  transcripts(participants: $participants, limit: $limit, skip: $skip) {
    id
    title
    date
    transcript_url
    video_url
    meeting_link
    participants
    host_email
    organizer_email
  }
}
""",
    )

    _ADD_TO_LIVE_MUTATION = """
mutation AddToLiveMeeting($meetingLink: String!, $title: String) {
  addToLiveMeeting(meeting_link: $meetingLink, title: $title) {
    success
    message
  }
}
"""

    _TRANSCRIPT_BY_ID_QUERY_CANDIDATES: tuple[str, ...] = (
        """
query TranscriptById($id: String!) {
  transcript(id: $id) {
    id
    title
    date
    transcript_url
    video_url
    meeting_link
    participants
    host_email
    organizer_email
    meeting_info {
      summary_status
      fred_joined
    }
    summary {
      short_summary
      short_overview
      overview
      notes
      gist
      bullet_gist
      shorthand_bullet
      outline
      action_items
      keywords
    }
  }
}
""",
        """
query TranscriptById($id: String!) {
  transcript(id: $id) {
    id
    title
    date
    transcript_url
    video_url
    meeting_link
    participants
    host_email
    organizer_email
    meeting_info {
      summary_status
    }
    summary {
      short_summary
      short_overview
      overview
      notes
      gist
      bullet_gist
      action_items
      keywords
    }
  }
}
""",
        """
query TranscriptById($id: String!) {
  transcript(id: $id) {
    id
    title
    date
    transcript_url
    video_url
    meeting_link
    participants
    host_email
    organizer_email
    summary {
      short_summary
      overview
      gist
      action_items
      keywords
    }
  }
}
""",
        """
query TranscriptById($id: String!) {
  transcript(id: $id) {
    id
    title
    date
    transcript_url
    video_url
    meeting_link
    participants
    host_email
    organizer_email
  }
}
""",
    )

    def __init__(
        self,
        *,
        api_key: str | None,
        config: SchedulingRuntimeConfig.FirefliesRuntimeConfig,
    ) -> None:
        """Initialize Fireflies service with runtime config + API key."""

        normalized_key = (api_key or "").strip()
        self._api_key = normalized_key
        self._config = config
        self._enabled = bool(config.enabled and normalized_key)
        self._owner_email = (config.owner_email or "").strip().casefold() or None

    @property
    def enabled(self) -> bool:
        """Return True when integration is configured and active."""

        return self._enabled

    def should_track_manager(self, manager_email: str | None) -> bool:
        """Return True when manager belongs to configured Fireflies owner scope."""

        if not self._enabled:
            return False
        if not self._owner_email:
            return True
        normalized = (manager_email or "").strip().casefold()
        return bool(normalized and normalized == self._owner_email)

    def build_tracking_state(
        self,
        *,
        manager_email: str,
        candidate_email: str,
        meeting_link: str | None,
        confirmed_event_id: str,
        confirmed_start_at: datetime,
        confirmed_end_at: datetime,
    ) -> dict[str, Any]:
        """Build deterministic Fireflies tracking payload persisted in DB JSON."""

        return {
            "enabled": self._enabled,
            "owner_email": self._owner_email,
            "status": "scheduled",
            "manager_email": manager_email,
            "candidate_email": candidate_email,
            "meeting_link": meeting_link,
            "confirmed_event_id": confirmed_event_id,
            "meeting_start_at": confirmed_start_at.astimezone(timezone.utc).isoformat(),
            "meeting_end_at": confirmed_end_at.astimezone(timezone.utc).isoformat(),
            "bot_request": {
                "status": "pending",
                "attempts": 0,
                "last_attempt_at": None,
                "last_error": None,
            },
            "transcript_sync": {
                "status": "pending",
                "attempts": 0,
                "last_checked_at": None,
                "last_error": None,
            },
            "transcript": None,
        }

    async def request_live_capture(self, *, meeting_link: str, title: str | None) -> dict[str, Any]:
        """Ask Fireflies bot to join a live meeting link."""

        payload = await self._graphql(
            query=self._ADD_TO_LIVE_MUTATION,
            variables={"meetingLink": meeting_link, "title": title},
        )
        raw = payload.get("addToLiveMeeting")
        if not isinstance(raw, dict):
            return {"success": False, "message": "missing addToLiveMeeting response"}
        success = bool(raw.get("success"))
        message = raw.get("message")
        error = raw.get("error")
        return {
            "success": success,
            "message": message if isinstance(message, str) else None,
            "error": error if isinstance(error, str) else None,
        }

    async def find_best_transcript(
        self,
        *,
        manager_email: str | None,
        candidate_email: str | None,
        meeting_link: str | None,
        candidate_name: str | None,
        meeting_start_at: datetime | None,
    ) -> FirefliesTranscriptMatch | None:
        """Search Fireflies transcripts and return best match for one interview."""

        participants = [
            value.strip().casefold()
            for value in [manager_email, candidate_email]
            if isinstance(value, str) and "@" in value
        ]
        transcripts = await self._search_transcripts(
            participants=participants,
            limit=max(1, self._config.transcripts_page_limit),
            max_pages=max(1, self._config.max_transcript_pages),
        )
        if not transcripts:
            return None

        lookup_threshold = datetime.now(tz=timezone.utc) - timedelta(
            hours=max(1, self._config.transcript_lookup_hours)
        )
        best_item: dict[str, Any] | None = None
        best_score = -1
        for item in transcripts:
            occurred_at = self._parse_fireflies_datetime(item.get("date"))
            if occurred_at and occurred_at < lookup_threshold:
                continue
            score = self._score_transcript(
                item=item,
                manager_email=manager_email,
                candidate_email=candidate_email,
                meeting_link=meeting_link,
                candidate_name=candidate_name,
                meeting_start_at=meeting_start_at,
            )
            if score > best_score:
                best_score = score
                best_item = item
        if best_item is None or best_score <= 0:
            return None

        transcript_id = best_item.get("id")
        if not isinstance(transcript_id, str) or not transcript_id.strip():
            return None
        summary_text, action_items, keywords = self._extract_summary_fields(best_item)
        transcript_url = best_item.get("transcript_url")
        video_url = best_item.get("video_url")
        raw_meeting_link = best_item.get("meeting_link")
        title = best_item.get("title")
        return FirefliesTranscriptMatch(
            transcript_id=transcript_id,
            title=title if isinstance(title, str) else None,
            transcript_url=transcript_url if isinstance(transcript_url, str) else None,
            video_url=video_url if isinstance(video_url, str) else None,
            meeting_link=raw_meeting_link if isinstance(raw_meeting_link, str) else None,
            occurred_at=self._parse_fireflies_datetime(best_item.get("date")),
            summary_text=summary_text,
            action_items=action_items,
            keywords=keywords,
            raw=best_item,
        )

    async def get_transcript_by_id(self, *, transcript_id: str) -> FirefliesTranscriptMatch | None:
        """Fetch one transcript directly by Fireflies transcript/meeting id."""

        normalized_id = transcript_id.strip()
        if not normalized_id:
            return None

        item: dict[str, Any] | None = None
        for query in self._TRANSCRIPT_BY_ID_QUERY_CANDIDATES:
            try:
                payload = await self._graphql(
                    query=query,
                    variables={"id": normalized_id},
                )
            except FirefliesApiError:
                continue
            raw = payload.get("transcript")
            if isinstance(raw, dict):
                item = raw
                break
        if item is None:
            return None

        record_id = item.get("id")
        if not isinstance(record_id, str) or not record_id.strip():
            return None
        summary_text, action_items, keywords = self._extract_summary_fields(item)
        transcript_url = item.get("transcript_url")
        video_url = item.get("video_url")
        raw_meeting_link = item.get("meeting_link")
        title = item.get("title")
        return FirefliesTranscriptMatch(
            transcript_id=record_id,
            title=title if isinstance(title, str) else None,
            transcript_url=transcript_url if isinstance(transcript_url, str) else None,
            video_url=video_url if isinstance(video_url, str) else None,
            meeting_link=raw_meeting_link if isinstance(raw_meeting_link, str) else None,
            occurred_at=self._parse_fireflies_datetime(item.get("date")),
            summary_text=summary_text,
            action_items=action_items,
            keywords=keywords,
            raw=item,
        )

    async def _search_transcripts(
        self,
        *,
        participants: list[str],
        limit: int,
        max_pages: int,
    ) -> list[dict[str, Any]]:
        """Fetch transcript candidates using resilient query fallback strategy."""

        for query in self._TRANSCRIPTS_QUERY_CANDIDATES:
            try:
                rows = await self._fetch_transcript_pages(
                    query=query,
                    participants=participants,
                    limit=limit,
                    max_pages=max_pages,
                )
                if rows:
                    return rows

                # Some Fireflies accounts return empty results when participants filter is applied.
                # Fall back to unfiltered listing and let scoring match by link/email/time/name.
                if participants:
                    fallback_rows = await self._fetch_transcript_pages(
                        query=query,
                        participants=[],
                        limit=limit,
                        max_pages=max_pages,
                    )
                    if fallback_rows:
                        return fallback_rows
            except FirefliesApiError:
                continue
        return []

    async def _fetch_transcript_pages(
        self,
        *,
        query: str,
        participants: list[str],
        limit: int,
        max_pages: int,
    ) -> list[dict[str, Any]]:
        """Execute paginated transcript query and normalize rows."""

        rows: list[dict[str, Any]] = []
        for page in range(max_pages):
            payload = await self._graphql(
                query=query,
                variables={
                    "participants": participants,
                    "limit": limit,
                    "skip": page * limit,
                },
            )
            chunk = payload.get("transcripts")
            if not isinstance(chunk, list) or not chunk:
                break
            rows.extend(item for item in chunk if isinstance(item, dict))
            if len(chunk) < limit:
                break
        return rows

    async def _graphql(self, *, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Send one Fireflies GraphQL request and return `data` payload."""

        if not self._enabled:
            raise FirefliesApiError("fireflies integration is disabled")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        request_payload = {"query": query, "variables": variables}
        try:
            async with httpx.AsyncClient(timeout=self._config.request_timeout_seconds) as client:
                response = await client.post(
                    self._config.api_url,
                    headers=headers,
                    json=request_payload,
                )
        except httpx.HTTPError as exc:
            raise FirefliesApiError(f"fireflies request failed: {exc}") from exc

        if response.status_code >= 400:
            raise FirefliesApiError(
                f"fireflies API returned HTTP {response.status_code}: {response.text[:500]}"
            )
        body = response.json()
        if not isinstance(body, dict):
            raise FirefliesApiError("fireflies API returned non-JSON response")
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            combined = "; ".join(
                item.get("message", "") for item in errors if isinstance(item, dict)
            ).strip()
            raise FirefliesApiError(combined or "fireflies API returned GraphQL errors")
        data = body.get("data")
        if not isinstance(data, dict):
            raise FirefliesApiError("fireflies API response missing GraphQL data")
        return data

    @classmethod
    def _score_transcript(
        cls,
        *,
        item: dict[str, Any],
        manager_email: str | None,
        candidate_email: str | None,
        meeting_link: str | None,
        candidate_name: str | None,
        meeting_start_at: datetime | None,
    ) -> int:
        """Compute match score between transcript record and candidate interview metadata."""

        score = 0
        normalized_link = cls._normalize_link(meeting_link)
        record_link = cls._normalize_link(item.get("meeting_link"))
        if normalized_link and record_link and normalized_link == record_link:
            score += 100

        participants = {
            token.strip().casefold()
            for token in cls._to_list(item.get("participants"))
            if isinstance(token, str)
        }
        raw_host_email = item.get("host_email")
        raw_organizer_email = item.get("organizer_email")
        host_email = raw_host_email.strip().casefold() if isinstance(raw_host_email, str) else ""
        organizer_email = (
            raw_organizer_email.strip().casefold() if isinstance(raw_organizer_email, str) else ""
        )
        candidate_email_norm = (candidate_email or "").strip().casefold()
        manager_email_norm = (manager_email or "").strip().casefold()
        if candidate_email_norm and candidate_email_norm in participants:
            score += 30
        if manager_email_norm and (
            manager_email_norm in participants
            or manager_email_norm == host_email
            or manager_email_norm == organizer_email
        ):
            score += 20

        occurred_at = cls._parse_fireflies_datetime(item.get("date"))
        if meeting_start_at and occurred_at:
            delta_minutes = abs(
                int(
                    (
                        occurred_at.astimezone(timezone.utc)
                        - meeting_start_at.astimezone(timezone.utc)
                    ).total_seconds()
                    / 60
                )
            )
            if delta_minutes <= 120:
                score += 20
            elif delta_minutes <= 720:
                score += 10

        title = (item.get("title") or "").strip().casefold()
        if title and candidate_name:
            for token in candidate_name.strip().casefold().split():
                if len(token) >= 3 and token in title:
                    score += 2
                    break
        return score

    @staticmethod
    def _extract_summary_fields(item: dict[str, Any]) -> tuple[str | None, list[str], list[str]]:
        """Extract normalized summary text, action items, and keywords from transcript."""

        summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
        summary_text = None
        for key in (
            "short_summary",
            "short_overview",
            "overview",
            "notes",
            "gist",
            "bullet_gist",
            "shorthand_bullet",
            "outline",
        ):
            value = summary.get(key)
            if isinstance(value, str) and value.strip():
                summary_text = " ".join(value.split()).strip()
                break

        action_items = [
            " ".join(str(value).split()).strip()
            for value in FirefliesService._to_list(summary.get("action_items"))
            if isinstance(value, str) and value.strip()
        ]
        keywords = [
            " ".join(str(value).split()).strip()
            for value in FirefliesService._to_list(summary.get("keywords"))
            if isinstance(value, str) and value.strip()
        ]
        return summary_text, action_items[:20], keywords[:25]

    @staticmethod
    def _to_list(value: Any) -> list[Any]:
        """Normalize scalar or list values into a list."""

        if isinstance(value, list):
            return value
        if value is None:
            return []
        return [value]

    @staticmethod
    def _normalize_link(value: Any) -> str | None:
        """Normalize meeting links for exact-ish comparison."""

        if not isinstance(value, str):
            return None
        normalized = value.strip().rstrip("/")
        return normalized.casefold() if normalized else None

    @staticmethod
    def _parse_fireflies_datetime(value: Any) -> datetime | None:
        """Parse Fireflies date value (epoch/int/float/ISO) into UTC datetime."""

        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp = timestamp / 1000.0
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        if isinstance(value, str) and value.strip():
            raw = value.strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        return None
