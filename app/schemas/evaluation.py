"""Schemas for candidate LLM evaluation results."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


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


class CandidateEvaluationResult(BaseModel):
    """Validated evaluator output with strict numeric bounds."""

    score: float = Field(ge=0, le=100)
    breakdown: EvaluationBreakdown
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_total(self) -> "CandidateEvaluationResult":
        """Ensure score is aligned with category subtotal."""

        if abs(float(self.score) - self.breakdown.total) > 10:
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
