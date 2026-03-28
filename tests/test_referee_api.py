"""Tests for referee authentication and referral endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI
from fastapi import HTTPException, status
from fastapi.testclient import TestClient

from app.api.deps import (
    get_admin_auth_service,
    get_application_service_dep,
    get_referee_auth_service,
    get_reference_service_dep,
)
from app.api.v1.referee import router as referee_router
from app.core.runtime_config import get_runtime_config
from app.core.settings import get_settings
from app.schemas.application import (
    ApplicationListResponse,
    ApplicationRecord,
    ResumeFileMeta,
)
from app.schemas.reference import ReferenceListResponse, ReferenceRecord


class _FakeApplicationService:
    """Minimal application service for referee endpoint tests."""

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
            parse_status="completed",
            evaluation_status="completed",
            applicant_status="shortlisted",
            ai_score=88.0,
            candidate_brief="Strong backend profile with relevant projects.",
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


class _FakeReferenceService:
    """Minimal reference service for referee endpoint tests."""

    def __init__(self, *, application_id, candidate_email: str) -> None:
        self.items: list[ReferenceRecord] = []
        self._application_id = application_id
        self._candidate_email = candidate_email

    async def create_from_referee(self, payload):
        applicant_email = str(payload.applicant_email).strip().casefold()
        if applicant_email != self._candidate_email.strip().casefold():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Sorry, no applicant found with this email.",
            )
        record = ReferenceRecord(
            id=uuid4(),
            application_id=self._application_id,
            candidate_email=self._candidate_email,
            candidate_name=payload.applicant_name,
            candidate_position=payload.applicant_position,
            referee_name=payload.referee_name,
            referee_email=payload.referee_email,
            referee_phone=None,
            referee_linkedin_url=None,
            referee_company=None,
            referee_position=None,
            relationship=None,
            notes=payload.referee_note,
            created_at=datetime.now(tz=timezone.utc),
        )
        self.items.append(record)
        return record

    async def list(self, *, application_id, offset: int = 0, limit: int = 20):
        filtered = [item for item in self.items if str(item.application_id) == str(application_id)]
        return ReferenceListResponse(
            items=filtered[offset : offset + limit],
            total=len(filtered),
            offset=offset,
            limit=limit,
        )


def _reset_cached_config() -> None:
    """Clear cached settings/runtime for env-based tests."""

    get_settings.cache_clear()
    get_runtime_config.cache_clear()
    get_admin_auth_service.cache_clear()
    get_referee_auth_service.cache_clear()


def _set_referee_test_env(monkeypatch) -> None:
    """Set deterministic referee env vars for auth endpoint tests."""

    monkeypatch.setenv("REFEREE_USERNAME", "referee")
    monkeypatch.setenv("REFEREE_PASSWORD", "StrongSecret123!")
    monkeypatch.setenv("ADMIN_JWT_SECRET", "this-is-a-long-test-secret-value-123456")
    monkeypatch.setenv("REFEREE_PASSWORD_HASH", "")


def _build_client() -> tuple[TestClient, _FakeApplicationService, _FakeReferenceService]:
    """Create test client with fake dependency overrides."""

    app = FastAPI()
    app.include_router(referee_router, prefix="/api/v1")
    fake_app_service = _FakeApplicationService()
    fake_reference_service = _FakeReferenceService(
        application_id=fake_app_service._record.id,
        candidate_email=fake_app_service._record.email,
    )
    app.dependency_overrides[get_application_service_dep] = lambda: fake_app_service
    app.dependency_overrides[get_reference_service_dep] = lambda: fake_reference_service
    return TestClient(app), fake_app_service, fake_reference_service


def test_referee_login_returns_bearer_token(monkeypatch) -> None:
    """Valid referee login should return token payload."""

    _set_referee_test_env(monkeypatch)
    _reset_cached_config()
    client, _, _ = _build_client()

    response = client.post(
        "/api/v1/referee/login",
        json={"username": "referee", "password": "StrongSecret123!"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["role"] == "referee"
    assert body["access_token"]


def test_referee_can_list_candidates_and_submit_reference(monkeypatch) -> None:
    """Referee should list candidates and submit a reference for an existing applicant."""

    _set_referee_test_env(monkeypatch)
    _reset_cached_config()
    client, fake_app_service, _ = _build_client()

    login_response = client.post(
        "/api/v1/referee/login",
        json={"username": "referee", "password": "StrongSecret123!"},
    )
    token = login_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    list_response = client.get("/api/v1/referee/candidates", headers=headers)
    assert list_response.status_code == 200
    list_body = list_response.json()
    assert list_body["total"] == 1

    create_response = client.post(
        "/api/v1/referee/references",
        headers=headers,
        json={
            "applicant_email": fake_app_service._record.email,
            "applicant_name": fake_app_service._record.full_name,
            "applicant_position": fake_app_service._record.role_selection,
            "referee_name": "Referee One",
            "referee_email": "ref1@example.com",
            "referee_note": "Strong referral from prior project collaboration.",
        },
    )
    assert create_response.status_code == 201
    created = create_response.json()
    assert created["referee_name"] == "Referee One"

    references_response = client.get(
        f"/api/v1/referee/references?application_id={fake_app_service._record.id}",
        headers=headers,
    )
    assert references_response.status_code == 200
    refs_body = references_response.json()
    assert refs_body["total"] == 1


def test_referee_submit_reference_returns_not_found_for_unknown_applicant_email(
    monkeypatch,
) -> None:
    """Referee submission should return clear error when applicant email does not exist."""

    _set_referee_test_env(monkeypatch)
    _reset_cached_config()
    client, _, _ = _build_client()

    login_response = client.post(
        "/api/v1/referee/login",
        json={"username": "referee", "password": "StrongSecret123!"},
    )
    token = login_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create_response = client.post(
        "/api/v1/referee/references",
        headers=headers,
        json={
            "applicant_email": "missing@example.com",
            "applicant_name": "Missing Person",
            "applicant_position": "Backend Engineer",
            "referee_name": "Referee One",
            "referee_email": "ref1@example.com",
            "referee_note": "I recommend this candidate.",
        },
    )
    assert create_response.status_code == 422
    body = create_response.json()
    assert body["detail"] == "Sorry, no applicant found with this email."
