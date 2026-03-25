"""API routes for candidate application submission."""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import ValidationError

from app.api.deps import get_admin_principal, get_application_service_dep
from app.core.security import AdminPrincipal
from app.schemas.application import (
    ApplicantStatus,
    ApplicationCreatePayload,
    ApplicationListResponse,
    ApplicationRecord,
)
from app.services.application_service import ApplicationService

router = APIRouter(tags=["applications"])


@router.get("/roles", response_model=list[str])
async def list_roles(
    service: ApplicationService = Depends(get_application_service_dep),
) -> list[str]:
    """Return current role titles that candidates can apply to."""

    return await service.get_allowed_roles()


@router.post("/applications", response_model=ApplicationRecord, status_code=status.HTTP_201_CREATED)
async def submit_application(
    full_name: str = Form(...),
    email: str = Form(...),
    linkedin_url: str | None = Form(default=None),
    portfolio_url: str = Form(...),
    github_url: str = Form(...),
    twitter_url: str | None = Form(default=None),
    role_selection: str = Form(...),
    resume: UploadFile = File(...),
    service: ApplicationService = Depends(get_application_service_dep),
) -> ApplicationRecord:
    """Submit an application with a resume upload."""

    raw_payload: dict[str, Any] = {
        "full_name": full_name,
        "email": email,
        "linkedin_url": linkedin_url or None,
        "portfolio_url": portfolio_url,
        "github_url": github_url,
        "twitter_url": twitter_url or None,
        "role_selection": role_selection,
    }

    try:
        payload = ApplicationCreatePayload.model_validate(raw_payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(),
        ) from exc

    return await service.submit(payload=payload, resume=resume)


@router.get("/applications", response_model=ApplicationListResponse)
async def list_applications(
    offset: int = Query(default=0, ge=0),
    limit: int | None = Query(default=None, ge=1),
    job_opening_id: UUID | None = Query(default=None),
    role_selection: str | None = Query(default=None),
    applicant_status: ApplicantStatus | None = Query(default=None),
    submitted_from: datetime | None = Query(default=None),
    submitted_to: datetime | None = Query(default=None),
    _: AdminPrincipal = Depends(get_admin_principal),
    service: ApplicationService = Depends(get_application_service_dep),
) -> ApplicationListResponse:
    """List submitted applicant records with optional opening filter."""

    return await service.list(
        offset=offset,
        limit=limit,
        job_opening_id=job_opening_id,
        role_selection=role_selection,
        applicant_status=applicant_status,
        submitted_from=submitted_from,
        submitted_to=submitted_to,
    )
