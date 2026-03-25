"""Postgres ORM model for candidate-provided references."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, Uuid, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.model.base import Base


class ApplicantReference(Base):
    """Reference entry submitted for a specific candidate application."""

    __tablename__ = "applicant_references"
    __table_args__ = (
        UniqueConstraint("application_id", "referee_email", name="uq_reference_app_email"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    application_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("applicant_applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    candidate_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    referee_name: Mapped[str] = mapped_column(String(120), nullable=False)
    referee_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    referee_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    referee_linkedin_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    referee_company: Mapped[str | None] = mapped_column(String(160), nullable=True)
    referee_position: Mapped[str | None] = mapped_column(String(160), nullable=True)
    relationship: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
