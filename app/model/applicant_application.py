"""Postgres-ready ORM model for applicant submissions."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Uuid,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.model.base import Base


class ApplicantApplication(Base):
    """Applicant-submitted application entity."""

    __tablename__ = "applicant_applications"
    __table_args__ = (
        UniqueConstraint("job_opening_id", "email", name="uq_application_opening_email"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    job_opening_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("job_openings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    portfolio_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    github_url: Mapped[str] = mapped_column(String(500), nullable=False)
    twitter_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    role_selection: Mapped[str] = mapped_column(String(120), nullable=False)
    resume_original_filename: Mapped[str] = mapped_column(String(260), nullable=False)
    resume_stored_filename: Mapped[str] = mapped_column(String(260), nullable=False, unique=True)
    resume_storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    resume_content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    resume_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    parse_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    parsed_total_years_experience: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        index=True,
    )
    parsed_search_text: Mapped[str | None] = mapped_column(String(8000), nullable=True)
    ai_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_screening_summary: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    candidate_brief: Mapped[str | None] = mapped_column(String(1500), nullable=True)
    online_research_summary: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    interview_schedule_status: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    interview_schedule_options: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    interview_schedule_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    interview_hold_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    interview_calendar_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    interview_schedule_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    interview_transcript_status: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    interview_transcript_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    interview_transcript_summary: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    interview_transcript_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    manager_decision: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    manager_decision_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    manager_decision_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    manager_selection_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    manager_selection_template_output: Mapped[str | None] = mapped_column(
        String(8000),
        nullable=True,
    )
    offer_letter_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    offer_letter_storage_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    offer_letter_signed_storage_path: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
    )
    offer_letter_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    offer_letter_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    offer_letter_signed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    offer_letter_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    docusign_envelope_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    slack_invite_status: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    slack_invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    slack_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    slack_joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    slack_welcome_message: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    slack_welcome_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    slack_onboarding_status: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    slack_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status_history: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    parse_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending", server_default="pending"
    )
    evaluation_status: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
        index=True,
    )
    applicant_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="applied", server_default="applied"
    )
    rejection_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    reference_status: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
