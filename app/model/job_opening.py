"""Postgres-ready ORM model for employer job openings."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.model.base import Base


class JobOpening(Base):
    """Employer-managed job opening entity."""

    __tablename__ = "job_openings"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    role_title: Mapped[str] = mapped_column(String(120), nullable=False, index=True, unique=True)
    manager_email: Mapped[str] = mapped_column(String(320), nullable=False)
    team: Mapped[str] = mapped_column(String(120), nullable=False)
    location: Mapped[str] = mapped_column(String(120), nullable=False)
    experience_level: Mapped[str] = mapped_column(String(30), nullable=False)
    experience_range: Mapped[str] = mapped_column(String(30), nullable=False)
    application_open_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    application_close_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paused: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    responsibilities: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    requirements: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


Index(
    "uq_job_openings_role_title_ci",
    func.lower(JobOpening.role_title),
    unique=True,
)
