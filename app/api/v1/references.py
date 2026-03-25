"""API routes for applicant reference submission and listing."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import get_admin_principal, get_reference_service_dep
from app.core.security import AdminPrincipal
from app.schemas.reference import ReferenceCreatePayload, ReferenceListResponse, ReferenceRecord
from app.services.reference_service import ReferenceService

router = APIRouter(tags=["references"])


@router.post("/references", response_model=ReferenceRecord, status_code=status.HTTP_201_CREATED)
async def create_reference(
    payload: ReferenceCreatePayload,
    _: AdminPrincipal = Depends(get_admin_principal),
    service: ReferenceService = Depends(get_reference_service_dep),
) -> ReferenceRecord:
    """Create one reference entry for a candidate application."""

    return await service.create(payload)


@router.get("/references", response_model=ReferenceListResponse)
async def list_references(
    application_id: UUID = Query(...),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1),
    _: AdminPrincipal = Depends(get_admin_principal),
    service: ReferenceService = Depends(get_reference_service_dep),
) -> ReferenceListResponse:
    """List references attached to one candidate application id."""

    return await service.list(
        application_id=application_id,
        offset=offset,
        limit=limit,
    )
