"""Pydantic schemas for request and response contracts."""

from app.schemas.application import (
    ApplicationCreatePayload,
    ApplicantStatusUpdatePayload,
    ApplicationRecord,
    ResumeFileMeta,
)
from app.schemas.auth import AdminAccessTokenResponse, AdminLoginPayload
from app.schemas.job_opening import (
    JobOpeningCreatePayload,
    JobOpeningListResponse,
    JobOpeningRecord,
)

__all__ = [
    "ApplicationCreatePayload",
    "ApplicantStatusUpdatePayload",
    "ResumeFileMeta",
    "ApplicationRecord",
    "AdminLoginPayload",
    "AdminAccessTokenResponse",
    "JobOpeningCreatePayload",
    "JobOpeningRecord",
    "JobOpeningListResponse",
]
