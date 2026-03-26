"""Tests for admin login and RBAC-protected candidate endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import (
    get_admin_auth_service,
    get_application_service_dep,
    get_candidate_evaluation_service_dep,
    get_evaluation_queue_publisher_dep,
)
from app.api.v1.admin import router as admin_router
from app.core.runtime_config import get_runtime_config
from app.core.settings import get_settings
from app.schemas.application import ApplicationListResponse, ApplicationRecord, ResumeFileMeta
from app.schemas.evaluation import CandidateEvaluationResult, EvaluationBreakdown
from app.services.evaluation_queue import CandidateEvaluationJob


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
            applicant_status="received",
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


def _build_client() -> tuple[TestClient, _FakeApplicationService, _FakeEvaluationQueuePublisher]:
    """Create test client with fake application service override."""

    app = FastAPI()
    app.include_router(admin_router, prefix="/api/v1")

    fake_service = _FakeApplicationService()
    fake_queue = _FakeEvaluationQueuePublisher()
    app.dependency_overrides[get_application_service_dep] = lambda: fake_service
    app.dependency_overrides[get_candidate_evaluation_service_dep] = (
        lambda: _FakeCandidateEvaluationService()
    )
    app.dependency_overrides[get_evaluation_queue_publisher_dep] = lambda: fake_queue
    return TestClient(app), fake_service, fake_queue


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

    client, _, _ = _build_client()
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

    client, _, _ = _build_client()
    response = client.post(
        "/api/v1/admin/login",
        json={"username": "admin", "password": "wrong-password"},
    )

    assert response.status_code == 401


def test_admin_candidates_requires_bearer_token(monkeypatch) -> None:
    """Candidate list endpoint should reject requests without auth token."""

    _set_admin_test_env(monkeypatch)
    _reset_cached_config()

    client, _, _ = _build_client()
    response = client.get("/api/v1/admin/candidates")

    assert response.status_code == 401


def test_admin_candidates_returns_data_with_token(monkeypatch) -> None:
    """Admin candidates endpoint should return candidate data for valid token."""

    _set_admin_test_env(monkeypatch)
    _reset_cached_config()

    client, _, _ = _build_client()
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

    client, fake_service, fake_queue = _build_client()
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

    client, fake_service, fake_queue = _build_client()
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
