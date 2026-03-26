"""Versioned API route modules."""

from app.api.v1 import admin, applications, google_auth, job_openings, referee, references

__all__ = ["admin", "applications", "google_auth", "job_openings", "referee", "references"]
