"""Schema models for job opening payloads and responses."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

JobOpeningStatus = Literal["open", "closed", "paused"]


class JobOpeningCreatePayload(BaseModel):
    """Create payload for a new job opening."""

    role_title: str = Field(min_length=2, max_length=120)
    team: str = Field(min_length=1, max_length=120)
    location: str = Field(min_length=1, max_length=120)
    experience_level: str = Field(min_length=2, max_length=30)
    experience_range: str = Field(min_length=3, max_length=30)
    application_open_at: datetime
    application_close_at: datetime
    responsibilities: list[str] = Field(min_length=1)
    requirements: list[str] = Field(min_length=1)


class JobOpeningRecord(JobOpeningCreatePayload):
    """Stored representation of a job opening."""

    id: UUID
    paused: bool = False
    status: JobOpeningStatus = "closed"
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class JobOpeningListResponse(BaseModel):
    """Paginated response model for job openings."""

    items: list[JobOpeningRecord]
    total: int
    offset: int
    limit: int


class JobOpeningPausePayload(BaseModel):
    """Payload for pausing/resuming application intake for a job opening."""

    paused: bool
