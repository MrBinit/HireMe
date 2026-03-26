"""JWT token utilities for admin route protection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from jose import JWTError, jwt

from app.core.runtime_config import SecurityRuntimeConfig


class TokenValidationError(ValueError):
    """Raised when a bearer token is invalid or expired."""


class AuthorizationError(ValueError):
    """Raised when token claims do not satisfy required authorization."""


@dataclass(frozen=True)
class AdminPrincipal:
    """Authenticated admin principal extracted from JWT claims."""

    subject: str
    role: str
    expires_at: datetime | None


@dataclass(frozen=True)
class InterviewConfirmationClaims:
    """Decoded claims used for candidate interview-slot confirmation."""

    application_id: UUID
    candidate_email: str
    option_number: int
    expires_at: datetime


@dataclass(frozen=True)
class InterviewActionClaims:
    """Decoded claims used for interview action links from emails."""

    application_id: UUID
    actor: str
    action: str
    option_number: int | None
    round_number: int | None
    candidate_email: str | None
    expires_at: datetime


def create_admin_access_token(
    *,
    subject: str,
    secret: str,
    config: SecurityRuntimeConfig,
    role: str | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a signed JWT access token for admin-protected endpoints."""

    now = datetime.now(tz=timezone.utc)
    ttl = expires_delta or timedelta(minutes=config.access_token_exp_minutes)
    expires_at = now + ttl

    claims = {
        "sub": subject,
        "role": role or config.required_role,
        "iss": config.issuer,
        "aud": config.audience,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(claims, secret, algorithm=config.jwt_algorithm)


def create_interview_confirmation_token(
    *,
    application_id: UUID,
    candidate_email: str,
    option_number: int,
    expires_at: datetime,
    secret: str,
    config: SecurityRuntimeConfig,
) -> str:
    """Create signed token for candidate slot-confirm links."""

    now = datetime.now(tz=timezone.utc)
    normalized_expiry = expires_at.astimezone(timezone.utc)
    claims = {
        "sub": str(application_id),
        "type": "interview_confirm",
        "email": candidate_email.strip().casefold(),
        "opt": int(option_number),
        "iss": config.issuer,
        "aud": f"{config.audience}:interview-confirm",
        "iat": int(now.timestamp()),
        "exp": int(normalized_expiry.timestamp()),
    }
    return jwt.encode(claims, secret, algorithm=config.jwt_algorithm)


def decode_admin_access_token(
    *,
    token: str,
    secret: str,
    config: SecurityRuntimeConfig,
    required_role: str | None = None,
) -> AdminPrincipal:
    """Decode and validate JWT, returning the authenticated admin principal."""

    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[config.jwt_algorithm],
            audience=config.audience,
            issuer=config.issuer,
            options={"leeway": config.leeway_seconds},
        )
    except JWTError as exc:
        raise TokenValidationError("invalid or expired bearer token") from exc

    subject = claims.get("sub")
    role = claims.get("role")
    expires_epoch = claims.get("exp")

    if not isinstance(subject, str) or not subject.strip():
        raise TokenValidationError("token missing subject claim")
    if not isinstance(role, str) or not role.strip():
        raise TokenValidationError("token missing role claim")
    expected_role = required_role or config.required_role
    if role != expected_role:
        raise AuthorizationError("insufficient role")
    if not isinstance(expires_epoch, int):
        raise TokenValidationError("token missing exp claim")

    expires_at = datetime.fromtimestamp(expires_epoch, tz=timezone.utc)
    return AdminPrincipal(subject=subject, role=role, expires_at=expires_at)


def decode_interview_confirmation_token(
    *,
    token: str,
    secret: str,
    config: SecurityRuntimeConfig,
) -> InterviewConfirmationClaims:
    """Decode and validate candidate interview confirmation token."""

    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[config.jwt_algorithm],
            audience=f"{config.audience}:interview-confirm",
            issuer=config.issuer,
            options={"leeway": config.leeway_seconds},
        )
    except JWTError as exc:
        raise TokenValidationError("invalid or expired interview confirmation token") from exc

    claim_type = claims.get("type")
    raw_application_id = claims.get("sub")
    raw_email = claims.get("email")
    raw_option_number = claims.get("opt")
    expires_epoch = claims.get("exp")

    if claim_type != "interview_confirm":
        raise TokenValidationError("invalid interview confirmation token type")
    if not isinstance(raw_application_id, str) or not raw_application_id.strip():
        raise TokenValidationError("interview confirmation token missing application id")
    if not isinstance(raw_email, str) or not raw_email.strip():
        raise TokenValidationError("interview confirmation token missing candidate email")
    if not isinstance(raw_option_number, int) or raw_option_number <= 0:
        raise TokenValidationError("interview confirmation token missing option number")
    if not isinstance(expires_epoch, int):
        raise TokenValidationError("interview confirmation token missing exp claim")

    try:
        application_id = UUID(raw_application_id)
    except ValueError as exc:
        raise TokenValidationError(
            "interview confirmation token has invalid application id"
        ) from exc

    return InterviewConfirmationClaims(
        application_id=application_id,
        candidate_email=raw_email.strip().casefold(),
        option_number=raw_option_number,
        expires_at=datetime.fromtimestamp(expires_epoch, tz=timezone.utc),
    )


def create_interview_action_token(
    *,
    application_id: UUID,
    actor: str,
    action: str,
    expires_at: datetime,
    secret: str,
    config: SecurityRuntimeConfig,
    option_number: int | None = None,
    round_number: int | None = None,
    candidate_email: str | None = None,
) -> str:
    """Create signed token for interview action links (reschedule/reject/accept)."""

    now = datetime.now(tz=timezone.utc)
    normalized_expiry = expires_at.astimezone(timezone.utc)
    claims: dict[str, object] = {
        "sub": str(application_id),
        "type": "interview_action",
        "actor": actor.strip().lower(),
        "action": action.strip().lower(),
        "iss": config.issuer,
        "aud": f"{config.audience}:interview-action",
        "iat": int(now.timestamp()),
        "exp": int(normalized_expiry.timestamp()),
    }
    if isinstance(option_number, int) and option_number > 0:
        claims["opt"] = option_number
    if isinstance(round_number, int) and round_number >= 1:
        claims["round"] = round_number
    if isinstance(candidate_email, str) and candidate_email.strip():
        claims["email"] = candidate_email.strip().casefold()
    return jwt.encode(claims, secret, algorithm=config.jwt_algorithm)


def decode_interview_action_token(
    *,
    token: str,
    secret: str,
    config: SecurityRuntimeConfig,
) -> InterviewActionClaims:
    """Decode and validate signed interview action token."""

    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[config.jwt_algorithm],
            audience=f"{config.audience}:interview-action",
            issuer=config.issuer,
            options={"leeway": config.leeway_seconds},
        )
    except JWTError as exc:
        raise TokenValidationError("invalid or expired interview action token") from exc

    claim_type = claims.get("type")
    raw_application_id = claims.get("sub")
    raw_actor = claims.get("actor")
    raw_action = claims.get("action")
    raw_option_number = claims.get("opt")
    raw_round_number = claims.get("round")
    raw_email = claims.get("email")
    expires_epoch = claims.get("exp")

    if claim_type != "interview_action":
        raise TokenValidationError("invalid interview action token type")
    if not isinstance(raw_application_id, str) or not raw_application_id.strip():
        raise TokenValidationError("interview action token missing application id")
    if not isinstance(raw_actor, str) or not raw_actor.strip():
        raise TokenValidationError("interview action token missing actor")
    if not isinstance(raw_action, str) or not raw_action.strip():
        raise TokenValidationError("interview action token missing action")
    if raw_option_number is not None and (
        not isinstance(raw_option_number, int) or raw_option_number <= 0
    ):
        raise TokenValidationError("interview action token has invalid option number")
    if raw_round_number is not None and (
        not isinstance(raw_round_number, int) or raw_round_number <= 0
    ):
        raise TokenValidationError("interview action token has invalid round number")
    if raw_email is not None and (not isinstance(raw_email, str) or not raw_email.strip()):
        raise TokenValidationError("interview action token has invalid candidate email")
    if not isinstance(expires_epoch, int):
        raise TokenValidationError("interview action token missing exp claim")

    try:
        application_id = UUID(raw_application_id)
    except ValueError as exc:
        raise TokenValidationError("interview action token has invalid application id") from exc

    return InterviewActionClaims(
        application_id=application_id,
        actor=raw_actor.strip().lower(),
        action=raw_action.strip().lower(),
        option_number=raw_option_number if isinstance(raw_option_number, int) else None,
        round_number=raw_round_number if isinstance(raw_round_number, int) else None,
        candidate_email=raw_email.strip().casefold() if isinstance(raw_email, str) else None,
        expires_at=datetime.fromtimestamp(expires_epoch, tz=timezone.utc),
    )
