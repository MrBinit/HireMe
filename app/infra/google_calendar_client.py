"""Google Calendar API client used for interviewer slot orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio


class GoogleCalendarAuthError(RuntimeError):
    """Raised when Google Calendar authentication cannot be initialized."""


class GoogleCalendarApiError(RuntimeError):
    """Raised when one Google Calendar API call fails."""


@dataclass(frozen=True)
class CalendarBusyInterval:
    """Busy interval returned by Google free/busy API."""

    start_at: datetime
    end_at: datetime


@dataclass(frozen=True)
class CalendarHoldEvent:
    """Created hold-event metadata from Google Calendar."""

    event_id: str
    html_link: str | None
    start_at: datetime
    end_at: datetime
    meeting_link: str | None = None


class GoogleCalendarClient:
    """Thin async wrapper around Google Calendar API calls."""

    def __init__(
        self,
        *,
        service_account_json: str | None,
        service_account_file: str | None,
        oauth_client_id: str | None = None,
        oauth_client_secret: str | None = None,
        oauth_refresh_token: str | None = None,
        oauth_token_uri: str = "https://oauth2.googleapis.com/token",
        scopes: list[str] | None = None,
    ) -> None:
        """Initialize Google Calendar client with service-account or OAuth credentials."""

        self._service_account_json = service_account_json
        self._service_account_file = service_account_file
        self._oauth_client_id = oauth_client_id
        self._oauth_client_secret = oauth_client_secret
        self._oauth_refresh_token = oauth_refresh_token
        self._oauth_token_uri = oauth_token_uri
        self._scopes = scopes or ["https://www.googleapis.com/auth/calendar"]
        self._auth_mode, self._service_account_info = self._resolve_auth_mode()

    async def list_busy_intervals(
        self,
        *,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        delegated_user: str | None,
    ) -> list[CalendarBusyInterval]:
        """Return busy intervals for one calendar within a UTC time window."""

        return await anyio.to_thread.run_sync(
            self._list_busy_intervals_sync,
            calendar_id,
            time_min.astimezone(timezone.utc),
            time_max.astimezone(timezone.utc),
            delegated_user,
        )

    async def create_hold_event(
        self,
        *,
        calendar_id: str,
        delegated_user: str | None,
        title: str,
        description: str,
        start_at: datetime,
        end_at: datetime,
        timezone_name: str,
        extended_private_properties: dict[str, str] | None = None,
    ) -> CalendarHoldEvent:
        """Create one blocking hold event in interviewer calendar."""

        return await anyio.to_thread.run_sync(
            self._create_hold_event_sync,
            calendar_id,
            delegated_user,
            title,
            description,
            start_at,
            end_at,
            timezone_name,
            extended_private_properties or {},
        )

    async def delete_event(
        self,
        *,
        calendar_id: str,
        delegated_user: str | None,
        event_id: str,
    ) -> None:
        """Delete one existing calendar event."""

        await anyio.to_thread.run_sync(
            self._delete_event_sync,
            calendar_id,
            delegated_user,
            event_id,
        )

    async def confirm_hold_event(
        self,
        *,
        calendar_id: str,
        delegated_user: str | None,
        event_id: str,
        title: str,
        description: str,
        attendee_emails: list[str],
        send_updates: str = "all",
        extended_private_properties: dict[str, str] | None = None,
    ) -> CalendarHoldEvent:
        """Confirm an existing hold event and invite candidate attendee."""

        return await anyio.to_thread.run_sync(
            self._confirm_hold_event_sync,
            calendar_id,
            delegated_user,
            event_id,
            title,
            description,
            attendee_emails,
            send_updates,
            extended_private_properties or {},
        )

    def _resolve_auth_mode(self) -> tuple[str, dict[str, Any] | None]:
        """Resolve active auth mode and any credential object for that mode."""

        if self._service_account_json:
            try:
                parsed = json.loads(self._service_account_json)
            except json.JSONDecodeError as exc:
                raise GoogleCalendarAuthError(
                    "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON"
                ) from exc
            if isinstance(parsed, dict):
                return "service_account", parsed
            raise GoogleCalendarAuthError("GOOGLE_SERVICE_ACCOUNT_JSON must decode to an object")

        if self._service_account_file:
            path = Path(self._service_account_file).expanduser()
            if not path.exists():
                raise GoogleCalendarAuthError(
                    f"GOOGLE_SERVICE_ACCOUNT_FILE not found: {path.as_posix()}"
                )
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                return "service_account", payload
            raise GoogleCalendarAuthError("GOOGLE_SERVICE_ACCOUNT_FILE must contain JSON object")

        if self._oauth_client_id and self._oauth_client_secret and self._oauth_refresh_token:
            return "oauth_refresh_token", None

        raise GoogleCalendarAuthError(
            "Google Calendar credentials missing. Set either "
            "(GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE) "
            "or (GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN)."
        )

    def _has_oauth_credentials(self) -> bool:
        """Return True when OAuth refresh-token credential set is complete."""

        return bool(
            self._oauth_client_id and self._oauth_client_secret and self._oauth_refresh_token
        )

    def _build_service(self, delegated_user: str | None, auth_mode: str | None = None):
        """Build googleapiclient discovery service with delegated credentials."""

        try:
            from google.oauth2.credentials import Credentials as UserCredentials
            from google.oauth2.service_account import Credentials as ServiceAccountCredentials
            from googleapiclient.discovery import build
        except ModuleNotFoundError as exc:
            raise GoogleCalendarAuthError(
                "google-auth and google-api-python-client are required for scheduling"
            ) from exc

        selected_auth_mode = auth_mode or self._auth_mode

        if selected_auth_mode == "service_account":
            if not isinstance(self._service_account_info, dict):
                raise GoogleCalendarAuthError("service-account payload not initialized")
            credentials = ServiceAccountCredentials.from_service_account_info(
                self._service_account_info,
                scopes=self._scopes,
            )
            if delegated_user:
                credentials = credentials.with_subject(delegated_user)
            return build("calendar", "v3", credentials=credentials, cache_discovery=False)

        if selected_auth_mode != "oauth_refresh_token":
            raise GoogleCalendarAuthError(f"unsupported google auth mode: {selected_auth_mode}")
        if not self._has_oauth_credentials():
            raise GoogleCalendarAuthError(
                "OAuth credentials are incomplete (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, "
                "GOOGLE_REFRESH_TOKEN required)"
            )
        credentials = UserCredentials(
            token=None,
            refresh_token=self._oauth_refresh_token,
            token_uri=self._oauth_token_uri,
            client_id=self._oauth_client_id,
            client_secret=self._oauth_client_secret,
            scopes=self._scopes,
        )
        return build("calendar", "v3", credentials=credentials, cache_discovery=False)

    def _list_busy_intervals_sync(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        delegated_user: str | None,
    ) -> list[CalendarBusyInterval]:
        """Blocking call to Google Calendar free/busy query."""

        service = self._build_service(delegated_user)
        body = {
            "timeMin": time_min.astimezone(timezone.utc).isoformat(),
            "timeMax": time_max.astimezone(timezone.utc).isoformat(),
            "items": [{"id": calendar_id}],
        }
        try:
            response = service.freebusy().query(body=body).execute()
        except Exception as exc:
            raise GoogleCalendarApiError(f"failed freebusy query for {calendar_id}") from exc

        calendars = response.get("calendars", {})
        if not isinstance(calendars, dict):
            return []
        details = calendars.get(calendar_id, {})
        if not isinstance(details, dict):
            return []
        busy_rows = details.get("busy", [])
        if not isinstance(busy_rows, list):
            return []

        intervals: list[CalendarBusyInterval] = []
        for item in busy_rows:
            if not isinstance(item, dict):
                continue
            start_raw = item.get("start")
            end_raw = item.get("end")
            if not isinstance(start_raw, str) or not isinstance(end_raw, str):
                continue
            start_at = self._parse_google_datetime(start_raw)
            end_at = self._parse_google_datetime(end_raw)
            if end_at <= start_at:
                continue
            intervals.append(CalendarBusyInterval(start_at=start_at, end_at=end_at))
        return intervals

    def _create_hold_event_sync(
        self,
        calendar_id: str,
        delegated_user: str | None,
        title: str,
        description: str,
        start_at: datetime,
        end_at: datetime,
        timezone_name: str,
        extended_private_properties: dict[str, str],
    ) -> CalendarHoldEvent:
        """Blocking call to create one opaque hold event."""

        service = self._build_service(delegated_user)
        event_body: dict[str, Any] = {
            "summary": title,
            "description": description,
            "start": {
                "dateTime": start_at.isoformat(),
                "timeZone": timezone_name,
            },
            "end": {
                "dateTime": end_at.isoformat(),
                "timeZone": timezone_name,
            },
            "transparency": "opaque",
        }
        if extended_private_properties:
            event_body["extendedProperties"] = {"private": extended_private_properties}
        try:
            response = (
                service.events()
                .insert(
                    calendarId=calendar_id,
                    body=event_body,
                    sendUpdates="none",
                )
                .execute()
            )
        except Exception as exc:
            raise GoogleCalendarApiError(f"failed to create hold event for {calendar_id}") from exc

        event_id = response.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise GoogleCalendarApiError("calendar hold event created without event id")

        html_link = response.get("htmlLink")
        return CalendarHoldEvent(
            event_id=event_id,
            html_link=html_link if isinstance(html_link, str) else None,
            start_at=start_at,
            end_at=end_at,
        )

    def _delete_event_sync(
        self,
        calendar_id: str,
        delegated_user: str | None,
        event_id: str,
    ) -> None:
        """Blocking call to delete one calendar event."""

        service = self._build_service(delegated_user)
        try:
            (
                service.events()
                .delete(calendarId=calendar_id, eventId=event_id, sendUpdates="none")
                .execute()
            )
        except Exception as exc:
            raise GoogleCalendarApiError(
                f"failed to delete hold event {event_id} for {calendar_id}"
            ) from exc

    def _confirm_hold_event_sync(
        self,
        calendar_id: str,
        delegated_user: str | None,
        event_id: str,
        title: str,
        description: str,
        attendee_emails: list[str],
        send_updates: str,
        extended_private_properties: dict[str, str],
    ) -> CalendarHoldEvent:
        """Blocking call to confirm hold event and add candidate attendee."""

        service = self._build_service(delegated_user)
        attendees: list[dict[str, str]] = []
        seen_emails: set[str] = set()
        for raw_email in attendee_emails:
            if not isinstance(raw_email, str) or "@" not in raw_email:
                continue
            normalized = raw_email.strip().lower()
            if not normalized or normalized in seen_emails:
                continue
            seen_emails.add(normalized)
            attendees.append({"email": normalized})
        event_body: dict[str, Any] = {
            "summary": title,
            "description": description,
            "attendees": attendees,
            "transparency": "opaque",
            "conferenceData": {
                "createRequest": {
                    "requestId": f"hireme-{uuid4()}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
        }
        if extended_private_properties:
            event_body["extendedProperties"] = {"private": extended_private_properties}

        def _patch_event(service_obj, *, body: dict[str, Any], send_updates_value: str):
            return (
                service_obj.events()
                .patch(
                    calendarId=calendar_id,
                    eventId=event_id,
                    body=body,
                    sendUpdates=send_updates_value,
                    conferenceDataVersion=1,
                )
                .execute()
            )

        try:
            response = _patch_event(service, body=event_body, send_updates_value=send_updates)
        except Exception as exc:
            primary_error_text = str(exc).lower()
            oauth_retry_response = None
            if self._auth_mode == "service_account" and self._has_oauth_credentials():
                try:
                    oauth_service = self._build_service(None, auth_mode="oauth_refresh_token")
                    oauth_retry_response = _patch_event(
                        oauth_service,
                        body=event_body,
                        send_updates_value=send_updates,
                    )
                except Exception:
                    oauth_retry_response = None

            if oauth_retry_response is not None:
                response = oauth_retry_response
            elif "invalid conference type value" in primary_error_text:
                # First retry: keep conferenceData but let Google choose solution key.
                fallback_body = dict(event_body)
                conference_data = fallback_body.get("conferenceData")
                if isinstance(conference_data, dict):
                    create_request = conference_data.get("createRequest")
                    if isinstance(create_request, dict):
                        create_request = dict(create_request)
                        create_request.pop("conferenceSolutionKey", None)
                        conference_data = dict(conference_data)
                        conference_data["createRequest"] = create_request
                        fallback_body["conferenceData"] = conference_data
                try:
                    response = _patch_event(
                        service,
                        body=fallback_body,
                        send_updates_value=send_updates,
                    )
                except Exception:
                    # Second retry: confirm without conferenceData (booking still succeeds).
                    no_conference_body = dict(event_body)
                    no_conference_body.pop("conferenceData", None)
                    try:
                        response = _patch_event(
                            service,
                            body=no_conference_body,
                            send_updates_value=send_updates,
                        )
                    except Exception as retry_exc:
                        raise GoogleCalendarApiError(
                            f"failed to confirm hold event {event_id} for {calendar_id}"
                        ) from retry_exc
            else:
                raise GoogleCalendarApiError(
                    f"failed to confirm hold event {event_id} for {calendar_id}"
                ) from exc

        response_event_id = response.get("id")
        if not isinstance(response_event_id, str) or not response_event_id.strip():
            raise GoogleCalendarApiError("calendar event confirmation returned invalid event id")

        start_at = self._extract_event_datetime(response.get("start"))
        end_at = self._extract_event_datetime(response.get("end"))
        if start_at is None or end_at is None:
            raise GoogleCalendarApiError("calendar event confirmation returned invalid time range")

        html_link = response.get("htmlLink")
        meeting_link = self._extract_meeting_link(response)
        return CalendarHoldEvent(
            event_id=response_event_id,
            html_link=html_link if isinstance(html_link, str) else None,
            start_at=start_at,
            end_at=end_at,
            meeting_link=meeting_link,
        )

    @staticmethod
    def _parse_google_datetime(value: str) -> datetime:
        """Parse Google RFC3339 datetime into timezone-aware UTC value."""

        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _extract_event_datetime(cls, payload: Any) -> datetime | None:
        """Extract RFC3339 dateTime from Google event start/end payload."""

        if not isinstance(payload, dict):
            return None
        raw = payload.get("dateTime")
        if not isinstance(raw, str):
            return None
        return cls._parse_google_datetime(raw)

    @staticmethod
    def _extract_meeting_link(payload: Any) -> str | None:
        """Extract Google Meet link from event payload when present."""

        if not isinstance(payload, dict):
            return None
        hangout_link = payload.get("hangoutLink")
        if isinstance(hangout_link, str) and hangout_link.strip():
            return hangout_link
        conference_data = payload.get("conferenceData")
        if not isinstance(conference_data, dict):
            return None
        entry_points = conference_data.get("entryPoints")
        if not isinstance(entry_points, list):
            return None
        for item in entry_points:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri")
            if isinstance(uri, str) and uri.strip():
                return uri
        return None
