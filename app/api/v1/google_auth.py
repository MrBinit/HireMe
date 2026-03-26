"""Temporary Google OAuth helper routes for obtaining refresh tokens."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from app.core.runtime_config import get_runtime_config
from app.core.settings import get_settings

router = APIRouter(tags=["google-auth"])

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_CALLBACK_PATH = "/api/v1/auth/google/callback"


def _build_callback_redirect_uri(request: Request) -> str:
    """Build absolute callback URI using fixed versioned callback path."""

    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}{_GOOGLE_CALLBACK_PATH}"


@router.get("/auth/google/login")
async def google_oauth_login(
    request: Request,
    state: str | None = Query(default=None, min_length=1, max_length=500),
) -> RedirectResponse:
    """Start OAuth flow and redirect to Google consent page."""

    settings = get_settings()
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required to start Google OAuth."
            ),
        )

    redirect_uri = _build_callback_redirect_uri(request)
    params: dict[str, str] = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/calendar",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if state:
        params["state"] = state

    auth_url = f"{_GOOGLE_AUTH_URL}?{urlencode(params)}"
    return RedirectResponse(url=auth_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/auth/google/callback", name="google_oauth_callback")
async def google_oauth_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
) -> dict[str, Any]:
    """Exchange Google auth code for tokens and return refresh token payload."""

    if error:
        detail = error_description or error
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
    if not code:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="missing authorization code",
        )

    settings = get_settings()
    runtime_config = get_runtime_config()
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required",
        )

    redirect_uri = _build_callback_redirect_uri(request)
    token_payload = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                runtime_config.google_api.token_uri,
                data=token_payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"failed to call Google token endpoint: {exc}",
        ) from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"google token exchange failed: {response.text}",
        )

    payload = response.json()
    refresh_token = payload.get("refresh_token")
    return {
        "message": (
            "Google OAuth success. Save refresh_token into GOOGLE_REFRESH_TOKEN in .env."
            if isinstance(refresh_token, str) and refresh_token.strip()
            else (
                "Google OAuth success, but no refresh_token returned. "
                "Revoke app access in Google Account and retry via /api/v1/auth/google/login."
            )
        ),
        "refresh_token": refresh_token,
        "token_type": payload.get("token_type"),
        "scope": payload.get("scope"),
        "expires_in": payload.get("expires_in"),
        "state": state,
        "redirect_uri_used": redirect_uri,
    }
