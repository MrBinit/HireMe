"""DocuSign e-signature integration for offer letter workflow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID
import xml.etree.ElementTree as ET

import anyio
import httpx
from jose import jwt

from app.core.runtime_config import DocusignRuntimeConfig


class DocusignApiError(RuntimeError):
    """Raised when DocuSign API call or webhook validation fails."""


@dataclass(frozen=True)
class DocusignEnvelopeDispatch:
    """Result payload returned after creating a DocuSign envelope."""

    envelope_id: str
    status: str


@dataclass(frozen=True)
class DocusignEnvelopeStatus:
    """Envelope status lookup payload from DocuSign."""

    envelope_id: str
    status: str


@dataclass(frozen=True)
class DocusignEnvelopeDocument:
    """Downloaded envelope document bundle payload."""

    envelope_id: str
    pdf_bytes: bytes


@dataclass(frozen=True)
class DocusignWebhookEvent:
    """Normalized DocuSign webhook event details."""

    envelope_id: str | None
    status: str
    raw: dict[str, Any] | str


class DocusignService:
    """Async service for sending offer-letter PDFs to DocuSign for signature."""

    def __init__(
        self,
        *,
        config: DocusignRuntimeConfig,
        access_token: str | None,
        integration_key: str | None,
        user_id: str | None,
        private_key: str | None,
        private_key_path: str | None,
        webhook_secret: str | None,
    ) -> None:
        """Initialize DocuSign runtime configuration and credentials."""

        self._config = config
        self._access_token = (access_token or "").strip()
        self._integration_key = (integration_key or "").strip()
        self._user_id = (user_id or "").strip()
        self._private_key = self._resolve_private_key(
            private_key=private_key,
            private_key_path=private_key_path,
        )
        self._webhook_secret = (webhook_secret or "").strip()
        self._token_lock = anyio.Lock()
        self._oauth_token: str | None = None
        self._oauth_token_expires_at: datetime | None = None

    @property
    def enabled(self) -> bool:
        """Return True when DocuSign is configured and ready to call."""

        has_oauth_credentials = bool(self._integration_key and self._user_id and self._private_key)
        return bool(
            self._config.enabled
            and (self._access_token or has_oauth_credentials)
            and self._config.account_id.strip()
            and self._config.webhook_url.strip()
        )

    async def send_offer_for_signature(
        self,
        *,
        application_id: UUID,
        candidate_name: str,
        candidate_email: str,
        role_title: str,
        pdf_bytes: bytes,
    ) -> DocusignEnvelopeDispatch:
        """Create and send one DocuSign envelope to candidate signer."""

        if not self.enabled:
            raise DocusignApiError("DocuSign is not configured")

        callback_url = self._build_webhook_url(application_id=application_id)
        envelope_subject = self._config.envelope_subject_template.format(role_title=role_title)
        payload: dict[str, Any] = {
            "emailSubject": envelope_subject,
            "emailBlurb": (
                f"Hi {candidate_name}, please review and sign your HireMe offer letter."
            ),
            "status": "sent",
            "documents": [
                {
                    "documentBase64": base64.b64encode(pdf_bytes).decode("ascii"),
                    "name": f"Offer Letter - {role_title}",
                    "fileExtension": "pdf",
                    "documentId": "1",
                    "transformPdfFields": "false",
                }
            ],
            "recipients": {
                "signers": [
                    {
                        "email": candidate_email,
                        "name": candidate_name,
                        "recipientId": "1",
                        "routingOrder": "1",
                        "tabs": {
                            "signHereTabs": [
                                {
                                    "documentId": "1",
                                    "pageNumber": self._config.sign_here_page_number,
                                    "xPosition": self._config.sign_here_x_position,
                                    "yPosition": self._config.sign_here_y_position,
                                }
                            ]
                        },
                    }
                ]
            },
        }
        if callback_url.lower().startswith("https://"):
            payload["eventNotification"] = {
                "url": callback_url,
                "loggingEnabled": "true",
                "requireAcknowledgment": "true",
                "envelopeEvents": [
                    {"envelopeEventStatusCode": "sent"},
                    {"envelopeEventStatusCode": "delivered"},
                    {"envelopeEventStatusCode": "completed"},
                    {"envelopeEventStatusCode": "declined"},
                    {"envelopeEventStatusCode": "voided"},
                ],
                "eventData": {
                    "version": "restv2.1",
                    "format": "json",
                },
            }

        endpoint = (
            f"{self._config.base_uri.rstrip('/')}/restapi/v2.1/accounts/"
            f"{self._config.account_id.strip()}/envelopes"
        )
        headers = {
            "Authorization": f"Bearer {await self._get_access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self._config.send_timeout_seconds) as client:
                response = await client.post(endpoint, headers=headers, json=payload)
        except Exception as exc:  # pragma: no cover - network/runtime failure
            raise DocusignApiError(f"failed to call DocuSign: {exc}") from exc

        body = self._safe_json(response)
        if response.status_code >= 400:
            raise DocusignApiError(
                f"DocuSign envelope create failed: {response.status_code} {self._truncate(str(body))}"
            )

        envelope_id = str(body.get("envelopeId") or "").strip()
        status = str(body.get("status") or "unknown").strip().lower()
        if not envelope_id:
            raise DocusignApiError("DocuSign response missing envelopeId")
        return DocusignEnvelopeDispatch(envelope_id=envelope_id, status=status)

    def validate_webhook_secret(self, *, token: str | None) -> None:
        """Validate shared webhook token when configured."""

        if not self._webhook_secret:
            return
        if (token or "").strip() != self._webhook_secret:
            raise DocusignApiError("invalid DocuSign webhook token")

    async def get_envelope_status(self, *, envelope_id: str) -> DocusignEnvelopeStatus:
        """Fetch one envelope status directly from DocuSign REST API."""

        if not self.enabled:
            raise DocusignApiError("DocuSign is not configured")
        normalized_id = (envelope_id or "").strip()
        if not normalized_id:
            raise DocusignApiError("DocuSign envelope id is required")

        endpoint = (
            f"{self._config.base_uri.rstrip('/')}/restapi/v2.1/accounts/"
            f"{self._config.account_id.strip()}/envelopes/{normalized_id}"
        )
        headers = {
            "Authorization": f"Bearer {await self._get_access_token()}",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self._config.send_timeout_seconds) as client:
                response = await client.get(endpoint, headers=headers)
        except Exception as exc:  # pragma: no cover - network/runtime failure
            raise DocusignApiError(f"failed to query DocuSign envelope status: {exc}") from exc

        body = self._safe_json(response)
        if response.status_code >= 400:
            raise DocusignApiError(
                "DocuSign envelope status fetch failed: "
                f"{response.status_code} {self._truncate(str(body))}"
            )
        status = self._normalize_status(str(body.get("status") or "").strip())
        resolved_id = str(body.get("envelopeId") or normalized_id).strip() or normalized_id
        return DocusignEnvelopeStatus(
            envelope_id=resolved_id,
            status=status,
        )

    async def download_completed_envelope_documents(
        self,
        *,
        envelope_id: str,
    ) -> DocusignEnvelopeDocument:
        """Download combined signed-envelope PDF bytes from DocuSign."""

        if not self.enabled:
            raise DocusignApiError("DocuSign is not configured")
        normalized_id = (envelope_id or "").strip()
        if not normalized_id:
            raise DocusignApiError("DocuSign envelope id is required")

        endpoint = (
            f"{self._config.base_uri.rstrip('/')}/restapi/v2.1/accounts/"
            f"{self._config.account_id.strip()}/envelopes/{normalized_id}/documents/combined"
        )
        headers = {
            "Authorization": f"Bearer {await self._get_access_token()}",
            "Accept": "application/pdf",
        }
        try:
            async with httpx.AsyncClient(timeout=self._config.send_timeout_seconds) as client:
                response = await client.get(endpoint, headers=headers)
        except Exception as exc:  # pragma: no cover - network/runtime failure
            raise DocusignApiError(
                f"failed to download DocuSign envelope documents: {exc}"
            ) from exc

        if response.status_code >= 400:
            body = self._safe_json(response)
            raise DocusignApiError(
                "DocuSign envelope document download failed: "
                f"{response.status_code} {self._truncate(str(body))}"
            )
        if not response.content:
            raise DocusignApiError("DocuSign envelope document download returned empty payload")
        return DocusignEnvelopeDocument(
            envelope_id=normalized_id, pdf_bytes=bytes(response.content)
        )

    def parse_webhook_event(
        self,
        *,
        raw_body: bytes,
        content_type: str | None,
    ) -> DocusignWebhookEvent:
        """Parse DocuSign webhook payload (JSON or XML) into normalized event."""

        mime = (content_type or "").casefold()
        body_text = raw_body.decode("utf-8", errors="replace").strip()
        if not body_text:
            raise DocusignApiError("empty DocuSign webhook payload")

        if "xml" in mime or body_text.startswith("<"):
            try:
                root = ET.fromstring(body_text)
            except ET.ParseError as exc:
                raise DocusignApiError("invalid DocuSign XML payload") from exc
            envelope_id = self._find_xml_text(root, "EnvelopeID")
            status = self._normalize_status(self._find_xml_text(root, "Status"))
            if status == "unknown":
                status = self._normalize_status(self._find_xml_text(root, "EnvelopeStatusCode"))
            return DocusignWebhookEvent(
                envelope_id=envelope_id,
                status=status,
                raw=body_text,
            )

        try:
            payload = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise DocusignApiError("invalid DocuSign webhook payload") from exc
        if not isinstance(payload, dict):
            raise DocusignApiError("invalid DocuSign webhook payload")

        envelope_id = self._extract_json_value(
            payload,
            keys=("envelopeId", "envelope_id"),
        )
        status_raw = self._extract_json_value(
            payload,
            keys=("status", "envelopeStatus", "envelope_status", "event"),
        )
        return DocusignWebhookEvent(
            envelope_id=envelope_id,
            status=self._normalize_status(status_raw),
            raw=payload,
        )

    async def _get_access_token(self) -> str:
        """Return DocuSign bearer token (static token or OAuth JWT grant)."""

        if self._access_token:
            return self._access_token
        return await self._get_oauth_access_token()

    async def _get_oauth_access_token(self) -> str:
        """Fetch and cache DocuSign OAuth access token using JWT assertion."""

        now = datetime.now(tz=timezone.utc)
        if (
            self._oauth_token
            and self._oauth_token_expires_at is not None
            and self._oauth_token_expires_at > now
        ):
            return self._oauth_token

        async with self._token_lock:
            now = datetime.now(tz=timezone.utc)
            if (
                self._oauth_token
                and self._oauth_token_expires_at is not None
                and self._oauth_token_expires_at > now
            ):
                return self._oauth_token

            if not self._integration_key or not self._user_id or not self._private_key:
                raise DocusignApiError("DocuSign OAuth credentials are incomplete")

            assertion = self._build_jwt_assertion(now=now)
            endpoint = f"{self._config.oauth_base_uri.rstrip('/')}/oauth/token"
            payload = {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            }
            headers = {"Content-Type": "application/x-www-form-urlencoded"}

            try:
                async with httpx.AsyncClient(timeout=self._config.oauth_timeout_seconds) as client:
                    response = await client.post(endpoint, data=payload, headers=headers)
            except Exception as exc:  # pragma: no cover - network/runtime failure
                raise DocusignApiError(f"failed to fetch DocuSign OAuth token: {exc}") from exc

            body = self._safe_json(response)
            if response.status_code >= 400:
                raise DocusignApiError(
                    f"DocuSign OAuth token request failed: {response.status_code} "
                    f"{self._truncate(str(body))}"
                )

            access_token = str(body.get("access_token") or "").strip()
            if not access_token:
                raise DocusignApiError("DocuSign OAuth response missing access_token")
            expires_in_raw = body.get("expires_in", 3600)
            try:
                expires_in = int(expires_in_raw)
            except (TypeError, ValueError):
                expires_in = 3600
            valid_for_seconds = max(60, expires_in - self._config.oauth_token_skew_seconds)
            self._oauth_token = access_token
            self._oauth_token_expires_at = now + timedelta(seconds=valid_for_seconds)
            return access_token

    def _build_jwt_assertion(self, *, now: datetime) -> str:
        """Build signed JWT assertion for DocuSign OAuth JWT grant."""

        aud = urlsplit(self._config.oauth_base_uri.strip()).netloc
        if not aud:
            aud = self._config.oauth_base_uri.strip().replace("https://", "").replace("http://", "")
        claims = {
            "iss": self._integration_key,
            "sub": self._user_id,
            "aud": aud,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=55)).timestamp()),
            "scope": "signature impersonation",
        }
        try:
            return str(jwt.encode(claims, self._private_key, algorithm="RS256"))
        except Exception as exc:
            raise DocusignApiError(f"failed to sign DocuSign JWT assertion: {exc}") from exc

    def _build_webhook_url(self, *, application_id: UUID) -> str:
        """Build webhook callback URL with application context and shared token."""

        parts = urlsplit(self._config.webhook_url.strip())
        query_pairs = parse_qsl(parts.query, keep_blank_values=True)
        query_pairs.append(("application_id", str(application_id)))
        if self._webhook_secret:
            query_pairs.append((self._config.webhook_secret_query_param, self._webhook_secret))
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(query_pairs),
                parts.fragment,
            )
        )

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict[str, Any]:
        """Parse response JSON payload and fallback to empty map."""

        try:
            payload = response.json()
        except Exception:
            return {}
        if isinstance(payload, dict):
            return payload
        return {}

    @staticmethod
    def _extract_json_value(payload: dict[str, Any], *, keys: tuple[str, ...]) -> str | None:
        """Find first matching key recursively inside nested JSON mapping."""

        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            if isinstance(value, dict):
                nested = DocusignService._extract_json_value(value, keys=keys)
                if nested:
                    return nested
        return None

    @staticmethod
    def _find_xml_text(root: ET.Element, local_name: str) -> str | None:
        """Find first XML element text by local tag name, namespace agnostic."""

        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] != local_name:
                continue
            if node.text and node.text.strip():
                return node.text.strip()
        return None

    @staticmethod
    def _normalize_status(value: str | None) -> str:
        """Normalize DocuSign status/event values to compact lower-case labels."""

        raw = (value or "").strip().casefold()
        if not raw:
            return "unknown"
        compact = raw.replace("_", "-").replace(" ", "-")
        if compact.startswith("envelope-"):
            compact = compact.removeprefix("envelope-")
        mapping = {
            "complete": "completed",
            "completed": "completed",
            "signed": "completed",
            "declined": "declined",
            "voided": "voided",
            "sent": "sent",
            "delivered": "delivered",
        }
        return mapping.get(compact, compact)

    @staticmethod
    def _truncate(value: str, limit: int = 300) -> str:
        """Truncate long strings for compact error payloads."""

        if len(value) <= limit:
            return value
        return value[:limit] + "..."

    @staticmethod
    def _resolve_private_key(*, private_key: str | None, private_key_path: str | None) -> str:
        """Resolve RSA private key from inline env var or file path."""

        inline = (private_key or "").strip()
        if inline:
            return inline.replace("\\n", "\n")
        path = (private_key_path or "").strip()
        if not path:
            return ""
        target = Path(path)
        candidates: list[Path] = [target]
        # Docker-style absolute path fallback for local development runs.
        if target.is_absolute() and len(target.parts) > 2 and target.parts[1] == "app":
            candidates.append(Path.cwd() / Path(*target.parts[2:]))
            candidates.append(Path.cwd() / target.name)
        try:
            for candidate in candidates:
                if not candidate.exists():
                    continue
                return candidate.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        return ""
