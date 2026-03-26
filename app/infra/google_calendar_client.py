"""Google Calendar API client used for interviewer slot orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

    def _build_service(self, delegated_user: str | None):
        """Build googleapiclient discovery service with delegated credentials."""

        try:
            from google.oauth2.credentials import Credentials as UserCredentials
            from google.oauth2.service_account import Credentials as ServiceAccountCredentials
            from googleapiclient.discovery import build
        except ModuleNotFoundError as exc:
            raise GoogleCalendarAuthError(
                "google-auth and google-api-python-client are required for scheduling"
            ) from exc

        if self._auth_mode == "service_account":
            if not isinstance(self._service_account_info, dict):
                raise GoogleCalendarAuthError("service-account payload not initialized")
            credentials = ServiceAccountCredentials.from_service_account_info(
                self._service_account_info,
                scopes=self._scopes,
            )
            if delegated_user:
                credentials = credentials.with_subject(delegated_user)
            return build("calendar", "v3", credentials=credentials, cache_discovery=False)

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
