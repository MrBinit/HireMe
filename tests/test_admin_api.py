"""Tests for admin login and RBAC-protected candidate endpoints."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import (
    get_admin_auth_service,
    get_application_service_dep,
    get_candidate_evaluation_service_dep,
    get_evaluation_queue_publisher_dep,
    get_research_queue_publisher_dep,
    get_scheduling_queue_publisher_dep,
)
from app.api.v1.admin import router as admin_router
from app.core.runtime_config import get_runtime_config
from app.core.settings import get_settings
from app.schemas.application import ApplicationListResponse, ApplicationRecord, ResumeFileMeta
from app.schemas.evaluation import (
    CandidateEvaluationResult,
    EvaluationBreakdown,
    EvaluationEvidence,
)
from app.services.evaluation_queue import CandidateEvaluationJob
from app.services.research_queue import CandidateResearchEnrichmentJob
from app.services.scheduling_queue import CandidateInterviewSchedulingJob


class _FakeApplicationService:
    """Minimal application service for admin endpoint tests."""

    def __init__(self) -> None:
        app_id = uuid4()
        self._record = ApplicationRecord(
            id=app_id,
            job_opening_id=uuid4(),
            full_name="Candidate One",
            email="candidate@example.com",
            linkedin_url="https://linkedin.com/in/candidate-one",
            portfolio_url="https://candidate.dev",
            github_url="https://github.com/candidate-one",
            twitter_url=None,
            role_selection="Backend Engineer",
            parse_result=None,
            parse_status="pending",
            applicant_status="shortlisted",
            reference_status=False,
            resume=ResumeFileMeta(
                original_filename="resume.pdf",
                stored_filename=f"{app_id}.pdf",
                storage_path=f"s3://hireme-cv-bucket/hireme/resumes/{app_id}.pdf",
                content_type="application/pdf",
                size_bytes=1024,
            ),
            created_at=datetime.now(tz=timezone.utc),
        )

    async def list(
        self,
        *,
        offset: int = 0,
        limit: int | None = None,
        job_opening_id=None,
        role_selection=None,
        applicant_status=None,
        submitted_from=None,
        submitted_to=None,
        keyword_search=None,
        experience_within_range=None,
        prefilter_by_job_opening=False,
    ) -> ApplicationListResponse:
        _ = (
            job_opening_id,
            role_selection,
            applicant_status,
            submitted_from,
            submitted_to,
            keyword_search,
            experience_within_range,
            prefilter_by_job_opening,
        )
        effective_limit = 1 if limit is None else limit
        return ApplicationListResponse(
            items=[self._record][offset : offset + effective_limit],
            total=1,
            offset=offset,
            limit=effective_limit,
        )

    async def get_by_id(self, application_id):
        if str(application_id) != str(self._record.id):
            return None
        return self._record

    async def update_applicant_status(self, *, application_id, applicant_status, note=None):
        _ = note
        if str(application_id) != str(self._record.id):
            return None
        self._record = self._record.model_copy(update={"applicant_status": applicant_status})
        return self._record

    async def update_admin_review(self, *, application_id, updates):
        if str(application_id) != str(self._record.id):
            return None
        self._record = self._record.model_copy(update=updates)
        return self._record

    async def record_manager_decision(
        self,
        *,
        application_id,
        decision,
        note=None,
        selection_details=None,
    ):
        if str(application_id) != str(self._record.id):
            return None
        if self._record.interview_schedule_status != "interview_done":
            self._record = self._record.model_copy(
                update={"interview_schedule_status": "interview_done"}
            )

        updates = {
            "manager_decision": decision,
            "manager_decision_note": note,
            "manager_selection_details": selection_details,
            "manager_selection_template_output": (
                "Subject: Offer of Employment - Backend Engineer II\n\n"
                "Dear Candidate One,\n\n"
                "We are pleased to offer you the position of Backend Engineer II at HireMe."
                if decision == "select"
                else None
            ),
            "offer_letter_status": "created" if decision == "select" else "rejected",
            "offer_letter_storage_path": (
                "s3://hireme-cv-bucket/offer-letters/fake-candidate.pdf"
                if decision == "select"
                else None
            ),
            "applicant_status": "offer_letter_created" if decision == "select" else "rejected",
        }
        self._record = self._record.model_copy(update=updates)
        return self._record

    async def set_fireflies_demo_state(self, *, application_id, mock_completed):
        if str(application_id) != str(self._record.id):
            return None
        if mock_completed:
            self._record = self._record.model_copy(
                update={
                    "interview_schedule_status": "interview_done",
                    "interview_transcript_status": "completed",
                    "interview_transcript_url": f"https://app.fireflies.ai/view/mock-{self._record.id}",
                    "interview_transcript_summary": "Mock transcript summary for demo flow.",
                }
            )
        else:
            self._record = self._record.model_copy(
                update={
                    "interview_schedule_status": "interview_booked",
                    "interview_transcript_status": "processing",
                    "interview_transcript_url": None,
                    "interview_transcript_summary": None,
                }
            )
        return self._record


class _FakeCandidateEvaluationService:
    """Minimal evaluator service for admin endpoint tests."""

    async def evaluate_application(self, *, application_id):
        _ = application_id
        return CandidateEvaluationResult(
            score=84,
            breakdown=EvaluationBreakdown(
                skills=34,
                experience=24,
                education=8,
                role_alignment=18,
            ),
            evidence=EvaluationEvidence(
                skills=["Strong skill overlap"],
                experience=["Experience in target range"],
                education=["Relevant CS background"],
                role_alignment=["Prior backend roles"],
            ),
            confidence=0.89,
            needs_human_review=False,
            reason="Good fit for the role.",
        )

    async def validate_candidate_for_evaluation(self, *, application_id):
        _ = application_id
        return None


class _FakeEvaluationQueuePublisher:
    """Minimal evaluation queue publisher for admin endpoint tests."""

    def __init__(self) -> None:
        self.jobs: list[CandidateEvaluationJob] = []

    async def publish(self, job: CandidateEvaluationJob) -> None:
        self.jobs.append(job)


class _FakeResearchQueuePublisher:
    """Minimal research queue publisher for admin endpoint tests."""

    def __init__(self) -> None:
        self.jobs: list[CandidateResearchEnrichmentJob] = []

    async def publish(self, job: CandidateResearchEnrichmentJob) -> None:
        self.jobs.append(job)


class _FakeSchedulingQueuePublisher:
    """Minimal scheduling queue publisher for admin endpoint tests."""

    def __init__(self) -> None:
        self.jobs: list[CandidateInterviewSchedulingJob] = []

    async def publish(self, job: CandidateInterviewSchedulingJob) -> None:
        self.jobs.append(job)


def _build_client() -> tuple[
    TestClient,
    _FakeApplicationService,
    _FakeEvaluationQueuePublisher,
    _FakeResearchQueuePublisher,
]:
    """Create test client with fake application service override."""

    app = FastAPI()
    app.include_router(admin_router, prefix="/api/v1")

    fake_service = _FakeApplicationService()
    fake_queue = _FakeEvaluationQueuePublisher()
    fake_research_queue = _FakeResearchQueuePublisher()
    app.dependency_overrides[get_application_service_dep] = lambda: fake_service
    app.dependency_overrides[get_candidate_evaluation_service_dep] = (
        lambda: _FakeCandidateEvaluationService()
    )
    app.dependency_overrides[get_evaluation_queue_publisher_dep] = lambda: fake_queue
    app.dependency_overrides[get_research_queue_publisher_dep] = lambda: fake_research_queue
    return TestClient(app), fake_service, fake_queue, fake_research_queue


def _reset_cached_config() -> None:
    """Clear cached settings/runtime for env-based tests."""

    get_settings.cache_clear()
    get_runtime_config.cache_clear()
    get_admin_auth_service.cache_clear()


def _set_admin_test_env(monkeypatch) -> None:
    """Set deterministic admin env vars for auth endpoint tests."""

    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "StrongSecret123!")
    monkeypatch.setenv("ADMIN_JWT_SECRET", "this-is-a-long-test-secret-value-123456")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "")


def test_admin_login_returns_bearer_token(monkeypatch) -> None:
    """Valid admin login should return token payload."""

    _set_admin_test_env(monkeypatch)
    _reset_cached_config()

    client, _, _, _ = _build_client()
    response = client.post(
        "/api/v1/admin/login",
        json={"username": "admin", "password": "StrongSecret123!"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["role"] == "admin"
    assert body["access_token"]


def test_admin_login_rejects_bad_credentials(monkeypatch) -> None:
    """Invalid admin password should return unauthorized."""

    _set_admin_test_env(monkeypatch)
    _reset_cached_config()

    client, _, _, _ = _build_client()
    response = client.post(
        "/api/v1/admin/login",
        json={"username": "admin", "password": "wrong-password"},
    )

    assert response.status_code == 401


def test_admin_candidates_requires_bearer_token(monkeypatch) -> None:
    """Candidate list endpoint should reject requests without auth token."""

    _set_admin_test_env(monkeypatch)
    _reset_cached_config()

    client, _, _, _ = _build_client()
    response = client.get("/api/v1/admin/candidates")

    assert response.status_code == 401


def test_admin_candidates_returns_data_with_token(monkeypatch) -> None:
    """Admin candidates endpoint should return candidate data for valid token."""

    _set_admin_test_env(monkeypatch)
    _reset_cached_config()

    client, _, _, _ = _build_client()
    login_response = client.post(
        "/api/v1/admin/login",
        json={"username": "admin", "password": "StrongSecret123!"},
    )
    token = login_response.json()["access_token"]

    response = client.get(
        "/api/v1/admin/candidates",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == "candidate@example.com"


def test_admin_candidate_evaluation_endpoint_enqueues_job(monkeypatch) -> None:
    """Admin evaluate endpoint should enqueue candidate evaluation job."""

    _set_admin_test_env(monkeypatch)
    _reset_cached_config()

    client, fake_service, fake_queue, _ = _build_client()
    login_response = client.post(
        "/api/v1/admin/login",
        json={"username": "admin", "password": "StrongSecret123!"},
    )
    token = login_response.json()["access_token"]

    response = client.post(
        f"/api/v1/admin/candidates/{fake_service._record.id}/evaluate",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["application_id"] == str(fake_service._record.id)
    assert body["queued"] is True
    assert len(fake_queue.jobs) == 1
    assert str(fake_queue.jobs[0].application_id) == str(fake_service._record.id)


def test_admin_candidate_evaluation_queue_endpoint_enqueues_job(monkeypatch) -> None:
    """Admin evaluate queue endpoint should enqueue candidate evaluation job."""

    _set_admin_test_env(monkeypatch)
    _reset_cached_config()

    client, fake_service, fake_queue, _ = _build_client()
    login_response = client.post(
        "/api/v1/admin/login",
        json={"username": "admin", "password": "StrongSecret123!"},
    )
    token = login_response.json()["access_token"]

    response = client.post(
        f"/api/v1/admin/candidates/{fake_service._record.id}/evaluate/queue",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["application_id"] == str(fake_service._record.id)
    assert body["queued"] is True
    assert len(fake_queue.jobs) == 1
    assert str(fake_queue.jobs[0].application_id) == str(fake_service._record.id)


def test_admin_candidate_research_endpoint_enqueues_job(monkeypatch) -> None:
    """Admin research endpoint should enqueue candidate research job."""

    _set_admin_test_env(monkeypatch)
    _reset_cached_config()

    client, fake_service, _, fake_research_queue = _build_client()
    login_response = client.post(
        "/api/v1/admin/login",
        json={"username": "admin", "password": "StrongSecret123!"},
    )
    token = login_response.json()["access_token"]

    response = client.post(
        f"/api/v1/admin/candidates/{fake_service._record.id}/research",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["application_id"] == str(fake_service._record.id)
    assert body["queued"] is True
    assert len(fake_research_queue.jobs) == 1
    assert str(fake_research_queue.jobs[0].application_id) == str(fake_service._record.id)


def test_admin_candidate_research_queue_endpoint_enqueues_job(monkeypatch) -> None:
    """Admin research queue endpoint should enqueue candidate research job."""

    _set_admin_test_env(monkeypatch)
    _reset_cached_config()

    client, fake_service, _, fake_research_queue = _build_client()
    login_response = client.post(
        "/api/v1/admin/login",
        json={"username": "admin", "password": "StrongSecret123!"},
    )
    token = login_response.json()["access_token"]

    response = client.post(
        f"/api/v1/admin/candidates/{fake_service._record.id}/research/queue",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["application_id"] == str(fake_service._record.id)
    assert body["queued"] is True
    assert len(fake_research_queue.jobs) == 1
    assert str(fake_research_queue.jobs[0].application_id) == str(fake_service._record.id)


def test_admin_manager_decision_select_endpoint_updates_candidate(monkeypatch) -> None:
    """Manager decision endpoint should accept select with required details."""

    _set_admin_test_env(monkeypatch)
    _reset_cached_config()

    client, fake_service, _, _ = _build_client()
    login_response = client.post(
        "/api/v1/admin/login",
        json={"username": "admin", "password": "StrongSecret123!"},
    )
    token = login_response.json()["access_token"]

    response = client.patch(
        f"/api/v1/admin/candidates/{fake_service._record.id}/manager-decision",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "decision": "select",
            "note": "Move to offer.",
            "selection_details": {
                "confirmed_job_title": "Backend Engineer II",
                "start_date": "2026-05-01",
                "base_salary": "USD 140,000",
                "compensation_structure": "Base + 10% yearly bonus",
                "equity_or_bonus": "0.1% equity",
                "reporting_manager": "VP Engineering",
                "custom_terms": "Remote-first role",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["manager_decision"] == "select"
    assert body["applicant_status"] == "offer_letter_created"
    assert body["offer_letter_status"] == "created"
    assert body["manager_selection_details"]["confirmed_job_title"] == "Backend Engineer II"
    assert "Offer of Employment - Backend Engineer II" in body["manager_selection_template_output"]


def test_admin_manager_decision_select_requires_selection_details(monkeypatch) -> None:
    """Manager select decision should fail validation without selection details."""

    _set_admin_test_env(monkeypatch)
    _reset_cached_config()

    client, fake_service, _, _ = _build_client()
    login_response = client.post(
        "/api/v1/admin/login",
        json={"username": "admin", "password": "StrongSecret123!"},
    )
    token = login_response.json()["access_token"]

    response = client.patch(
        f"/api/v1/admin/candidates/{fake_service._record.id}/manager-decision",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "decision": "select",
            "note": "Move to offer.",
        },
    )

    assert response.status_code == 422


def test_admin_fireflies_demo_true_marks_interview_done(monkeypatch) -> None:
    """Fireflies demo=true should mark interview done with completed transcript fields."""

    _set_admin_test_env(monkeypatch)
    _reset_cached_config()

    client, fake_service, _, _ = _build_client()
    login_response = client.post(
        "/api/v1/admin/login",
        json={"username": "admin", "password": "StrongSecret123!"},
    )
    token = login_response.json()["access_token"]

    response = client.patch(
        f"/api/v1/admin/candidates/{fake_service._record.id}/fireflies-demo",
        headers={"Authorization": f"Bearer {token}"},
        json={"mock_completed": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["interview_schedule_status"] == "interview_done"
    assert body["interview_transcript_status"] == "completed"
    assert "mock-" in str(body["interview_transcript_url"])


def test_admin_fireflies_demo_false_sets_processing(monkeypatch) -> None:
    """Fireflies demo=false should keep transcript in processing state."""

    _set_admin_test_env(monkeypatch)
    _reset_cached_config()

    client, fake_service, _, _ = _build_client()
    login_response = client.post(
        "/api/v1/admin/login",
        json={"username": "admin", "password": "StrongSecret123!"},
    )
    token = login_response.json()["access_token"]

    response = client.patch(
        f"/api/v1/admin/candidates/{fake_service._record.id}/fireflies-demo",
        headers={"Authorization": f"Bearer {token}"},
        json={"mock_completed": False},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["interview_schedule_status"] == "interview_booked"
    assert body["interview_transcript_status"] == "processing"
    assert body["interview_transcript_url"] is None


def test_admin_candidate_get_returns_typed_research_summary(monkeypatch) -> None:
    """Candidate GET should include parsed research_summary object for frontend usage."""

    _set_admin_test_env(monkeypatch)
    _reset_cached_config()

    client, fake_service, _, _ = _build_client()
    fake_service._record = fake_service._record.model_copy(
        update={
            "online_research_summary": json.dumps(
                {
                    "brief": "Candidate has relevant OSS contributions.",
                    "deterministic_checks": {
                        "manual_review_required": False,
                        "confidence_baseline": "medium",
                    },
                    "llm_analysis": {"confidence": "high", "issues": []},
                }
            )
        }
    )

    login_response = client.post(
        "/api/v1/admin/login",
        json={"username": "admin", "password": "StrongSecret123!"},
    )
    token = login_response.json()["access_token"]

    response = client.get(
        f"/api/v1/admin/candidates/{fake_service._record.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["online_research_summary"]
    assert body["research_summary"]["brief"] == "Candidate has relevant OSS contributions."
    assert body["research_summary"]["llm_analysis"]["confidence"] == "high"


def test_admin_schedule_endpoint_blocks_manual_review_required(monkeypatch) -> None:
    """Scheduling queue should reject candidates gated by manual review requirement."""

    _set_admin_test_env(monkeypatch)
    _reset_cached_config()

    client, fake_service, _, _ = _build_client()
    scheduling_queue = _FakeSchedulingQueuePublisher()
    client.app.dependency_overrides[get_scheduling_queue_publisher_dep] = lambda: scheduling_queue
    fake_service._record = fake_service._record.model_copy(
        update={
            "online_research_summary": json.dumps(
                {
                    "deterministic_checks": {
                        "manual_review_required": True,
                        "confidence_baseline": "medium",
                    }
                }
            )
        }
    )

    login_response = client.post(
        "/api/v1/admin/login",
        json={"username": "admin", "password": "StrongSecret123!"},
    )
    token = login_response.json()["access_token"]

    response = client.post(
        f"/api/v1/admin/candidates/{fake_service._record.id}/schedule",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert "explicit reviewer action" in response.json()["detail"]
    assert len(scheduling_queue.jobs) == 0


def test_admin_schedule_endpoint_blocks_low_confidence(monkeypatch) -> None:
    """Scheduling queue should reject candidates gated by low confidence."""

    _set_admin_test_env(monkeypatch)
    _reset_cached_config()

    client, fake_service, _, _ = _build_client()
    scheduling_queue = _FakeSchedulingQueuePublisher()
    client.app.dependency_overrides[get_scheduling_queue_publisher_dep] = lambda: scheduling_queue
    fake_service._record = fake_service._record.model_copy(
        update={
            "online_research_summary": json.dumps(
                {
                    "deterministic_checks": {"manual_review_required": False},
                    "llm_analysis": {"confidence": "low"},
                }
            )
        }
    )

    login_response = client.post(
        "/api/v1/admin/login",
        json={"username": "admin", "password": "StrongSecret123!"},
    )
    token = login_response.json()["access_token"]

    response = client.post(
        f"/api/v1/admin/candidates/{fake_service._record.id}/schedule",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert "explicit reviewer action" in response.json()["detail"]
    assert len(scheduling_queue.jobs) == 0
