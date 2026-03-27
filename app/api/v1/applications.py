"""API routes for candidate application submission."""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from pydantic import ValidationError

from app.api.deps import (
    get_admin_principal,
    get_application_service_dep,
    get_interview_scheduling_service_dep,
)
from app.core.security import (
    TokenValidationError,
    decode_interview_action_token,
    decode_interview_confirmation_token,
)
from app.core.runtime_config import get_runtime_config
from app.core.settings import get_settings
from app.core.security import AdminPrincipal
from app.schemas.application import (
    ApplicantStatus,
    InterviewActionResponse,
    InterviewActionTokenPayload,
    ApplicationCreatePayload,
    ApplicationListResponse,
    ApplicationRecord,
    InterviewTokenConfirmPayload,
    InterviewSlotConfirmPayload,
    InterviewSlotConfirmResponse,
    PublicApplicationStatusResponse,
)
from app.services.application_service import ApplicationService, ApplicationValidationError
from app.services.interview_scheduling_service import InterviewSchedulingService

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
    linkedin_url: str = Form(...),
    portfolio_url: str | None = Form(default=None),
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
        "linkedin_url": linkedin_url,
        "portfolio_url": portfolio_url or None,
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


@router.get(
    "/applications/{application_id}/status",
    response_model=PublicApplicationStatusResponse,
)
async def get_public_application_status(
    application_id: UUID,
    email: str = Query(...),
    service: ApplicationService = Depends(get_application_service_dep),
) -> PublicApplicationStatusResponse:
    """Return applicant status by application id + applicant email."""

    record = await service.get_by_id(application_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Application not found.",
        )

    if str(record.email).strip().casefold() != email.strip().casefold():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Application not found.",
        )

    return PublicApplicationStatusResponse(
        application_id=record.id,
        applicant_status=record.applicant_status,
        parse_status=record.parse_status,
        evaluation_status=record.evaluation_status,
        interview_schedule_status=record.interview_schedule_status,
        ai_score=record.ai_score,
        role_selection=record.role_selection,
        submitted_at=record.created_at,
        research_ready=bool(record.online_research_summary),
    )


@router.post(
    "/applications/{application_id}/interview/confirm",
    response_model=InterviewSlotConfirmResponse,
)
async def confirm_public_interview_slot(
    application_id: UUID,
    payload: InterviewSlotConfirmPayload,
    service: ApplicationService = Depends(get_application_service_dep),
    scheduling_service: InterviewSchedulingService = Depends(get_interview_scheduling_service_dep),
) -> InterviewSlotConfirmResponse:
    """Confirm one offered interview slot as candidate and finalize booking."""

    record = await service.get_by_id(application_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Application not found.",
        )

    updated_payload = await scheduling_service.confirm_candidate_slot(
        application_id=application_id,
        candidate_email=str(payload.email),
        option_number=payload.option_number,
    )

    refreshed = await service.get_by_id(application_id)
    if refreshed is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Application not found.",
        )

    raw_confirmed_at = updated_payload.get("confirmed_at")
    if not isinstance(raw_confirmed_at, str):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="interview confirmation missing confirmed timestamp",
        )
    normalized = (
        raw_confirmed_at[:-1] + "+00:00" if raw_confirmed_at.endswith("Z") else raw_confirmed_at
    )
    confirmed_at = datetime.fromisoformat(normalized)

    confirmed_event_id = updated_payload.get("confirmed_event_id")
    if not isinstance(confirmed_event_id, str) or not confirmed_event_id.strip():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="interview confirmation missing confirmed event id",
        )

    confirmed_event_link = updated_payload.get("confirmed_event_link")
    confirmed_meeting_link = updated_payload.get("confirmed_meeting_link")
    return InterviewSlotConfirmResponse(
        application_id=application_id,
        interview_schedule_status=refreshed.interview_schedule_status or "unknown",
        applicant_status=refreshed.applicant_status,
        selected_option_number=payload.option_number,
        confirmed_event_id=confirmed_event_id,
        confirmed_event_link=(
            confirmed_event_link if isinstance(confirmed_event_link, str) else None
        ),
        confirmed_meeting_link=(
            confirmed_meeting_link if isinstance(confirmed_meeting_link, str) else None
        ),
        confirmed_at=confirmed_at,
    )


@router.post(
    "/applications/interview/confirm-token",
    response_model=InterviewSlotConfirmResponse,
)
async def confirm_public_interview_slot_with_token(
    payload: InterviewTokenConfirmPayload,
    service: ApplicationService = Depends(get_application_service_dep),
    scheduling_service: InterviewSchedulingService = Depends(get_interview_scheduling_service_dep),
) -> InterviewSlotConfirmResponse:
    """Confirm candidate interview slot using signed token from email link."""

    settings = get_settings()
    runtime_config = get_runtime_config()
    secret = settings.interview_confirmation_token_secret or settings.admin_jwt_secret
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="interview confirmation is not configured",
        )
    try:
        claims = decode_interview_confirmation_token(
            token=payload.token,
            secret=secret,
            config=runtime_config.security,
        )
    except TokenValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    updated_payload = await scheduling_service.confirm_candidate_slot(
        application_id=claims.application_id,
        candidate_email=claims.candidate_email,
        option_number=claims.option_number,
    )

    refreshed = await service.get_by_id(claims.application_id)
    if refreshed is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Application not found.",
        )

    raw_confirmed_at = updated_payload.get("confirmed_at")
    if not isinstance(raw_confirmed_at, str):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="interview confirmation missing confirmed timestamp",
        )
    normalized = (
        raw_confirmed_at[:-1] + "+00:00" if raw_confirmed_at.endswith("Z") else raw_confirmed_at
    )
    confirmed_at = datetime.fromisoformat(normalized)
    confirmed_event_id = updated_payload.get("confirmed_event_id")
    if not isinstance(confirmed_event_id, str) or not confirmed_event_id.strip():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="interview confirmation missing confirmed event id",
        )

    confirmed_event_link = updated_payload.get("confirmed_event_link")
    confirmed_meeting_link = updated_payload.get("confirmed_meeting_link")
    return InterviewSlotConfirmResponse(
        application_id=claims.application_id,
        interview_schedule_status=refreshed.interview_schedule_status or "unknown",
        applicant_status=refreshed.applicant_status,
        selected_option_number=claims.option_number,
        confirmed_event_id=confirmed_event_id,
        confirmed_event_link=(
            confirmed_event_link if isinstance(confirmed_event_link, str) else None
        ),
        confirmed_meeting_link=(
            confirmed_meeting_link if isinstance(confirmed_meeting_link, str) else None
        ),
        confirmed_at=confirmed_at,
    )


@router.post(
    "/applications/interview/action-token",
    response_model=InterviewActionResponse,
)
async def process_interview_action_token(
    payload: InterviewActionTokenPayload,
    service: ApplicationService = Depends(get_application_service_dep),
    scheduling_service: InterviewSchedulingService = Depends(get_interview_scheduling_service_dep),
) -> InterviewActionResponse:
    """Process one signed interview action token from email CTA links."""

    settings = get_settings()
    runtime_config = get_runtime_config()
    secret = settings.interview_confirmation_token_secret or settings.admin_jwt_secret
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="interview action is not configured",
        )
    try:
        claims = decode_interview_action_token(
            token=payload.token,
            secret=secret,
            config=runtime_config.security,
        )
    except TokenValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    action = claims.action
    if action == "request_reschedule":
        updated_payload = await scheduling_service.request_reschedule(
            application_id=claims.application_id,
            actor=claims.actor,
            candidate_email=claims.candidate_email,
        )
        message = "Reschedule request received. Alternative slots were sent to the interviewer."
    elif action == "manager_accept_reschedule":
        updated_payload = await scheduling_service.process_manager_reschedule_decision(
            application_id=claims.application_id,
            decision="accept",
            round_number=claims.round_number,
            option_number=claims.option_number,
        )
        message = "Alternative interview slot accepted and confirmed."
    elif action == "manager_reject_reschedule":
        updated_payload = await scheduling_service.process_manager_reschedule_decision(
            application_id=claims.application_id,
            decision="reject",
            round_number=claims.round_number,
        )
        message = "Current alternatives rejected. New options were generated and sent."
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="unsupported interview action",
        )

    refreshed = await service.get_by_id(claims.application_id)
    if refreshed is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Application not found.",
        )
    confirmed_event_link = updated_payload.get("confirmed_event_link")
    confirmed_meeting_link = updated_payload.get("confirmed_meeting_link")
    return InterviewActionResponse(
        application_id=claims.application_id,
        interview_schedule_status=refreshed.interview_schedule_status or "unknown",
        applicant_status=refreshed.applicant_status,
        message=message,
        confirmed_event_link=(
            confirmed_event_link if isinstance(confirmed_event_link, str) else None
        ),
        confirmed_meeting_link=(
            confirmed_meeting_link if isinstance(confirmed_meeting_link, str) else None
        ),
    )


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


@router.post(
    "/integrations/docusign/webhook",
    status_code=status.HTTP_202_ACCEPTED,
)
async def docusign_webhook_callback(
    request: Request,
    application_id: UUID = Query(...),
    token: str | None = Query(default=None),
    service: ApplicationService = Depends(get_application_service_dep),
) -> dict[str, bool]:
    """Handle DocuSign envelope events and update candidate offer-signature status."""

    try:
        processed = await service.handle_docusign_webhook(
            application_id=application_id,
            webhook_token=token,
            raw_body=await request.body(),
            content_type=request.headers.get("content-type"),
        )
    except ApplicationValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return {"processed": bool(processed)}
