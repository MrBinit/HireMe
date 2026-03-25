"""Schema models for candidate reference endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, EmailStr, Field


class ReferenceCreatePayload(BaseModel):
    """Create payload for one reference entry."""

    application_id: UUID
    candidate_email: EmailStr
    referee_name: str = Field(min_length=2, max_length=120)
    referee_email: EmailStr | None = None
    referee_phone: str | None = Field(default=None, max_length=50)
    referee_linkedin_url: AnyHttpUrl | None = None
    referee_company: str | None = Field(default=None, max_length=160)
    referee_position: str | None = Field(default=None, max_length=160)
    relationship: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=1000)


class ReferenceRecord(BaseModel):
    """Stored representation of one reference."""

    id: UUID
    application_id: UUID
    candidate_email: EmailStr
    referee_name: str
    referee_email: EmailStr | None = None
    referee_phone: str | None = None
    referee_linkedin_url: AnyHttpUrl | None = None
    referee_company: str | None = None
    referee_position: str | None = None
    relationship: str | None = None
    notes: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReferenceListResponse(BaseModel):
    """Paginated response model for reference records."""

    items: list[ReferenceRecord]
    total: int
    offset: int
    limit: int
