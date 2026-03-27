"""Tests for secondary-model-based offer-letter generation service."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from uuid import uuid4

from app.core.runtime_config import (
    BedrockRuntimeConfig,
    EvaluationRuntimeConfig,
)
from app.schemas.application import ApplicationRecord, ManagerSelectionDetails, ResumeFileMeta
from app.services.offer_letter_service import OfferLetterService


class _FakeBedrockClient:
    def __init__(self, responses: list[object]):
        self._responses = responses
        self.calls: list[str] = []

    async def invoke_json(self, *, model_id, payload):
        _ = payload
        self.calls.append(model_id)
        value = self._responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _candidate() -> ApplicationRecord:
    app_id = uuid4()
    return ApplicationRecord(
        id=app_id,
        job_opening_id=uuid4(),
        full_name="Offer Candidate",
        email="offer@example.com",
        linkedin_url="https://linkedin.com/in/offer",
        portfolio_url="https://offer.dev",
        github_url="https://github.com/offer",
        twitter_url=None,
        role_selection="Backend Engineer",
        parse_result={"summary": "Strong backend candidate"},
        parse_status="completed",
        applicant_status="in_interview",
        resume=ResumeFileMeta(
            original_filename="offer.pdf",
            stored_filename=f"{app_id}.pdf",
            storage_path=f"s3://hireme-cv-bucket/hireme/resumes/{app_id}.pdf",
            content_type="application/pdf",
            size_bytes=1024,
        ),
        created_at=datetime.now(tz=timezone.utc),
    )


def test_offer_letter_service_uses_secondary_model() -> None:
    """Offer-letter generation should use fallback/secondary Bedrock model id."""

    async def run() -> None:
        fake_client = _FakeBedrockClient(
            responses=[
                {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Subject: Offer of Employment - Backend Engineer\n\n"
                                "Dear Offer Candidate,\n\n"
                                "We are pleased to offer you the role."
                            ),
                        }
                    ]
                }
            ],
        )
        service = OfferLetterService(
            bedrock_client=fake_client,  # type: ignore[arg-type]
            bedrock_config=BedrockRuntimeConfig(
                enabled=True,
                fallback_model_id="fallback-model",
                request_timeout_seconds=3.0,
            ),
            evaluation_config=EvaluationRuntimeConfig(
                offer_letter_generation_enabled=True,
                offer_letter_prompt_template=(
                    "Use only this manager input:\n{manager_input_json}\n"
                    "Use only this candidate profile:\n{candidate_profile_json}\n"
                    "Return only the offer letter text."
                ),
            ),
        )

        letter = await service.generate_offer_letter(
            candidate=_candidate(),
            selection_details=ManagerSelectionDetails(
                confirmed_job_title="Backend Engineer",
                start_date=date(2026, 5, 1),
                base_salary="USD 140,000",
                compensation_structure="Base + annual bonus",
                equity_or_bonus="0.1% equity",
                reporting_manager="VP Engineering",
                custom_terms="Remote-first",
            ),
        )

        assert fake_client.calls == ["fallback-model"]
        assert "Subject: Offer of Employment - Backend Engineer" in letter
        assert "Dear Offer Candidate," in letter

    asyncio.run(run())
