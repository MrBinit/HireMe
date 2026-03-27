"""Versioned API route modules."""

from app.api.v1 import (
    admin,
    applications,
    fireflies,
    google_auth,
    job_openings,
    referee,
    references,
    slack,
)

__all__ = [
    "admin",
    "applications",
    "fireflies",
    "google_auth",
    "job_openings",
    "referee",
    "references",
    "slack",
]
