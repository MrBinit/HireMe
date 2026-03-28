"""db_hardening_indexes_constraints

Revision ID: 20260328_0001
Revises:
Create Date: 2026-03-28
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260328_0001"
down_revision = None
branch_labels = None
depends_on = None


def _add_check_not_valid(name: str, expression_sql: str) -> None:
    """Add a PostgreSQL CHECK constraint if missing, without back-validating old rows."""

    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = '{name}'
            ) THEN
                ALTER TABLE applicant_applications
                ADD CONSTRAINT {name} CHECK ({expression_sql}) NOT VALID;
            END IF;
        END
        $$;
        """
    )


def upgrade() -> None:
    """Apply DB hardening for status constraints and case-insensitive uniqueness."""

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Canonical application identity uniqueness (case-insensitive email per opening).
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_applications_opening_email_ci "
        "ON applicant_applications (job_opening_id, lower(email))"
    )

    # Canonical job opening role lookup uniqueness (case-insensitive).
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_job_openings_role_title_ci "
        "ON job_openings (lower(role_title))"
    )

    # Replace nullable unique constraint with case-insensitive null-safe uniqueness.
    op.execute("ALTER TABLE applicant_references DROP CONSTRAINT IF EXISTS uq_reference_app_email")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_reference_app_email_ci "
        "ON applicant_references (application_id, lower(coalesce(referee_email, '')))"
    )

    # Keep one canonical index set for Slack fields.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_applicant_applications_slack_invite_status "
        "ON applicant_applications (slack_invite_status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_applicant_applications_slack_user_id "
        "ON applicant_applications (slack_user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_applicant_applications_slack_onboarding_status "
        "ON applicant_applications (slack_onboarding_status)"
    )
    op.execute("DROP INDEX IF EXISTS idx_applications_slack_invite_status")
    op.execute("DROP INDEX IF EXISTS idx_applications_slack_user_id")
    op.execute("DROP INDEX IF EXISTS idx_applications_slack_onboarding_status")

    # Add DB-level status constraints for high-signal lifecycle columns.
    _add_check_not_valid(
        "ck_applications_parse_status",
        "parse_status IN ('pending', 'in_progress', 'completed', 'failed')",
    )
    _add_check_not_valid(
        "ck_applications_evaluation_status",
        "evaluation_status IS NULL OR "
        "evaluation_status IN ('queued', 'in_progress', 'completed', 'failed')",
    )
    _add_check_not_valid(
        "ck_applications_applicant_status",
        "applicant_status IN ("
        "'applied', 'screened', 'shortlisted', 'in_interview', 'offer', 'rejected', "
        "'received', 'in_progress', 'interview', 'accepted', 'sent_to_manager', "
        "'offer_letter_created', 'offer_letter_sent', 'offer_letter_sign'"
        ")",
    )
    _add_check_not_valid(
        "ck_applications_manager_decision",
        "manager_decision IS NULL OR manager_decision IN ('select', 'reject')",
    )
    _add_check_not_valid(
        "ck_applications_interview_transcript_status",
        "interview_transcript_status IS NULL OR "
        "interview_transcript_status IN ('pending', 'processing', 'completed', 'not_found', 'failed')",
    )


def downgrade() -> None:
    """Revert DB hardening migration."""

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        "ALTER TABLE applicant_applications "
        "DROP CONSTRAINT IF EXISTS ck_applications_interview_transcript_status"
    )
    op.execute(
        "ALTER TABLE applicant_applications "
        "DROP CONSTRAINT IF EXISTS ck_applications_manager_decision"
    )
    op.execute(
        "ALTER TABLE applicant_applications "
        "DROP CONSTRAINT IF EXISTS ck_applications_applicant_status"
    )
    op.execute(
        "ALTER TABLE applicant_applications "
        "DROP CONSTRAINT IF EXISTS ck_applications_evaluation_status"
    )
    op.execute(
        "ALTER TABLE applicant_applications "
        "DROP CONSTRAINT IF EXISTS ck_applications_parse_status"
    )

    op.execute("DROP INDEX IF EXISTS uq_reference_app_email_ci")
    op.execute(
        "ALTER TABLE applicant_references "
        "ADD CONSTRAINT uq_reference_app_email UNIQUE (application_id, referee_email)"
    )

    op.execute("DROP INDEX IF EXISTS uq_job_openings_role_title_ci")
    op.execute("DROP INDEX IF EXISTS uq_applications_opening_email_ci")

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_applications_slack_invite_status "
        "ON applicant_applications (slack_invite_status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_applications_slack_user_id "
        "ON applicant_applications (slack_user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_applications_slack_onboarding_status "
        "ON applicant_applications (slack_onboarding_status)"
    )
