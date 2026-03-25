"""Tests for JWT creation/validation and authorization checks."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.runtime_config import SecurityRuntimeConfig
from app.core.security import (
    AuthorizationError,
    TokenValidationError,
    create_admin_access_token,
    decode_admin_access_token,
)


def _security_config() -> SecurityRuntimeConfig:
    """Build deterministic security config for tests."""

    return SecurityRuntimeConfig(
        enabled=True,
        jwt_algorithm="HS256",
        required_role="admin",
        issuer="hireme-backend",
        audience="hireme-admin",
        access_token_exp_minutes=60,
        leeway_seconds=0,
    )


def test_create_and_decode_admin_jwt_round_trip() -> None:
    """Valid token should decode into an admin principal."""

    config = _security_config()
    token = create_admin_access_token(
        subject="admin-user",
        secret="unit-test-secret",
        config=config,
    )
    principal = decode_admin_access_token(
        token=token,
        secret="unit-test-secret",
        config=config,
    )

    assert principal.subject == "admin-user"
    assert principal.role == "admin"
    assert principal.expires_at is not None


def test_decode_rejects_wrong_role_claim() -> None:
    """Token with non-admin role should fail authorization."""

    config = _security_config()
    token = create_admin_access_token(
        subject="admin-user",
        secret="unit-test-secret",
        config=config,
        role="viewer",
    )

    with pytest.raises(AuthorizationError):
        decode_admin_access_token(
            token=token,
            secret="unit-test-secret",
            config=config,
        )


def test_decode_rejects_expired_token() -> None:
    """Expired token should fail validation."""

    config = _security_config()
    token = create_admin_access_token(
        subject="admin-user",
        secret="unit-test-secret",
        config=config,
        expires_delta=timedelta(seconds=-1),
    )

    with pytest.raises(TokenValidationError):
        decode_admin_access_token(
            token=token,
            secret="unit-test-secret",
            config=config,
        )
