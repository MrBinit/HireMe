"""API routes for job opening management."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.deps import get_admin_principal, get_job_opening_service_dep
from app.core.security import AdminPrincipal
from app.schemas.job_opening import (
    JobOpeningCreatePayload,
    JobOpeningListResponse,
    JobOpeningPausePayload,
    JobOpeningRecord,
)
from app.services.job_opening_service import JobOpeningService

router = APIRouter(tags=["job-openings"])


@router.post("/job-openings", response_model=JobOpeningRecord, status_code=status.HTTP_201_CREATED)
async def create_job_opening(
    payload: JobOpeningCreatePayload,
    _: AdminPrincipal = Depends(get_admin_principal),
    service: JobOpeningService = Depends(get_job_opening_service_dep),
) -> JobOpeningRecord:
    """Create a new job opening."""

    return await service.create(payload)


@router.get("/job-openings", response_model=JobOpeningListResponse)
async def list_job_openings(
    offset: int = Query(default=0, ge=0),
    limit: int | None = Query(default=None, ge=1),
    service: JobOpeningService = Depends(get_job_opening_service_dep),
) -> JobOpeningListResponse:
    """List available job openings."""

    return await service.list(offset=offset, limit=limit)


@router.delete(
    "/job-openings/{job_opening_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_job_opening(
    job_opening_id: UUID,
    _: AdminPrincipal = Depends(get_admin_principal),
    service: JobOpeningService = Depends(get_job_opening_service_dep),
) -> Response:
    """Delete a job opening by its UUID."""

    deleted = await service.delete(str(job_opening_id))

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job opening not found.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/job-openings/{job_opening_id}/pause", response_model=JobOpeningRecord)
async def pause_job_opening(
    job_opening_id: UUID,
    payload: JobOpeningPausePayload,
    _: AdminPrincipal = Depends(get_admin_principal),
    service: JobOpeningService = Depends(get_job_opening_service_dep),
) -> JobOpeningRecord:
    """Pause or resume one job opening."""

    updated = await service.set_paused(str(job_opening_id), payload.paused)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job opening not found.",
        )
    return updated
