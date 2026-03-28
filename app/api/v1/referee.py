"""API routes for referee login and referee-facing candidate/reference actions."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import (
    get_application_service_dep,
    get_referee_auth_service_dep,
    get_referee_principal,
    get_reference_service_dep,
)
from app.core.security import AdminPrincipal
from app.schemas.application import (
    ApplicantStatus,
    ApplicationListResponse,
    ApplicationRecord,
)
from app.schemas.auth import AdminAccessTokenResponse, AdminLoginPayload
from app.schemas.reference import (
    RefereeReferenceCreatePayload,
    ReferenceListResponse,
    ReferenceRecord,
)
from app.services.admin_auth_service import (
    AdminAuthConfigurationError,
    AdminAuthError,
    AdminAuthService,
)
from app.services.application_service import ApplicationService
from app.services.reference_service import ReferenceService

router = APIRouter(tags=["referee"])


@router.post(
    "/referee/login",
    response_model=AdminAccessTokenResponse,
)
async def referee_login(
    payload: AdminLoginPayload,
    service: AdminAuthService = Depends(get_referee_auth_service_dep),
) -> AdminAccessTokenResponse:
    """Authenticate referee credentials and return bearer JWT."""

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


@router.get("/referee/candidates", response_model=ApplicationListResponse)
async def list_referee_candidates(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    role_selection: str | None = Query(default=None),
    applicant_status: ApplicantStatus | None = Query(default=None),
    submitted_from: datetime | None = Query(default=None),
    submitted_to: datetime | None = Query(default=None),
    _: AdminPrincipal = Depends(get_referee_principal),
    service: ApplicationService = Depends(get_application_service_dep),
) -> ApplicationListResponse:
    """List candidates for referee workflows."""

    return await service.list(
        offset=offset,
        limit=limit,
        role_selection=role_selection,
        applicant_status=applicant_status,
        submitted_from=submitted_from,
        submitted_to=submitted_to,
    )


@router.get("/referee/candidates/{application_id}", response_model=ApplicationRecord)
async def get_referee_candidate(
    application_id: UUID,
    _: AdminPrincipal = Depends(get_referee_principal),
    service: ApplicationService = Depends(get_application_service_dep),
) -> ApplicationRecord:
    """Return one candidate application by UUID for referee users."""

    record = await service.get_by_id(application_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidate application not found.",
        )
    return record


@router.post(
    "/referee/references",
    response_model=ReferenceRecord,
    status_code=status.HTTP_201_CREATED,
)
async def create_referee_reference(
    payload: RefereeReferenceCreatePayload,
    _: AdminPrincipal = Depends(get_referee_principal),
    service: ReferenceService = Depends(get_reference_service_dep),
) -> ReferenceRecord:
    """Create one reference entry from referee-provided applicant and referee details."""

    return await service.create_from_referee(payload)


@router.get("/referee/references", response_model=ReferenceListResponse)
async def list_referee_references(
    application_id: UUID = Query(...),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    _: AdminPrincipal = Depends(get_referee_principal),
    service: ReferenceService = Depends(get_reference_service_dep),
) -> ReferenceListResponse:
    """List references attached to one candidate application id."""

    return await service.list(
        application_id=application_id,
        offset=offset,
        limit=limit,
    )
