"""Schemas for candidate LLM evaluation results."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class EvaluationBreakdown(BaseModel):
    """Category-level scoring output from the evaluator model."""

    skills: float = Field(ge=0, le=40)
    experience: float = Field(ge=0, le=30)
    education: float = Field(ge=0, le=10)
    role_alignment: float = Field(ge=0, le=20)

    @property
    def total(self) -> float:
        """Return sum of all category scores."""

        return float(self.skills + self.experience + self.education + self.role_alignment)


class EvaluationEvidence(BaseModel):
    """Evidence snippets backing each category score."""

    skills: list[str] = Field(min_length=1, max_length=5)
    experience: list[str] = Field(min_length=1, max_length=5)
    education: list[str] = Field(min_length=1, max_length=5)
    role_alignment: list[str] = Field(min_length=1, max_length=5)

    @field_validator("skills", "experience", "education", "role_alignment")
    @classmethod
    def validate_evidence_items(cls, value: list[str]) -> list[str]:
        """Ensure evidence lists only contain non-empty trimmed items."""

        cleaned = [" ".join(item.split()).strip() for item in value if isinstance(item, str)]
        cleaned = [item for item in cleaned if item]
        if not cleaned:
            raise ValueError("evidence must include at least one non-empty item")
        return cleaned


class CandidateEvaluationResult(BaseModel):
    """Validated evaluator output with strict numeric bounds."""

    score: float = Field(ge=0, le=100)
    breakdown: EvaluationBreakdown
    evidence: EvaluationEvidence
    confidence: float = Field(ge=0, le=1)
    needs_human_review: bool = False
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_total(self) -> "CandidateEvaluationResult":
        """Ensure score is aligned with category subtotal."""

        if abs(float(self.score) - self.breakdown.total) > 2:
            raise ValueError("score and breakdown total are inconsistent")
        return self


class CandidateEvaluationQueueResponse(BaseModel):
    """Response payload for queued asynchronous candidate LLM scoring."""

    application_id: UUID
    queued: bool
    queued_at: datetime
    queue_name: str


class CandidateResearchQueueResponse(BaseModel):
    """Response payload for queued asynchronous candidate research enrichment."""

    application_id: UUID
    queued: bool
    queued_at: datetime
    queue_name: str


class CandidateSchedulingQueueResponse(BaseModel):
    """Response payload for queued asynchronous interview scheduling orchestration."""

    application_id: UUID
    queued: bool
    queued_at: datetime
    queue_name: str
