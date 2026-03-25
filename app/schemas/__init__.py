"""Pydantic schemas for request and response contracts."""

from app.schemas.application import (
    AdminCandidateReviewPayload,
    ApplicationCreatePayload,
    ApplicantStatusUpdatePayload,
    ApplicationRecord,
    ResumeFileMeta,
)
from app.schemas.auth import AdminAccessTokenResponse, AdminLoginPayload
from app.schemas.job_opening import (
    JobOpeningCreatePayload,
    JobOpeningListResponse,
    JobOpeningPausePayload,
    JobOpeningRecord,
)

__all__ = [
    "ApplicationCreatePayload",
    "AdminCandidateReviewPayload",
    "ApplicantStatusUpdatePayload",
    "ResumeFileMeta",
    "ApplicationRecord",
    "AdminLoginPayload",
    "AdminAccessTokenResponse",
    "JobOpeningCreatePayload",
    "JobOpeningPausePayload",
    "JobOpeningRecord",
    "JobOpeningListResponse",
]
