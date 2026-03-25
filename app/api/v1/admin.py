"""API routes for admin authentication and candidate management."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import (
    get_admin_auth_service_dep,
    get_admin_principal,
    get_application_service_dep,
)
from app.core.security import AdminPrincipal
from app.schemas.application import (
    ApplicantStatusUpdatePayload,
    ApplicationListResponse,
    ApplicationRecord,
)
from app.schemas.auth import AdminAccessTokenResponse, AdminLoginPayload
from app.services.admin_auth_service import (
    AdminAuthConfigurationError,
    AdminAuthError,
    AdminAuthService,
)
from app.services.application_service import ApplicationService

router = APIRouter(tags=["admin"])


@router.post(
    "/admin/login",
    response_model=AdminAccessTokenResponse,
)
async def admin_login(
    payload: AdminLoginPayload,
    service: AdminAuthService = Depends(get_admin_auth_service_dep),
) -> AdminAccessTokenResponse:
    """Authenticate admin credentials and return bearer JWT."""

    try:
        return service.login(payload)
    except AdminAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except AdminAuthConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.get("/admin/candidates", response_model=ApplicationListResponse)
async def list_candidates(
    offset: int = Query(default=0, ge=0),
    limit: int | None = Query(default=None, ge=1),
    job_opening_id: UUID | None = Query(default=None),
    _: AdminPrincipal = Depends(get_admin_principal),
    service: ApplicationService = Depends(get_application_service_dep),
) -> ApplicationListResponse:
    """List candidate applications for admin users."""

    return await service.list(
        offset=offset,
        limit=limit,
        job_opening_id=job_opening_id,
    )


@router.get("/admin/candidates/{application_id}", response_model=ApplicationRecord)
async def get_candidate(
    application_id: UUID,
    _: AdminPrincipal = Depends(get_admin_principal),
    service: ApplicationService = Depends(get_application_service_dep),
) -> ApplicationRecord:
    """Return one candidate application by UUID for admin users."""

    record = await service.get_by_id(application_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidate application not found.",
        )
    return record


@router.patch(
    "/admin/candidates/{application_id}/status",
    response_model=ApplicationRecord,
)
async def update_candidate_status(
    application_id: UUID,
    payload: ApplicantStatusUpdatePayload,
    _: AdminPrincipal = Depends(get_admin_principal),
    service: ApplicationService = Depends(get_application_service_dep),
) -> ApplicationRecord:
    """Update one candidate's applicant_status for admin workflows."""

    updated = await service.update_applicant_status(
        application_id=application_id,
        applicant_status=payload.applicant_status,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidate application not found.",
        )
    return updated
