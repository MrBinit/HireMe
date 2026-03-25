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
    portfolio_url: Mapped[str] = mapped_column(String(500), nullable=False)
    github_url: Mapped[str] = mapped_column(String(500), nullable=False)
    twitter_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    role_selection: Mapped[str] = mapped_column(String(120), nullable=False)
    resume_original_filename: Mapped[str] = mapped_column(String(260), nullable=False)
    resume_stored_filename: Mapped[str] = mapped_column(String(260), nullable=False, unique=True)
    resume_storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    resume_content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    resume_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    parse_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    parsed_skills: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    parsed_education: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    latest_position: Mapped[str | None] = mapped_column(String(160), nullable=True)
    total_years_experience: Mapped[float | None] = mapped_column(Float, nullable=True)
    parse_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending", server_default="pending"
    )
    applicant_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="received", server_default="received"
    )
    reference_status: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
