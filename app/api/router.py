"""Top-level API router registration."""

from fastapi import APIRouter

from app.api.v1 import admin, applications, google_auth, job_openings, referee, references

api_router = APIRouter()
api_router.include_router(admin.router)
api_router.include_router(google_auth.router)
api_router.include_router(job_openings.router)
api_router.include_router(applications.router)
api_router.include_router(references.router)
api_router.include_router(referee.router)
