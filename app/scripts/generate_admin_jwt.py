"""Generate admin JWT access token from local runtime configuration."""

from __future__ import annotations

import argparse
from datetime import timedelta

from app.core.runtime_config import get_runtime_config
from app.core.security import create_admin_access_token
from app.core.settings import get_settings


def _build_parser() -> argparse.ArgumentParser:
    """Build CLI parser for admin-token generation."""

    parser = argparse.ArgumentParser(description="Generate admin JWT token for HireMe API")
    parser.add_argument("--subject", default="hireme-admin", help="JWT subject value")
    parser.add_argument(
        "--expires-minutes",
        type=int,
        default=None,
        help="Token TTL in minutes (defaults to security.access_token_exp_minutes)",
    )
    return parser


def main() -> None:
    """Read env/config and print a signed admin bearer token."""

    args = _build_parser().parse_args()
    settings = get_settings()
    runtime_config = get_runtime_config()

    if not settings.admin_jwt_secret:
        raise RuntimeError("ADMIN_JWT_SECRET is required in .env")

    ttl = None
    if args.expires_minutes is not None:
        ttl = timedelta(minutes=max(1, args.expires_minutes))

    token = create_admin_access_token(
        subject=args.subject,
        secret=settings.admin_jwt_secret,
        config=runtime_config.security,
        expires_delta=ttl,
    )
    print(token)


if __name__ == "__main__":
    from app.scripts.error import run_script_entrypoint

    raise SystemExit(run_script_entrypoint(main))
