"""Tests for evaluation SQS worker auto-queue behavior for research enrichment."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

from app.core.error import ApplicationValidationError
from app.infra.sqs_queue import SqsMessage
from app.schemas.application import ApplicationRecord, ResumeFileMeta
from app.schemas.evaluation import CandidateEvaluationResult, EvaluationBreakdown
from app.scripts.sqs_evaluation_worker import SqsEvaluationWorker
from app.services.research_queue import CandidateResearchEnrichmentJob


def _build_candidate(*, applicant_status: str) -> ApplicationRecord:
    """Build one candidate record for evaluation-worker tests."""

    app_id = uuid4()
    return ApplicationRecord(
        id=app_id,
        job_opening_id=uuid4(),
        full_name="Eval Candidate",
        email="candidate@example.com",
        linkedin_url="https://linkedin.com/in/eval-candidate",
        portfolio_url="https://flowcv.me/eval-candidate",
        github_url="https://github.com/eval-candidate",
        twitter_url=None,
        role_selection="Backend Engineer",
        parse_result={"skills": ["Python"]},
        parsed_total_years_experience=3.0,
        parsed_search_text="python fastapi",
        parse_status="completed",
        evaluation_status=None,
        applicant_status=applicant_status,  # type: ignore[arg-type]
        resume=ResumeFileMeta(
            original_filename="resume.pdf",
            stored_filename=f"{app_id}.pdf",
            storage_path=f"s3://hireme-cv-bucket/hireme/resumes/{app_id}.pdf",
            content_type="application/pdf",
            size_bytes=1000,
        ),
        created_at=datetime.now(tz=timezone.utc),
    )


class _FakeQueueClient:
    """Queue client fake for delete tracking."""

    def __init__(self) -> None:
        self.deleted_receipt_handles: list[str] = []

    async def delete_message(self, receipt_handle: str) -> None:
        self.deleted_receipt_handles.append(receipt_handle)


class _FakeEvaluator:
    """Evaluator fake returning deterministic score."""

    def __init__(self, score: float) -> None:
        self.score = score

    async def evaluate_application(self, *, application_id):
        _ = application_id
        return CandidateEvaluationResult(
            score=float(self.score),
            breakdown=EvaluationBreakdown(
                skills=min(40.0, self.score * 0.4),
                experience=min(30.0, self.score * 0.3),
                education=min(10.0, self.score * 0.1),
                role_alignment=min(20.0, self.score * 0.2),
            ),
            reason="Deterministic score for tests.",
        )


class _RaisingEvaluator:
    """Evaluator fake raising one predefined exception."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def evaluate_application(self, *, application_id):
        _ = application_id
        raise self.error


class _FakeApplicationRepository:
    """Application repository fake implementing required update/get methods."""

    def __init__(self, record: ApplicationRecord) -> None:
        self.record = record
        self.update_calls: list[dict[str, object]] = []

    async def update_admin_review(self, *, application_id, updates):
        if str(self.record.id) != str(application_id):
            return False
        self.update_calls.append(dict(updates))
        self.record = self.record.model_copy(update=updates)
        return True

    async def get_by_id(self, application_id):
        if str(self.record.id) != str(application_id):
            return None
        return self.record


class _FakeResearchQueuePublisher:
    """Research queue fake storing published jobs."""

    def __init__(self) -> None:
        self.jobs: list[CandidateResearchEnrichmentJob] = []

    async def publish(self, job: CandidateResearchEnrichmentJob) -> None:
        self.jobs.append(job)


def test_evaluation_worker_queues_research_only_when_score_passes_threshold() -> None:
    """Score above threshold should queue research enrichment."""

    async def run() -> None:
        candidate = _build_candidate(applicant_status="screened")
        queue_client = _FakeQueueClient()
        app_repo = _FakeApplicationRepository(candidate)
        research_queue = _FakeResearchQueuePublisher()
        worker = SqsEvaluationWorker(
            queue_client=queue_client,  # type: ignore[arg-type]
            evaluator=_FakeEvaluator(82.0),  # type: ignore[arg-type]
            application_repository=app_repo,  # type: ignore[arg-type]
            research_queue_publisher=research_queue,  # type: ignore[arg-type]
            research_queue_enabled=True,
            research_target_statuses={"shortlisted"},
            research_enqueue_timeout_seconds=1.0,
            ai_score_threshold=70.0,
            max_in_flight=1,
            receive_batch_size=1,
            receive_wait_seconds=1,
            visibility_timeout_seconds=30,
        )

        message = SqsMessage(
            message_id="e1",
            receipt_handle="r1",
            body=json.dumps({"application_id": str(candidate.id)}),
        )
        await worker._process_message(message)

        assert len(research_queue.jobs) == 1
        assert str(research_queue.jobs[0].application_id) == str(candidate.id)
        assert queue_client.deleted_receipt_handles == ["r1"]

    asyncio.run(run())


def test_evaluation_worker_does_not_queue_research_when_score_is_below_threshold() -> None:
    """Score below threshold should skip research enrichment queue."""

    async def run() -> None:
        candidate = _build_candidate(applicant_status="screened")
        queue_client = _FakeQueueClient()
        app_repo = _FakeApplicationRepository(candidate)
        research_queue = _FakeResearchQueuePublisher()
        worker = SqsEvaluationWorker(
            queue_client=queue_client,  # type: ignore[arg-type]
            evaluator=_FakeEvaluator(45.0),  # type: ignore[arg-type]
            application_repository=app_repo,  # type: ignore[arg-type]
            research_queue_publisher=research_queue,  # type: ignore[arg-type]
            research_queue_enabled=True,
            research_target_statuses={"shortlisted"},
            research_enqueue_timeout_seconds=1.0,
            ai_score_threshold=70.0,
            max_in_flight=1,
            receive_batch_size=1,
            receive_wait_seconds=1,
            visibility_timeout_seconds=30,
        )

        message = SqsMessage(
            message_id="e2",
            receipt_handle="r2",
            body=json.dumps({"application_id": str(candidate.id)}),
        )
        await worker._process_message(message)

        assert research_queue.jobs == []
        assert queue_client.deleted_receipt_handles == ["r2"]

    asyncio.run(run())


def test_evaluation_worker_drops_message_when_application_is_missing() -> None:
    """Missing candidate should be treated as non-retryable and message should be deleted."""

    async def run() -> None:
        candidate = _build_candidate(applicant_status="screened")
        queue_client = _FakeQueueClient()
        app_repo = _FakeApplicationRepository(candidate)
        research_queue = _FakeResearchQueuePublisher()
        worker = SqsEvaluationWorker(
            queue_client=queue_client,  # type: ignore[arg-type]
            evaluator=_FakeEvaluator(82.0),  # type: ignore[arg-type]
            application_repository=app_repo,  # type: ignore[arg-type]
            research_queue_publisher=research_queue,  # type: ignore[arg-type]
            research_queue_enabled=True,
            research_target_statuses={"shortlisted"},
            research_enqueue_timeout_seconds=1.0,
            ai_score_threshold=70.0,
            max_in_flight=1,
            receive_batch_size=1,
            receive_wait_seconds=1,
            visibility_timeout_seconds=30,
        )

        message = SqsMessage(
            message_id="e3",
            receipt_handle="r3",
            body=json.dumps({"application_id": str(uuid4())}),
        )
        await worker._process_message(message)

        assert queue_client.deleted_receipt_handles == ["r3"]
        assert app_repo.update_calls == []
        assert research_queue.jobs == []

    asyncio.run(run())


def test_evaluation_worker_drops_message_when_candidate_removed_mid_evaluation() -> None:
    """Candidate-not-found validation error should not be retried indefinitely."""

    async def run() -> None:
        candidate = _build_candidate(applicant_status="screened")
        queue_client = _FakeQueueClient()
        app_repo = _FakeApplicationRepository(candidate)
        research_queue = _FakeResearchQueuePublisher()
        worker = SqsEvaluationWorker(
            queue_client=queue_client,  # type: ignore[arg-type]
            evaluator=_RaisingEvaluator(  # type: ignore[arg-type]
                ApplicationValidationError("candidate application not found")
            ),
            application_repository=app_repo,  # type: ignore[arg-type]
            research_queue_publisher=research_queue,  # type: ignore[arg-type]
            research_queue_enabled=True,
            research_target_statuses={"shortlisted"},
            research_enqueue_timeout_seconds=1.0,
            ai_score_threshold=70.0,
            max_in_flight=1,
            receive_batch_size=1,
            receive_wait_seconds=1,
            visibility_timeout_seconds=30,
        )

        message = SqsMessage(
            message_id="e4",
            receipt_handle="r4",
            body=json.dumps({"application_id": str(candidate.id)}),
        )
        await worker._process_message(message)

        assert queue_client.deleted_receipt_handles == ["r4"]
        assert app_repo.update_calls == [{"evaluation_status": "in_progress"}]
        assert research_queue.jobs == []

    asyncio.run(run())
