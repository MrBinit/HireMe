"""Tests for Bedrock-backed candidate evaluation service."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import ValidationError

from app.core.error import ApplicationValidationError
from app.core.runtime_config import (
    ApplicationRuntimeConfig,
    BedrockRuntimeConfig,
    EvaluationRuntimeConfig,
)
from app.schemas.application import ApplicationRecord, ResumeFileMeta
from app.schemas.job_opening import JobOpeningRecord
from app.services.candidate_evaluation_service import CandidateEvaluationService


class _FakeApplicationRepository:
    def __init__(self, record: ApplicationRecord | None):
        self._record = record

    async def get_by_id(self, application_id):
        _ = application_id
        return self._record


class _FakeJobOpeningRepository:
    def __init__(self, record: JobOpeningRecord | None):
        self._record = record

    async def get(self, job_opening_id):
        _ = job_opening_id
        return self._record


class _FakeBedrockClient:
    def __init__(self, responses: list[object]):
        self._responses = responses
        self.calls: list[str] = []
        self.payloads: list[dict] = []

    async def invoke_json(self, *, model_id, payload):
        self.calls.append(model_id)
        self.payloads.append(payload)
        value = self._responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _application_record() -> ApplicationRecord:
    app_id = uuid4()
    return ApplicationRecord(
        id=app_id,
        job_opening_id=uuid4(),
        full_name="Alice Candidate",
        email="alice@example.com",
        linkedin_url="https://linkedin.com/in/alice",
        portfolio_url="https://alice.dev",
        github_url="https://github.com/alice",
        twitter_url=None,
        role_selection="Backend Engineer",
        parse_result={
            "skills": ["Python", "FastAPI", "PostgreSQL"],
            "total_years_experience": 3.1,
            "initial_screening": {"passed": True},
            "work_experience": [
                {
                    "position": "Backend Engineer",
                    "company": "Example Inc",
                    "job_description": ["Built async APIs", "Optimized SQL queries"],
                }
            ],
            "education": [
                {"degree": "BSc Computer Science", "institution": "State University"},
            ],
        },
        parsed_total_years_experience=3.1,
        parsed_search_text="python fastapi postgresql",
        parse_status="completed",
        applicant_status="screened",
        rejection_reason=None,
        reference_status=False,
        resume=ResumeFileMeta(
            original_filename="alice.pdf",
            stored_filename=f"{app_id}.pdf",
            storage_path=f"s3://hireme-cv-bucket/hireme/resumes/{app_id}.pdf",
            content_type="application/pdf",
            size_bytes=1000,
        ),
        created_at=datetime.now(tz=timezone.utc),
    )


def _job_opening_record(job_opening_id) -> JobOpeningRecord:
    now = datetime.now(tz=timezone.utc)
    return JobOpeningRecord(
        id=job_opening_id,
        role_title="Backend Engineer",
        manager_email="manager@example.com",
        team="Platform",
        location="remote",
        experience_level="mid",
        experience_range="2-4 years",
        application_open_at=now,
        application_close_at=now,
        responsibilities=["Build async APIs"],
        requirements=["Python", "FastAPI", "PostgreSQL"],
        paused=False,
        status="open",
        created_at=now,
        updated_at=now,
    )


def _build_service(*, bedrock_client, candidate_record=None, opening_record=None):
    return CandidateEvaluationService(
        application_repository=_FakeApplicationRepository(candidate_record),
        job_opening_repository=_FakeJobOpeningRepository(opening_record),
        bedrock_client=bedrock_client,
        bedrock_config=BedrockRuntimeConfig(
            enabled=True,
            primary_model_id="primary-model",
            fallback_model_id="fallback-model",
            request_timeout_seconds=3.0,
            max_concurrency=2,
        ),
        evaluation_config=EvaluationRuntimeConfig(
            enabled=True,
            summary_prompt_template="Summarize\n{work_history_json}",
            prompt_template=(
                "Skills: {skills}\nExperience: {years}\n"
                "Work: {work_summary}\nEducation: {education}\n"
                "Role: {role}\nMust: {must_have_skills}\nNice: {nice_to_have_skills}\n"
                "Req: {required_skills}\nRange: {min_exp}-{max_exp}\n"
                "Description: {job_description}"
            ),
        ),
        application_config=ApplicationRuntimeConfig(),
    )


def test_candidate_evaluation_success() -> None:
    """Primary model response should be parsed into validated result."""

    async def run() -> None:
        candidate = _application_record()
        opening = _job_opening_record(candidate.job_opening_id)
        fake_client = _FakeBedrockClient(
            responses=[
                {
                    "content": [
                        {"type": "text", "text": "Backend Engineer at Example Inc (3 years)."}
                    ]
                },
                {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                '{"score": 86, "breakdown": {"skills": 34, "experience": 24, '
                                '"education": 8, "role_alignment": 20}, '
                                '"evidence": {"skills": ["Python and FastAPI overlap"], '
                                '"experience": ["3.1 years parsed experience"], '
                                '"education": ["BSc Computer Science"], '
                                '"role_alignment": ["Backend Engineer experience"]}, '
                                '"confidence": 0.91, "needs_human_review": false, '
                                '"reason": "Strong overlap."}'
                            ),
                        }
                    ]
                },
            ]
        )
        service = _build_service(
            bedrock_client=fake_client,
            candidate_record=candidate,
            opening_record=opening,
        )

        result = await service.evaluate_application(application_id=candidate.id)
        assert result.score == 86
        assert result.breakdown.skills == 34
        assert fake_client.calls == ["fallback-model", "primary-model"]

    asyncio.run(run())


def test_candidate_evaluation_uses_fallback_for_summary_only() -> None:
    """Fallback model should be used for summary, primary for scoring."""

    async def run() -> None:
        candidate = _application_record()
        opening = _job_opening_record(candidate.job_opening_id)
        fake_client = _FakeBedrockClient(
            responses=[
                {
                    "content": [
                        {
                            "type": "text",
                            "text": "Associate Engineer at Example Inc with API delivery impact.",
                        }
                    ],
                },
                {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                '{"score": 72, "breakdown": {"skills": 28, "experience": 22, '
                                '"education": 8, "role_alignment": 14}, '
                                '"evidence": {"skills": ["Python baseline fit"], '
                                '"experience": ["Experience in target range"], '
                                '"education": ["Relevant CS degree"], '
                                '"role_alignment": ["Similar backend role history"]}, '
                                '"confidence": 0.76, "needs_human_review": false, '
                                '"reason": "Good baseline fit."}'
                            ),
                        }
                    ],
                },
            ],
        )
        service = _build_service(
            bedrock_client=fake_client,
            candidate_record=candidate,
            opening_record=opening,
        )

        result = await service.evaluate_application(application_id=candidate.id)
        assert result.score == 72
        assert fake_client.calls == ["fallback-model", "primary-model"]

    asyncio.run(run())


def test_candidate_evaluation_primary_has_no_score_fallback() -> None:
    """Primary scoring failure should not fallback to secondary model for scoring."""

    async def run() -> None:
        candidate = _application_record()
        opening = _job_opening_record(candidate.job_opening_id)
        fake_client = _FakeBedrockClient(
            responses=[
                {"content": [{"type": "text", "text": "Summary text."}]},
                RuntimeError("primary scoring failed"),
            ]
        )
        service = _build_service(
            bedrock_client=fake_client,
            candidate_record=candidate,
            opening_record=opening,
        )

        error = None
        try:
            await service.evaluate_application(application_id=candidate.id)
        except ApplicationValidationError as exc:
            error = exc

        assert error is not None
        assert "primary scoring failed" in str(error)
        assert fake_client.calls == ["fallback-model", "primary-model"]

    asyncio.run(run())


def test_candidate_evaluation_requires_completed_parse() -> None:
    """Evaluation should fail when parse is not completed."""

    async def run() -> None:
        candidate = _application_record().model_copy(update={"parse_status": "pending"})
        opening = _job_opening_record(candidate.job_opening_id)
        fake_client = _FakeBedrockClient(responses=[])
        service = _build_service(
            bedrock_client=fake_client,
            candidate_record=candidate,
            opening_record=opening,
        )

        caught = None
        try:
            await service.evaluate_application(application_id=candidate.id)
        except ApplicationValidationError as exc:
            caught = exc
        assert caught is not None
        assert "parse is not completed" in str(caught)

    asyncio.run(run())


def test_candidate_evaluation_requires_initial_screening_pass() -> None:
    """Evaluation should fail when initial screening did not pass."""

    async def run() -> None:
        candidate = _application_record().model_copy(
            update={"parse_result": {"initial_screening": {"passed": False}}}
        )
        opening = _job_opening_record(candidate.job_opening_id)
        fake_client = _FakeBedrockClient(responses=[])
        service = _build_service(
            bedrock_client=fake_client,
            candidate_record=candidate,
            opening_record=opening,
        )

        caught = None
        try:
            await service.evaluate_application(application_id=candidate.id)
        except ApplicationValidationError as exc:
            caught = exc
        assert caught is not None
        assert "failed initial screening" in str(caught)

    asyncio.run(run())


def test_candidate_evaluation_includes_must_and_nice_requirements_in_prompt() -> None:
    """Prompt should include split must-have and nice-to-have requirement context."""

    async def run() -> None:
        candidate = _application_record()
        opening = _job_opening_record(candidate.job_opening_id).model_copy(
            update={"requirements": ["Must: Python", "Nice to have: Rust"]}
        )
        fake_client = _FakeBedrockClient(
            responses=[
                {"content": [{"type": "text", "text": "Summary text."}]},
                {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                '{"score": 74, "breakdown": {"skills": 30, "experience": 22, '
                                '"education": 8, "role_alignment": 14}, '
                                '"evidence": {"skills": ["Must-have Python matched"], '
                                '"experience": ["Years align with role"], '
                                '"education": ["Relevant academic background"], '
                                '"role_alignment": ["Prior backend engineering role"]}, '
                                '"confidence": 0.82, "needs_human_review": false, '
                                '"reason": "Strong must-have alignment."}'
                            ),
                        }
                    ]
                },
            ]
        )
        service = _build_service(
            bedrock_client=fake_client,
            candidate_record=candidate,
            opening_record=opening,
        )

        result = await service.evaluate_application(application_id=candidate.id)
        assert result.score == 74
        assert len(fake_client.payloads) == 2
        scoring_prompt = fake_client.payloads[1]["messages"][0]["content"][0]["text"]
        assert "Must: Python" in scoring_prompt
        assert "Nice: Rust" in scoring_prompt
        assert "Req: Python, Rust" in scoring_prompt

    asyncio.run(run())


def test_candidate_evaluation_rejects_score_breakdown_mismatch_over_tolerance() -> None:
    """Output should fail validation when score and subtotal differ by more than 2."""

    async def run() -> None:
        candidate = _application_record()
        opening = _job_opening_record(candidate.job_opening_id)
        fake_client = _FakeBedrockClient(
            responses=[
                {"content": [{"type": "text", "text": "Summary text."}]},
                {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                '{"score": 90, "breakdown": {"skills": 30, "experience": 24, '
                                '"education": 8, "role_alignment": 20}, '
                                '"evidence": {"skills": ["Strong technical overlap"], '
                                '"experience": ["Aligned years experience"], '
                                '"education": ["Relevant degree"], '
                                '"role_alignment": ["Role history alignment"]}, '
                                '"confidence": 0.88, "needs_human_review": false, '
                                '"reason": "Inconsistent math."}'
                            ),
                        }
                    ]
                },
            ]
        )
        service = _build_service(
            bedrock_client=fake_client,
            candidate_record=candidate,
            opening_record=opening,
        )

        caught: ValidationError | None = None
        try:
            await service.evaluate_application(application_id=candidate.id)
        except ValidationError as exc:
            caught = exc
        assert caught is not None
        assert "score and breakdown total are inconsistent" in str(caught)

    asyncio.run(run())
