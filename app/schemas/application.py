"""Schema models for candidate application payloads and responses."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, EmailStr, Field

ParseStatus = Literal["pending", "in_progress", "completed", "failed"]
EvaluationStatus = Literal["queued", "in_progress", "completed", "failed"]
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
    linkedin_url: AnyHttpUrl
    portfolio_url: AnyHttpUrl | None = None
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
    portfolio_url: AnyHttpUrl | None = None
    github_url: AnyHttpUrl
    twitter_url: AnyHttpUrl | None = None
    role_selection: str
    parse_result: dict | None = None
    parsed_total_years_experience: float | None = None
    parsed_search_text: str | None = None
    parse_status: ParseStatus = "pending"
    evaluation_status: EvaluationStatus | None = None
    applicant_status: ApplicantStatus = "applied"
    rejection_reason: str | None = None
    ai_score: float | None = None
    ai_screening_summary: str | None = None
    candidate_brief: str | None = None
    online_research_summary: str | None = None
    status_history: list[StatusHistoryEntry] = Field(default_factory=list)
    reference_status: bool = False
    resume: ResumeFileMeta
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ApplicationListResponse(BaseModel):
    """Paginated response model for applicant records."""

    items: list[ApplicationRecord]
    total: int
    offset: int
    limit: int


class ResumeDownloadResponse(BaseModel):
    """Response payload containing temporary resume download URL."""

    download_url: str
    expires_in_seconds: int
    filename: str


class PublicApplicationStatusResponse(BaseModel):
    """Public status payload for applicant self-tracking after submission."""

    application_id: UUID
    applicant_status: ApplicantStatus
    parse_status: ParseStatus
    evaluation_status: EvaluationStatus | None = None
    ai_score: float | None = None
    role_selection: str
    submitted_at: datetime
    research_ready: bool = False


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
    candidate_brief: str | None = Field(default=None, max_length=1500)
    online_research_summary: str | None = Field(default=None, max_length=4000)
    rejection_reason: str | None = Field(default=None, max_length=1000)
