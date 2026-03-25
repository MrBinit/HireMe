"""Schema models for admin authentication endpoints."""

from datetime import datetime

from pydantic import BaseModel, Field


class AdminLoginPayload(BaseModel):
    """Request body for admin login."""

    username: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=8, max_length=256)


class AdminAccessTokenResponse(BaseModel):
    """Response body with JWT token for admin routes."""

    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    role: str
