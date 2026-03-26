"""JWT token utilities for admin route protection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
