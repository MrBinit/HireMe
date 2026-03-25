"""Tests for admin authentication service behavior."""

from passlib.context import CryptContext

from app.core.runtime_config import SecurityRuntimeConfig
from app.schemas.auth import AdminLoginPayload
from app.services.admin_auth_service import (
    AdminAuthConfigurationError,
    AdminAuthError,
    AdminAuthService,
)


def _security_config() -> SecurityRuntimeConfig:
    """Return deterministic security config for auth tests."""

    return SecurityRuntimeConfig(
        enabled=True,
        jwt_algorithm="HS256",
        required_role="admin",
        issuer="hireme-backend",
        audience="hireme-admin",
        access_token_exp_minutes=15,
        leeway_seconds=5,
    )


def test_admin_login_with_plaintext_password() -> None:
    """Valid username/password should return bearer token response."""

    service = AdminAuthService(
        admin_username="admin",
        admin_password="StrongSecret123!",
        admin_password_hash=None,
        jwt_secret="this-is-a-long-test-secret-value-123456",
        security_config=_security_config(),
    )

    response = service.login(
        AdminLoginPayload(username="admin", password="StrongSecret123!"),
    )

    assert response.token_type == "bearer"
    assert response.role == "admin"
    assert response.access_token
    assert response.expires_at is not None


def test_admin_login_with_password_hash() -> None:
    """Valid password should authenticate against configured password hash."""

    context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
    password_hash = context.hash("MySecret123!")
    service = AdminAuthService(
        admin_username="admin",
        admin_password=None,
        admin_password_hash=password_hash,
        jwt_secret="this-is-a-long-test-secret-value-123456",
        security_config=_security_config(),
    )

    response = service.login(
        AdminLoginPayload(username="admin", password="MySecret123!"),
    )

    assert response.access_token
    assert response.role == "admin"


def test_admin_login_rejects_invalid_credentials() -> None:
    """Wrong username or password should fail with auth error."""

    service = AdminAuthService(
        admin_username="admin",
        admin_password="StrongSecret123!",
        admin_password_hash=None,
        jwt_secret="this-is-a-long-test-secret-value-123456",
        security_config=_security_config(),
    )

    error = None
    try:
        service.login(AdminLoginPayload(username="admin", password="wrong-password"))
    except AdminAuthError as exc:
        error = exc

    assert error is not None
    assert "invalid admin credentials" in str(error)


def test_admin_login_fails_when_configuration_missing() -> None:
    """Missing admin credentials or JWT secret should fail fast."""

    service = AdminAuthService(
        admin_username=None,
        admin_password=None,
        admin_password_hash=None,
        jwt_secret=None,
        security_config=_security_config(),
    )

    error = None
    try:
        service.login(AdminLoginPayload(username="admin", password="Secret123!"))
    except AdminAuthConfigurationError as exc:
        error = exc

    assert error is not None
