"""Data-access repositories for persistence abstraction."""

from app.repositories.application_repository import (
    ApplicationRepository,
    DuplicateApplicationError,
)
from app.repositories.local_application_repository import LocalApplicationRepository
from app.repositories.job_opening_repository import JobOpeningRepository
from app.repositories.local_job_opening_repository import LocalJobOpeningRepository
from app.repositories.postgres_application_repository import PostgresApplicationRepository
from app.repositories.postgres_job_opening_repository import PostgresJobOpeningRepository
from app.repositories.s3_application_repository import S3ApplicationRepository
from app.repositories.s3_job_opening_repository import S3JobOpeningRepository

__all__ = [
    "ApplicationRepository",
    "DuplicateApplicationError",
    "LocalApplicationRepository",
    "PostgresApplicationRepository",
    "S3ApplicationRepository",
    "JobOpeningRepository",
    "LocalJobOpeningRepository",
    "PostgresJobOpeningRepository",
    "S3JobOpeningRepository",
]
