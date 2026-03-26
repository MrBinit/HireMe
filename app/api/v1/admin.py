"""API routes for admin authentication and candidate management."""

from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
import anyio

from app.api.deps import (
    get_admin_auth_service_dep,
    get_candidate_evaluation_service_dep,
    get_evaluation_queue_publisher_dep,
    get_research_queue_publisher_dep,
    get_scheduling_queue_publisher_dep,
    get_admin_principal,
    get_application_service_dep,
    get_s3_store,
)
from app.core.runtime_config import get_runtime_config
from app.core.security import AdminPrincipal
from app.schemas.application import (
    AdminCandidateReviewPayload,
    ApplicantStatusUpdatePayload,
    ApplicantStatus,
    ApplicationListResponse,
    ApplicationRecord,
    ResumeDownloadResponse,
)
from app.schemas.auth import AdminAccessTokenResponse, AdminLoginPayload
from app.infra.s3_store import S3ObjectStore
from app.schemas.evaluation import (
    CandidateEvaluationQueueResponse,
    CandidateResearchQueueResponse,
    CandidateSchedulingQueueResponse,
)
from app.services.admin_auth_service import (
    AdminAuthConfigurationError,
    AdminAuthError,
    AdminAuthService,
)
from app.services.application_service import ApplicationService
from app.services.candidate_evaluation_service import CandidateEvaluationService
from app.services.evaluation_queue import (
    CandidateEvaluationJob,
    EvaluationQueuePublishError,
    EvaluationQueuePublisher,
)
from app.services.research_queue import (
    CandidateResearchEnrichmentJob,
    ResearchQueuePublishError,
    ResearchQueuePublisher,
)
from app.services.scheduling_queue import (
    CandidateInterviewSchedulingJob,
    SchedulingQueuePublishError,
    SchedulingQueuePublisher,
)

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
    role_selection: str | None = Query(default=None),
    applicant_status: ApplicantStatus | None = Query(default=None),
    submitted_from: datetime | None = Query(default=None),
    submitted_to: datetime | None = Query(default=None),
    keyword_search: str | None = Query(default=None),
    experience_within_range: bool | None = Query(default=None),
    prefilter_by_job_opening: bool = Query(default=False),
    _: AdminPrincipal = Depends(get_admin_principal),
    service: ApplicationService = Depends(get_application_service_dep),
) -> ApplicationListResponse:
    """List candidate applications for admin users."""

    return await service.list(
        offset=offset,
        limit=limit,
        job_opening_id=job_opening_id,
        role_selection=role_selection,
        applicant_status=applicant_status,
        submitted_from=submitted_from,
        submitted_to=submitted_to,
        keyword_search=keyword_search,
        experience_within_range=experience_within_range,
        prefilter_by_job_opening=prefilter_by_job_opening,
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
        note=payload.note,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidate application not found.",
        )
    return updated


@router.get(
    "/admin/candidates/{application_id}/resume-download",
    response_model=ResumeDownloadResponse,
)
async def get_candidate_resume_download_url(
    application_id: UUID,
    _: AdminPrincipal = Depends(get_admin_principal),
    service: ApplicationService = Depends(get_application_service_dep),
    s3_store: S3ObjectStore = Depends(get_s3_store),
) -> ResumeDownloadResponse:
    """Return a temporary pre-signed download URL for candidate resume."""

    candidate = await service.get_by_id(application_id)
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidate application not found.",
        )

    storage_path = candidate.resume.storage_path
    parsed = urlparse(storage_path)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="resume is not stored in S3",
        )

    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    runtime_config = get_runtime_config()
    expires_in = runtime_config.application.resume_download_url_expire_seconds
    content_disposition = f'attachment; filename="{candidate.resume.original_filename}"'
    download_url = await s3_store.generate_presigned_get_url(
        key=key,
        expires_in_seconds=expires_in,
        bucket=bucket,
        response_content_disposition=content_disposition,
    )
    return ResumeDownloadResponse(
        download_url=download_url,
        expires_in_seconds=expires_in,
        filename=candidate.resume.original_filename,
    )


@router.patch(
    "/admin/candidates/{application_id}/review",
    response_model=ApplicationRecord,
)
async def update_candidate_review(
    application_id: UUID,
    payload: AdminCandidateReviewPayload,
    _: AdminPrincipal = Depends(get_admin_principal),
    service: ApplicationService = Depends(get_application_service_dep),
) -> ApplicationRecord:
    """Update AI/admin review fields and optional status override note."""

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="provide at least one review field",
        )
    updated = await service.update_admin_review(
        application_id=application_id,
        updates=updates,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidate application not found.",
        )
    return updated


@router.post(
    "/admin/candidates/{application_id}/evaluate",
    response_model=CandidateEvaluationQueueResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def evaluate_candidate_with_llm(
    application_id: UUID,
    _: AdminPrincipal = Depends(get_admin_principal),
    evaluator: CandidateEvaluationService = Depends(get_candidate_evaluation_service_dep),
    service: ApplicationService = Depends(get_application_service_dep),
    queue: EvaluationQueuePublisher = Depends(get_evaluation_queue_publisher_dep),
) -> CandidateEvaluationQueueResponse:
    """Queue LLM evaluation for one candidate (async only)."""

    return await _enqueue_candidate_evaluation(
        application_id=application_id,
        evaluator=evaluator,
        service=service,
        queue=queue,
    )


@router.post(
    "/admin/candidates/{application_id}/evaluate/queue",
    response_model=CandidateEvaluationQueueResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def queue_candidate_evaluation(
    application_id: UUID,
    _: AdminPrincipal = Depends(get_admin_principal),
    evaluator: CandidateEvaluationService = Depends(get_candidate_evaluation_service_dep),
    service: ApplicationService = Depends(get_application_service_dep),
    queue: EvaluationQueuePublisher = Depends(get_evaluation_queue_publisher_dep),
) -> CandidateEvaluationQueueResponse:
    """Enqueue candidate LLM evaluation to be processed asynchronously."""

    return await _enqueue_candidate_evaluation(
        application_id=application_id,
        evaluator=evaluator,
        service=service,
        queue=queue,
    )


async def _enqueue_candidate_evaluation(
    *,
    application_id: UUID,
    evaluator: CandidateEvaluationService,
    service: ApplicationService,
    queue: EvaluationQueuePublisher,
) -> CandidateEvaluationQueueResponse:
    """Shared queue-enqueue implementation for async evaluation routes."""

    runtime_config = get_runtime_config()
    if not runtime_config.evaluation.use_queue:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="evaluation queue is disabled",
        )

    await evaluator.validate_candidate_for_evaluation(application_id=application_id)
    queued_at = datetime.now(tz=timezone.utc)
    job = CandidateEvaluationJob(
        application_id=application_id,
        queued_at=queued_at,
    )
    try:
        with anyio.fail_after(runtime_config.evaluation.enqueue_timeout_seconds):
            await queue.publish(job)
    except (EvaluationQueuePublishError, TimeoutError) as exc:
        await service.update_admin_review(
            application_id=application_id,
            updates={
                "evaluation_status": "failed",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="failed to queue LLM evaluation job",
        ) from exc

    updated = await service.update_admin_review(
        application_id=application_id,
        updates={
            "evaluation_status": "queued",
        },
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidate application not found.",
        )

    return CandidateEvaluationQueueResponse(
        application_id=application_id,
        queued=True,
        queued_at=queued_at,
        queue_name=runtime_config.evaluation.queue_name,
    )


@router.post(
    "/admin/candidates/{application_id}/research",
    response_model=CandidateResearchQueueResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_candidate_research_enrichment(
    application_id: UUID,
    _: AdminPrincipal = Depends(get_admin_principal),
    service: ApplicationService = Depends(get_application_service_dep),
    queue: ResearchQueuePublisher = Depends(get_research_queue_publisher_dep),
) -> CandidateResearchQueueResponse:
    """Queue research enrichment for one candidate (async only)."""

    return await _enqueue_candidate_research(
        application_id=application_id,
        service=service,
        queue=queue,
    )


@router.post(
    "/admin/candidates/{application_id}/research/queue",
    response_model=CandidateResearchQueueResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def queue_candidate_research_enrichment(
    application_id: UUID,
    _: AdminPrincipal = Depends(get_admin_principal),
    service: ApplicationService = Depends(get_application_service_dep),
    queue: ResearchQueuePublisher = Depends(get_research_queue_publisher_dep),
) -> CandidateResearchQueueResponse:
    """Enqueue candidate research enrichment to be processed asynchronously."""

    return await _enqueue_candidate_research(
        application_id=application_id,
        service=service,
        queue=queue,
    )


@router.post(
    "/admin/candidates/{application_id}/schedule",
    response_model=CandidateSchedulingQueueResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def queue_candidate_interview_scheduling(
    application_id: UUID,
    _: AdminPrincipal = Depends(get_admin_principal),
    service: ApplicationService = Depends(get_application_service_dep),
    queue: SchedulingQueuePublisher = Depends(get_scheduling_queue_publisher_dep),
) -> CandidateSchedulingQueueResponse:
    """Enqueue interview scheduling orchestration for one candidate."""

    return await _enqueue_candidate_scheduling(
        application_id=application_id,
        service=service,
        queue=queue,
    )


async def _enqueue_candidate_research(
    *,
    application_id: UUID,
    service: ApplicationService,
    queue: ResearchQueuePublisher,
) -> CandidateResearchQueueResponse:
    """Shared queue-enqueue implementation for async research routes."""

    runtime_config = get_runtime_config()
    enrichment_config = runtime_config.research.enrichment
    if not enrichment_config.use_queue:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="research enrichment queue is disabled",
        )

    candidate = await service.get_by_id(application_id)
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidate application not found.",
        )
    if candidate.applicant_status not in enrichment_config.target_statuses:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "candidate status not eligible for research enrichment queue; "
                f"allowed={enrichment_config.target_statuses}"
            ),
        )

    queued_at = datetime.now(tz=timezone.utc)
    job = CandidateResearchEnrichmentJob(
        application_id=application_id,
        queued_at=queued_at,
    )
    try:
        with anyio.fail_after(enrichment_config.enqueue_timeout_seconds):
            await queue.publish(job)
    except (ResearchQueuePublishError, TimeoutError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="failed to queue research enrichment job",
        ) from exc

    return CandidateResearchQueueResponse(
        application_id=application_id,
        queued=True,
        queued_at=queued_at,
        queue_name=enrichment_config.queue_name,
    )


async def _enqueue_candidate_scheduling(
    *,
    application_id: UUID,
    service: ApplicationService,
    queue: SchedulingQueuePublisher,
) -> CandidateSchedulingQueueResponse:
    """Shared queue-enqueue implementation for interview scheduling route."""

    runtime_config = get_runtime_config()
    scheduling_config = runtime_config.scheduling
    if not scheduling_config.enabled:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="interview scheduling is disabled",
        )
    if not scheduling_config.use_queue:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="interview scheduling queue is disabled",
        )

    candidate = await service.get_by_id(application_id)
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidate application not found.",
        )
    if candidate.applicant_status not in set(scheduling_config.target_statuses):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "candidate status not eligible for interview scheduling queue; "
                f"allowed={scheduling_config.target_statuses}"
            ),
        )

    queued_at = datetime.now(tz=timezone.utc)
    job = CandidateInterviewSchedulingJob(
        application_id=application_id,
        queued_at=queued_at,
    )
    try:
        with anyio.fail_after(scheduling_config.enqueue_timeout_seconds):
            await queue.publish(job)
    except (SchedulingQueuePublishError, TimeoutError) as exc:
        await service.update_admin_review(
            application_id=application_id,
            updates={
                "interview_schedule_status": "failed",
                "interview_schedule_error": "failed to queue scheduling job",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="failed to queue interview scheduling job",
        ) from exc

    updated = await service.update_admin_review(
        application_id=application_id,
        updates={
            "interview_schedule_status": "queued",
            "interview_schedule_error": None,
        },
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidate application not found.",
        )
    return CandidateSchedulingQueueResponse(
        application_id=application_id,
        queued=True,
        queued_at=queued_at,
        queue_name=scheduling_config.queue_name,
    )
