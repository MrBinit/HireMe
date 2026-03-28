"""Schema models for candidate application payloads and responses."""

from datetime import date, datetime
import json
from typing import Literal
from uuid import UUID

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    ValidationError,
    computed_field,
    model_validator,
)

ParseStatus = Literal["pending", "in_progress", "completed", "failed"]
EvaluationStatus = Literal["queued", "in_progress", "completed", "failed"]
ManagerDecision = Literal["select", "reject"]
ApplicantStatus = Literal[
    "applied",
    "screened",
    "shortlisted",
    "in_interview",
    "offer",
    "rejected",
    "received",
    "in_progress",
    "interview",
    "accepted",
    "sent_to_manager",
    "offer_letter_created",
    "offer_letter_sent",
    "offer_letter_sign",
]


class ApplicationCreatePayload(BaseModel):
    """Create payload for an application submission."""

    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    linkedin_url: AnyHttpUrl
    portfolio_url: AnyHttpUrl | None = None
    github_url: AnyHttpUrl | None = None
    twitter_url: AnyHttpUrl | None = None
    role_selection: str = Field(min_length=2, max_length=120)


class ResumeFileMeta(BaseModel):
    """Metadata stored for an uploaded resume file."""

    original_filename: str
    stored_filename: str
    storage_path: str
    content_type: str
    size_bytes: int


class StatusHistoryEntry(BaseModel):
    """One status-transition event for applicant lifecycle auditing."""

    status: str
    note: str | None = None
    changed_at: datetime
    source: str = "system"


class ManagerSelectionDetails(BaseModel):
    """Offer details required when manager selects a candidate."""

    confirmed_job_title: str = Field(min_length=2, max_length=160)
    start_date: date
    base_salary: str = Field(min_length=1, max_length=200)
    compensation_structure: str = Field(min_length=2, max_length=400)
    equity_or_bonus: str | None = Field(default=None, max_length=400)
    reporting_manager: str = Field(min_length=2, max_length=160)
    custom_terms: str | None = Field(default=None, max_length=2000)


class ResearchProvenanceEntry(BaseModel):
    """One claim provenance row with evidence reference paths."""

    claim: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="ignore")


class ResearchIssueFlag(BaseModel):
    """One discrepancy/issue flag row for manager review."""

    type: str | None = None
    severity: str | None = None
    source: str | None = None
    details: str | None = None
    model_config = ConfigDict(extra="ignore")


class ResearchLlmIssue(BaseModel):
    """One issue row from LLM analysis payload."""

    type: str | None = None
    severity: str | None = None
    evidence: str | None = None
    model_config = ConfigDict(extra="ignore")


class ResearchLlmAnalysis(BaseModel):
    """Normalized LLM analysis block persisted in research summary."""

    source: str | None = None
    model_id: str | None = None
    summary: str | None = None
    confidence: str | None = None
    issues: list[ResearchLlmIssue] = Field(default_factory=list)
    provenance: list[ResearchProvenanceEntry] = Field(default_factory=list)
    model_config = ConfigDict(extra="ignore")


class ResearchDeterministicChecks(BaseModel):
    """Non-LLM quality checks used for confidence routing."""

    has_minimum_public_evidence: bool | None = None
    manual_review_required: bool | None = None
    confidence_baseline: str | None = None
    high_severity_issue_count: int | None = None
    notes: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="ignore")


class ResearchHit(BaseModel):
    """One source hit row used for evidence link rendering."""

    title: str | None = None
    link: str | None = None
    snippet: str | None = None
    model_config = ConfigDict(extra="ignore")


class ResearchGithubRepo(BaseModel):
    """One compact GitHub repository row from research summary."""

    name: str | None = None
    html_url: str | None = None
    model_config = ConfigDict(extra="ignore")


class ResearchLinkedinExtractor(BaseModel):
    """LinkedIn extractor subset for UI and auditing."""

    matched_profile_url: str | None = None
    top_hits: list[ResearchHit] = Field(default_factory=list)
    model_config = ConfigDict(extra="ignore")


class ResearchGithubExtractor(BaseModel):
    """GitHub extractor subset for UI and auditing."""

    profile_url: str | None = None
    top_repositories: list[ResearchGithubRepo] = Field(default_factory=list)
    model_config = ConfigDict(extra="ignore")


class ResearchPortfolioExtractor(BaseModel):
    """Portfolio extractor subset for UI and auditing."""

    matched_portfolio_url: str | None = None
    top_hits: list[ResearchHit] = Field(default_factory=list)
    model_config = ConfigDict(extra="ignore")


class ResearchExtractors(BaseModel):
    """Extractor envelope persisted in research summary payload."""

    linkedin: ResearchLinkedinExtractor | None = None
    github: ResearchGithubExtractor | None = None
    portfolio: ResearchPortfolioExtractor | None = None
    model_config = ConfigDict(extra="ignore")


class ResearchSummary(BaseModel):
    """Typed representation of online_research_summary JSON."""

    brief: str | None = None
    discrepancies: list[str] = Field(default_factory=list)
    issue_flags: list[ResearchIssueFlag] = Field(default_factory=list)
    deterministic_checks: ResearchDeterministicChecks | None = None
    llm_analysis: ResearchLlmAnalysis | None = None
    extractors: ResearchExtractors | None = None
    model_config = ConfigDict(extra="ignore")


def parse_research_summary_json(value: str | None) -> ResearchSummary | None:
    """Parse persisted research JSON into typed model when possible."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return ResearchSummary.model_validate(payload)
    except ValidationError:
        return None


class ApplicationRecord(BaseModel):
    """Stored representation of an application."""

    id: UUID
    job_opening_id: UUID
    full_name: str
    email: EmailStr
    linkedin_url: AnyHttpUrl | None = None
    portfolio_url: AnyHttpUrl | None = None
    github_url: AnyHttpUrl
    twitter_url: AnyHttpUrl | None = None
    role_selection: str
    parse_result: dict | None = None
    parsed_total_years_experience: float | None = None
    parsed_search_text: str | None = None
    parse_status: ParseStatus = "pending"
    evaluation_status: EvaluationStatus | None = None
    applicant_status: ApplicantStatus = "applied"
    rejection_reason: str | None = None
    ai_score: float | None = None
    ai_screening_summary: str | None = None
    candidate_brief: str | None = None
    online_research_summary: str | None = None
    interview_schedule_status: str | None = None
    interview_schedule_options: dict | None = None
    interview_schedule_sent_at: datetime | None = None
    interview_hold_expires_at: datetime | None = None
    interview_calendar_email: str | None = None
    interview_schedule_error: str | None = None
    interview_transcript_status: str | None = None
    interview_transcript_url: str | None = None
    interview_transcript_summary: str | None = None
    interview_transcript_synced_at: datetime | None = None
    manager_decision: ManagerDecision | None = None
    manager_decision_at: datetime | None = None
    manager_decision_note: str | None = None
    manager_selection_details: ManagerSelectionDetails | None = None
    manager_selection_template_output: str | None = None
    offer_letter_status: str | None = None
    offer_letter_storage_path: str | None = None
    offer_letter_signed_storage_path: str | None = None
    offer_letter_generated_at: datetime | None = None
    offer_letter_sent_at: datetime | None = None
    offer_letter_signed_at: datetime | None = None
    offer_letter_error: str | None = None
    docusign_envelope_id: str | None = None
    slack_invite_status: str | None = None
    slack_invited_at: datetime | None = None
    slack_user_id: str | None = None
    slack_joined_at: datetime | None = None
    slack_welcome_message: str | None = None
    slack_welcome_sent_at: datetime | None = None
    slack_onboarding_status: str | None = None
    slack_error: str | None = None
    status_history: list[StatusHistoryEntry] = Field(default_factory=list)
    reference_status: bool = False
    resume: ResumeFileMeta
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @computed_field
    @property
    def research_summary(self) -> ResearchSummary | None:
        """Expose typed research summary derived from persisted JSON string."""

        return parse_research_summary_json(self.online_research_summary)


class ApplicationListResponse(BaseModel):
    """Paginated response model for applicant records."""

    items: list[ApplicationRecord]
    total: int
    offset: int
    limit: int


class ResumeDownloadResponse(BaseModel):
    """Response payload containing temporary resume download URL."""

    download_url: str
    expires_in_seconds: int
    filename: str


class PublicApplicationStatusResponse(BaseModel):
    """Public status payload for applicant self-tracking after submission."""

    application_id: UUID
    applicant_status: ApplicantStatus
    parse_status: ParseStatus
    evaluation_status: EvaluationStatus | None = None
    interview_schedule_status: str | None = None
    ai_score: float | None = None
    role_selection: str
    submitted_at: datetime
    research_ready: bool = False


class InterviewSlotConfirmPayload(BaseModel):
    """Candidate payload to confirm one interview slot option."""

    email: EmailStr
    option_number: int = Field(ge=1, le=20)


class InterviewSlotConfirmResponse(BaseModel):
    """Response payload after candidate confirms interview slot."""

    application_id: UUID
    interview_schedule_status: str
    applicant_status: ApplicantStatus
    selected_option_number: int
    confirmed_event_id: str
    confirmed_event_link: str | None = None
    confirmed_meeting_link: str | None = None
    confirmed_at: datetime


class InterviewTokenConfirmPayload(BaseModel):
    """Payload for one-click interview confirmation via signed token."""

    token: str = Field(min_length=16, max_length=5000)


class InterviewActionTokenPayload(BaseModel):
    """Payload for signed interview action token (reschedule/approve/reject)."""

    token: str = Field(min_length=16, max_length=5000)


class InterviewActionResponse(BaseModel):
    """Response payload after one interview action token is processed."""

    application_id: UUID
    interview_schedule_status: str
    applicant_status: ApplicantStatus
    message: str
    confirmed_event_link: str | None = None
    confirmed_meeting_link: str | None = None


class ApplicantStatusUpdatePayload(BaseModel):
    """Request payload for updating an applicant lifecycle status."""

    applicant_status: ApplicantStatus
    note: str | None = Field(default=None, max_length=1000)


class AdminCandidateReviewPayload(BaseModel):
    """Admin payload for status override notes and AI screening metadata."""

    applicant_status: ApplicantStatus | None = None
    note: str | None = Field(default=None, max_length=1000)
    ai_score: float | None = Field(default=None, ge=0.0, le=100.0)
    ai_screening_summary: str | None = Field(default=None, max_length=4000)
    candidate_brief: str | None = Field(default=None, max_length=1500)
    online_research_summary: str | None = Field(default=None, max_length=4000)
    rejection_reason: str | None = Field(default=None, max_length=1000)


class ManagerDecisionPayload(BaseModel):
    """Admin payload for manager select/reject decision after interview completion."""

    decision: ManagerDecision
    note: str | None = Field(default=None, max_length=1000)
    selection_details: ManagerSelectionDetails | None = None

    @model_validator(mode="after")
    def validate_selection_details(self) -> "ManagerDecisionPayload":
        """Require details only for select decisions."""

        if self.decision == "select" and self.selection_details is None:
            raise ValueError("selection_details are required when decision=select")
        if self.decision == "reject" and self.selection_details is not None:
            raise ValueError("selection_details are allowed only when decision=select")
        return self


class FirefliesDemoPayload(BaseModel):
    """Admin payload to simulate transcript completion state for demo flows."""

    mock_completed: bool = Field(
        default=True,
        description=(
            "When true, force transcript completed + interview_done with fake transcript data. "
            "When false, force transcript processing state with no transcript content."
        ),
    )
