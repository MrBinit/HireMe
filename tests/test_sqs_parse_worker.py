"""Tests for parse SQS worker auto-queue behavior for AI evaluation."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

from app.infra.sqs_queue import SqsMessage
from app.schemas.application import ApplicationRecord, ResumeFileMeta
from app.scripts.sqs_worker import SqsParseWorker
from app.services.evaluation_queue import CandidateEvaluationJob


def _build_candidate(*, applicant_status: str, evaluation_status: str | None) -> ApplicationRecord:
    """Build one application record used by parse-worker tests."""

    app_id = uuid4()
    return ApplicationRecord(
        id=app_id,
        job_opening_id=uuid4(),
        full_name="Test Candidate",
        email="candidate@example.com",
        linkedin_url="https://linkedin.com/in/test-candidate",
        portfolio_url="https://flowcv.me/test-candidate",
        github_url="https://github.com/test-candidate",
        twitter_url="https://x.com/test_candidate",
        role_selection="Backend Engineer",
        parse_result={"skills": ["Python"]},
        parsed_total_years_experience=2.0,
        parsed_search_text="python fastapi",
        parse_status="completed",
        applicant_status=applicant_status,  # type: ignore[arg-type]
        evaluation_status=evaluation_status,  # type: ignore[arg-type]
        resume=ResumeFileMeta(
            original_filename="resume.pdf",
            stored_filename=f"{app_id}.pdf",
            storage_path=f"s3://hireme-cv-bucket/hireme/resumes/{app_id}.pdf",
            content_type="application/pdf",
            size_bytes=2048,
        ),
        created_at=datetime.now(tz=timezone.utc),
    )


class _FakeQueueClient:
    """Queue client fake that only tracks message deletion."""

    def __init__(self) -> None:
        self.deleted_receipt_handles: list[str] = []

    async def delete_message(self, receipt_handle: str) -> None:
        self.deleted_receipt_handles.append(receipt_handle)


class _FakeParseProcessor:
    """Parse processor fake returning fixed existence flag."""

    def __init__(self, *, exists: bool = True) -> None:
        self.exists = exists
        self.processed_ids: list[str] = []

    async def process(self, application_id) -> bool:
        self.processed_ids.append(str(application_id))
        return self.exists


class _FakeApplicationRepository:
    """Application repository fake returning one in-memory record."""

    def __init__(self, record: ApplicationRecord | None) -> None:
        self.record = record

    async def get_by_id(self, application_id):
        if self.record is None:
            return None
        if str(self.record.id) != str(application_id):
            return None
        return self.record


class _FakeEvaluationQueuePublisher:
    """Evaluation queue fake that stores published jobs."""

    def __init__(self) -> None:
        self.jobs: list[CandidateEvaluationJob] = []

    async def publish(self, job: CandidateEvaluationJob) -> None:
        self.jobs.append(job)


def test_parse_worker_auto_queues_evaluation_when_candidate_is_eligible() -> None:
    """Completed parse should enqueue AI evaluation for eligible candidate status."""

    async def run() -> None:
        candidate = _build_candidate(
            applicant_status="screened",
            evaluation_status=None,
        )
        queue_client = _FakeQueueClient()
        parse_processor = _FakeParseProcessor(exists=True)
        app_repo = _FakeApplicationRepository(candidate)
        evaluation_queue = _FakeEvaluationQueuePublisher()
        worker = SqsParseWorker(
            queue_client=queue_client,  # type: ignore[arg-type]
            parse_processor=parse_processor,  # type: ignore[arg-type]
            application_repository=app_repo,  # type: ignore[arg-type]
            evaluation_queue_publisher=evaluation_queue,  # type: ignore[arg-type]
            evaluation_queue_enabled=True,
            evaluation_target_statuses={"screened", "shortlisted"},
            evaluation_enqueue_timeout_seconds=1.0,
            max_in_flight=1,
            receive_batch_size=1,
            receive_wait_seconds=1,
            visibility_timeout_seconds=30,
        )

        message = SqsMessage(
            message_id="m1",
            receipt_handle="r1",
            body=json.dumps({"application_id": str(candidate.id)}),
        )
        await worker._process_message(message)

        assert len(evaluation_queue.jobs) == 1
        assert str(evaluation_queue.jobs[0].application_id) == str(candidate.id)
        assert queue_client.deleted_receipt_handles == ["r1"]

    asyncio.run(run())


def test_parse_worker_skips_evaluation_when_status_not_eligible() -> None:
    """Parse worker should skip enqueue when candidate status is not allowed."""

    async def run() -> None:
        candidate = _build_candidate(
            applicant_status="rejected",
            evaluation_status=None,
        )
        queue_client = _FakeQueueClient()
        parse_processor = _FakeParseProcessor(exists=True)
        app_repo = _FakeApplicationRepository(candidate)
        evaluation_queue = _FakeEvaluationQueuePublisher()
        worker = SqsParseWorker(
            queue_client=queue_client,  # type: ignore[arg-type]
            parse_processor=parse_processor,  # type: ignore[arg-type]
            application_repository=app_repo,  # type: ignore[arg-type]
            evaluation_queue_publisher=evaluation_queue,  # type: ignore[arg-type]
            evaluation_queue_enabled=True,
            evaluation_target_statuses={"screened", "shortlisted"},
            evaluation_enqueue_timeout_seconds=1.0,
            max_in_flight=1,
            receive_batch_size=1,
            receive_wait_seconds=1,
            visibility_timeout_seconds=30,
        )

        message = SqsMessage(
            message_id="m2",
            receipt_handle="r2",
            body=json.dumps({"application_id": str(candidate.id)}),
        )
        await worker._process_message(message)

        assert evaluation_queue.jobs == []
        assert queue_client.deleted_receipt_handles == ["r2"]

    asyncio.run(run())


def test_parse_worker_skips_evaluation_when_already_completed() -> None:
    """Parse worker should not enqueue duplicate evaluation when already completed."""

    async def run() -> None:
        candidate = _build_candidate(
            applicant_status="screened",
            evaluation_status="completed",
        )
        queue_client = _FakeQueueClient()
        parse_processor = _FakeParseProcessor(exists=True)
        app_repo = _FakeApplicationRepository(candidate)
        evaluation_queue = _FakeEvaluationQueuePublisher()
        worker = SqsParseWorker(
            queue_client=queue_client,  # type: ignore[arg-type]
            parse_processor=parse_processor,  # type: ignore[arg-type]
            application_repository=app_repo,  # type: ignore[arg-type]
            evaluation_queue_publisher=evaluation_queue,  # type: ignore[arg-type]
            evaluation_queue_enabled=True,
            evaluation_target_statuses={"screened", "shortlisted"},
            evaluation_enqueue_timeout_seconds=1.0,
            max_in_flight=1,
            receive_batch_size=1,
            receive_wait_seconds=1,
            visibility_timeout_seconds=30,
        )

        message = SqsMessage(
            message_id="m3",
            receipt_handle="r3",
            body=json.dumps({"application_id": str(candidate.id)}),
        )
        await worker._process_message(message)

        assert evaluation_queue.jobs == []
        assert queue_client.deleted_receipt_handles == ["r3"]

    asyncio.run(run())
