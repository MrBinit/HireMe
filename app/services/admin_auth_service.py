"""Service for admin credential verification and JWT issuance."""

from __future__ import annotations

import secrets

from passlib.context import CryptContext

from app.core.runtime_config import SecurityRuntimeConfig
from app.core.security import create_admin_access_token, decode_admin_access_token
from app.schemas.auth import AdminAccessTokenResponse, AdminLoginPayload

_PASSWORD_CONTEXT = CryptContext(
    schemes=["pbkdf2_sha256", "bcrypt"],
    deprecated="auto",
)


class AdminAuthError(ValueError):
    """Raised when submitted admin credentials are invalid."""


class AdminAuthConfigurationError(RuntimeError):
    """Raised when admin auth runtime/env configuration is incomplete."""


class AdminAuthService:
    """Authenticate admin users and mint access tokens."""

    def __init__(
        self,
        *,
        admin_username: str | None,
        admin_password: str | None,
        admin_password_hash: str | None,
        jwt_secret: str | None,
        security_config: SecurityRuntimeConfig,
        auth_role: str | None = None,
        auth_label: str = "admin",
    ):
        """Store auth settings and security token config."""

        self._admin_username = admin_username
        self._admin_password = admin_password
        self._admin_password_hash = admin_password_hash
        self._jwt_secret = jwt_secret
        self._security_config = security_config
        self._auth_role = auth_role or security_config.required_role
        self._auth_label = auth_label.strip().lower() or "admin"

    def login(self, payload: AdminLoginPayload) -> AdminAccessTokenResponse:
        """Validate admin credentials and return signed bearer token."""

        expected_username = (self._admin_username or "").strip()
        submitted_username = payload.username.strip()

        if not expected_username:
            raise AdminAuthConfigurationError(f"{self._auth_label.upper()}_USERNAME is not configured")
        if not self._jwt_secret:
            raise AdminAuthConfigurationError("ADMIN_JWT_SECRET is not configured")
        if not (self._admin_password_hash or self._admin_password):
            raise AdminAuthConfigurationError(
                f"Configure {self._auth_label.upper()}_PASSWORD_HASH or {self._auth_label.upper()}_PASSWORD"
            )

        username_match = secrets.compare_digest(
            submitted_username.casefold(),
            expected_username.casefold(),
        )
        password_match = self._verify_password(payload.password)

        if not (username_match and password_match):
            raise AdminAuthError(f"invalid {self._auth_label} credentials")

        token = create_admin_access_token(
            subject=submitted_username,
            secret=self._jwt_secret,
            config=self._security_config,
            role=self._auth_role,
        )
        principal = decode_admin_access_token(
            token=token,
            secret=self._jwt_secret,
            config=self._security_config,
            required_role=self._auth_role,
        )
        return AdminAccessTokenResponse(
            access_token=token,
            expires_at=principal.expires_at,
            role=principal.role,
        )

    def _verify_password(self, submitted_password: str) -> bool:
        """Verify password from either bcrypt hash or plaintext env fallback."""

        if self._admin_password_hash:
            return bool(_PASSWORD_CONTEXT.verify(submitted_password, self._admin_password_hash))
        if self._admin_password is None:
            return False
        return secrets.compare_digest(submitted_password, self._admin_password)
