"""Schema models for candidate application payloads and responses."""

from datetime import datetime
from typing import Any
from typing import Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, EmailStr, Field

ParseStatus = Literal["pending", "in_progress", "completed", "failed"]
ApplicantStatus = Literal[
    "applied",
    "screened",
    "shortlisted",
    "in_interview",
    "offer",
    "rejected",
    "received",
    "in_progress",
    "interview",
    "accepted",
    "sent_to_manager",
]


class ApplicationCreatePayload(BaseModel):
    """Create payload for an application submission."""

    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    linkedin_url: AnyHttpUrl | None = None
    portfolio_url: AnyHttpUrl
    github_url: AnyHttpUrl
    twitter_url: AnyHttpUrl | None = None
    role_selection: str = Field(min_length=2, max_length=120)


class ResumeFileMeta(BaseModel):
    """Metadata stored for an uploaded resume file."""

    original_filename: str
    stored_filename: str
    storage_path: str
    content_type: str
    size_bytes: int


class StatusHistoryEntry(BaseModel):
    """One status-transition event for applicant lifecycle auditing."""

    status: str
    note: str | None = None
    changed_at: datetime
    source: str = "system"


class ApplicationRecord(BaseModel):
    """Stored representation of an application."""

    id: UUID
    job_opening_id: UUID
    full_name: str
    email: EmailStr
    linkedin_url: AnyHttpUrl | None = None
    portfolio_url: AnyHttpUrl
    github_url: AnyHttpUrl
    twitter_url: AnyHttpUrl | None = None
    role_selection: str
    parse_result: dict | None = None
    parse_status: ParseStatus = "pending"
    applicant_status: ApplicantStatus = "applied"
    ai_score: float | None = None
    ai_screening_summary: str | None = None
    online_research_summary: str | None = None
    status_history: list[StatusHistoryEntry] = Field(default_factory=list)
    reference_status: bool = False
    latest_position: str | None = None
    total_years_experience: float | None = None
    parsed_skills: list[str] | None = None
    parsed_education: list[dict[str, Any]] | None = None
    resume: ResumeFileMeta
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ApplicationListResponse(BaseModel):
    """Paginated response model for applicant records."""

    items: list[ApplicationRecord]
    total: int
    offset: int
    limit: int


class ApplicantStatusUpdatePayload(BaseModel):
    """Request payload for updating an applicant lifecycle status."""

    applicant_status: ApplicantStatus
    note: str | None = Field(default=None, max_length=1000)


class AdminCandidateReviewPayload(BaseModel):
    """Admin payload for status override notes and AI screening metadata."""

    applicant_status: ApplicantStatus | None = None
    note: str | None = Field(default=None, max_length=1000)
    ai_score: float | None = Field(default=None, ge=0.0, le=100.0)
    ai_screening_summary: str | None = Field(default=None, max_length=4000)
    online_research_summary: str | None = Field(default=None, max_length=4000)
