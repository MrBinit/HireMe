"""Slack API integration for onboarding invite and messaging workflow."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import time
from typing import Any, Literal, Mapping

import anyio
import httpx

from app.core.runtime_config import SlackRuntimeConfig


class SlackApiError(RuntimeError):
    """Raised when Slack API call or event validation fails."""


@dataclass(frozen=True)
class SlackInviteResult:
    """Normalized result payload after invite attempt."""

    status: str
    user_id: str | None = None
    raw_error: str | None = None


class SlackService:
    """Async service for Slack onboarding operations."""

    def __init__(
        self,
        *,
        config: SlackRuntimeConfig,
        bot_token: str | None,
        admin_user_token: str | None,
        signing_secret: str | None,
        client_id: str | None = None,
        client_secret: str | None = None,
        bot_refresh_token: str | None = None,
        admin_refresh_token: str | None = None,
    ) -> None:
        """Initialize Slack runtime configuration and credentials."""

        self._config = config
        self._bot_token = (bot_token or "").strip()
        self._admin_user_token = (admin_user_token or "").strip()
        self._signing_secret = (signing_secret or "").strip()
        self._client_id = (client_id or "").strip()
        self._client_secret = (client_secret or "").strip()
        self._bot_refresh_token = (bot_refresh_token or "").strip()
        self._admin_refresh_token = (admin_refresh_token or "").strip()
        self._token_lock = anyio.Lock()

    @property
    def enabled(self) -> bool:
        """Return True when Slack workflow is configured and enabled."""

        has_bot_auth = bool(self._bot_token) or self._can_refresh("bot")
        has_invite_auth = (
            bool(self._admin_user_token)
            or self._can_refresh("admin")
            or has_bot_auth
        )
        has_signature_config = bool(
            not self._config.verify_event_signature or self._signing_secret
        )
        return bool(
            self._config.enabled
            and has_bot_auth
            and has_invite_auth
            and has_signature_config
        )

    @property
    def onboarding_resource_links(self) -> list[str]:
        """Return configured onboarding resource links."""

        return list(self._config.onboarding_resource_links)

    def validate_event_signature(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
    ) -> None:
        """Validate Slack signing secret for incoming events."""

        if not self._config.verify_event_signature:
            return
        if not self._signing_secret:
            raise SlackApiError("Slack signing secret is not configured")

        timestamp = (headers.get("x-slack-request-timestamp") or "").strip()
        signature = (headers.get("x-slack-signature") or "").strip()
        if not timestamp or not signature:
            raise SlackApiError("missing Slack signature headers")
        try:
            timestamp_int = int(timestamp)
        except ValueError as exc:
            raise SlackApiError("invalid Slack signature timestamp") from exc

        now = int(time.time())
        ttl = max(1, int(self._config.signature_ttl_seconds))
        if abs(now - timestamp_int) > ttl:
            raise SlackApiError("expired Slack signature timestamp")

        sig_basestring = b"v0:" + timestamp.encode("utf-8") + b":" + raw_body
        digest = hmac.new(
            self._signing_secret.encode("utf-8"),
            sig_basestring,
            hashlib.sha256,
        ).hexdigest()
        expected = f"v0={digest}"
        if not hmac.compare_digest(expected, signature):
            raise SlackApiError("invalid Slack event signature")

    @staticmethod
    def parse_event_payload(*, raw_body: bytes) -> dict[str, Any]:
        """Parse Slack event payload into JSON mapping."""

        body_text = raw_body.decode("utf-8", errors="replace").strip()
        if not body_text:
            raise SlackApiError("empty Slack event payload")
        try:
            payload = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise SlackApiError("invalid Slack event payload") from exc
        if not isinstance(payload, dict):
            raise SlackApiError("invalid Slack event payload")
        return payload

    async def lookup_user_by_email(self, *, email: str) -> str | None:
        """Look up Slack user id by email; return None when not found."""

        response = await self._call_api(
            "users.lookupByEmail",
            token_kind="bot",
            params={"email": email},
        )
        if response.get("ok") is True:
            user = response.get("user")
            if isinstance(user, dict):
                user_id = user.get("id")
                if isinstance(user_id, str) and user_id.strip():
                    return user_id.strip()
            return None
        if str(response.get("error") or "") == "users_not_found":
            return None
        raise SlackApiError(f"Slack users.lookupByEmail failed: {response.get('error')}")

    async def invite_candidate(
        self,
        *,
        candidate_email: str,
        candidate_name: str,
        role_title: str,
    ) -> SlackInviteResult:
        """Invite candidate into workspace (or detect existing membership)."""

        if not self.enabled:
            raise SlackApiError("Slack onboarding is not configured")

        existing_user_id = await self.lookup_user_by_email(email=candidate_email)
        if existing_user_id:
            return SlackInviteResult(status="already_in_workspace", user_id=existing_user_id)
        if not (self._admin_user_token or self._can_refresh("admin")):
            raise SlackApiError(
                "Slack admin invite token is missing. "
                "Set SLACK_ADMIN_USER_TOKEN or SLACK_ADMIN_REFRESH_TOKEN."
            )

        invite_message = self._config.invite_custom_message_template.format(
            candidate_name=candidate_name,
            role_title=role_title,
        )
        channel_ids = [item.strip() for item in self._config.invite_channel_ids if item.strip()]

        # Preferred endpoint for modern admin tokens.
        admin_payload: dict[str, Any] = {
            "email": candidate_email,
            "real_name": candidate_name,
            "custom_message": invite_message,
            "resend": True,
        }
        if self._config.invite_team_id.strip():
            admin_payload["team_id"] = self._config.invite_team_id.strip()
        if channel_ids:
            admin_payload["channel_ids"] = channel_ids

        admin_response = await self._call_api(
            "admin.users.invite",
            token_kind="admin",
            json_payload=admin_payload,
        )
        if admin_response.get("ok") is True:
            return SlackInviteResult(status="invited")
        admin_error = str(admin_response.get("error") or "")
        if admin_error in {"already_invited", "already_in_team", "already_in_workspace"}:
            return SlackInviteResult(status=admin_error)

        # Compatibility fallback for legacy workspace admin endpoint.
        legacy_payload: dict[str, Any] = {
            "email": candidate_email,
            "real_name": candidate_name,
            "resend": True,
        }
        if invite_message:
            legacy_payload["custom_message"] = invite_message
        if channel_ids:
            legacy_payload["channels"] = ",".join(channel_ids)
        legacy_response = await self._call_api(
            "users.admin.invite",
            token_kind="admin",
            json_payload=legacy_payload,
        )
        if legacy_response.get("ok") is True:
            return SlackInviteResult(status="invited")
        legacy_error = str(legacy_response.get("error") or "")
        if legacy_error in {"already_invited", "already_in_team", "already_in_workspace"}:
            return SlackInviteResult(status=legacy_error)
        if (
            admin_error == "not_allowed_token_type"
            or legacy_error == "not_allowed_token_type"
        ):
            raise SlackApiError(
                "Slack token type is not allowed for invite API. "
                "Use a workspace admin user token in SLACK_ADMIN_USER_TOKEN."
            )
        raise SlackApiError(
            "Slack invite failed: "
            f"admin.users.invite={admin_error or 'unknown'} "
            f"users.admin.invite={legacy_error or 'unknown'}"
        )

    async def send_direct_message(self, *, user_id: str, text: str) -> None:
        """Send one direct message to the given Slack user id."""

        response = await self._call_api(
            "chat.postMessage",
            token_kind="bot",
            json_payload={
                "channel": user_id,
                "text": text,
            },
        )
        if response.get("ok") is not True:
            raise SlackApiError(f"Slack DM failed: {response.get('error')}")

    async def notify_hr_channel(self, *, text: str) -> None:
        """Post onboarding update to HR channel, when configured."""

        channel_id = self._config.hr_channel_id.strip()
        if not channel_id:
            return
        response = await self._call_api(
            "chat.postMessage",
            token_kind="bot",
            json_payload={
                "channel": channel_id,
                "text": text,
            },
        )
        if response.get("ok") is not True:
            raise SlackApiError(f"Slack HR notification failed: {response.get('error')}")

    async def _call_api(
        self,
        endpoint: str,
        *,
        token_kind: Literal["bot", "admin"],
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call Slack API endpoint and parse JSON response."""

        token = await self._get_access_token(token_kind)
        payload = await self._send_request(
            endpoint=endpoint,
            token=token,
            params=params,
            json_payload=json_payload,
        )

        error = str(payload.get("error") or "")
        if error not in {"token_expired", "invalid_auth"}:
            return payload
        if not self._can_refresh(token_kind):
            return payload

        try:
            await self._refresh_access_token(token_kind=token_kind)
        except SlackApiError:
            return payload

        refreshed_token = await self._get_access_token(token_kind)
        return await self._send_request(
            endpoint=endpoint,
            token=refreshed_token,
            params=params,
            json_payload=json_payload,
        )

    async def _send_request(
        self,
        *,
        endpoint: str,
        token: str,
        params: dict[str, Any] | None,
        json_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Issue one HTTP request to Slack API and parse payload."""

        if not token:
            raise SlackApiError("missing Slack token")

        url = f"{self._config.api_base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if json_payload is not None:
            headers["Content-Type"] = "application/json"

        try:
            async with httpx.AsyncClient(timeout=self._config.send_timeout_seconds) as client:
                response = await client.post(
                    url,
                    headers=headers,
                    params=params,
                    json=json_payload,
                )
        except Exception as exc:  # pragma: no cover - network/runtime failure
            raise SlackApiError(f"failed to call Slack API: {exc}") from exc

        try:
            payload = response.json()
        except Exception as exc:
            raise SlackApiError("Slack API response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise SlackApiError("Slack API response is not a JSON object")
        return payload

    async def _get_access_token(self, token_kind: Literal["bot", "admin"]) -> str:
        """Return active access token, refreshing when needed and configured."""

        current = (
            self._bot_token
            if token_kind == "bot"
            else (self._admin_user_token or self._bot_token)
        )
        if current:
            return current
        if self._can_refresh(token_kind):
            await self._refresh_access_token(token_kind=token_kind)
            refreshed = (
                self._bot_token
                if token_kind == "bot"
                else (self._admin_user_token or self._bot_token)
            )
            if refreshed:
                return refreshed
        raise SlackApiError("missing Slack token")

    def _can_refresh(self, token_kind: Literal["bot", "admin"]) -> bool:
        """Return True when OAuth token refresh credentials are configured."""

        has_client = bool(self._client_id and self._client_secret)
        if token_kind == "bot":
            return bool(has_client and self._bot_refresh_token)
        return bool(has_client and (self._admin_refresh_token or self._bot_refresh_token))

    async def _refresh_access_token(self, *, token_kind: Literal["bot", "admin"]) -> None:
        """Refresh Slack access token in-memory via oauth.v2.access."""

        refresh_token = (
            self._bot_refresh_token
            if token_kind == "bot"
            else (self._admin_refresh_token or self._bot_refresh_token)
        )
        if not refresh_token:
            raise SlackApiError("missing Slack refresh token")
        if not self._client_id or not self._client_secret:
            raise SlackApiError("Slack client credentials are missing")

        async with self._token_lock:
            endpoint = f"{self._config.api_base_url.rstrip('/')}/oauth.v2.access"
            payload = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
            try:
                async with httpx.AsyncClient(timeout=self._config.send_timeout_seconds) as client:
                    response = await client.post(endpoint, data=payload)
            except Exception as exc:  # pragma: no cover - network/runtime failure
                raise SlackApiError(f"failed to refresh Slack token: {exc}") from exc

            try:
                body = response.json()
            except Exception as exc:
                raise SlackApiError("Slack OAuth response is not valid JSON") from exc
            if not isinstance(body, dict):
                raise SlackApiError("Slack OAuth response is not a JSON object")
            if body.get("ok") is not True:
                raise SlackApiError(
                    f"Slack OAuth refresh failed: {body.get('error') or 'unknown'}"
                )

            access_token = str(body.get("access_token") or "").strip()
            if not access_token:
                raise SlackApiError("Slack OAuth response missing access_token")
            next_refresh_token = str(body.get("refresh_token") or "").strip()

            if token_kind == "bot":
                self._bot_token = access_token
                if next_refresh_token:
                    self._bot_refresh_token = next_refresh_token
                return

            self._admin_user_token = access_token
            if next_refresh_token:
                self._admin_refresh_token = next_refresh_token
