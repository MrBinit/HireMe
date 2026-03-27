"""YAML-backed runtime configuration models and loader."""

import copy
from functools import lru_cache
from pathlib import Path
from typing import Any
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.core.settings import get_settings


class ApiRuntimeConfig(BaseModel):
    """Public API metadata shown in OpenAPI docs."""

    title: str = "HireMe Hiring API"
    version: str = "0.1.0"
    description: str = "Async backend for job openings and candidate applications."


class JobOpeningRuntimeConfig(BaseModel):
    """Config options for job opening validation and pagination."""

    allowed_experience_levels: list[str] = Field(
        default_factory=lambda: ["intern", "junior", "mid", "senior", "staff", "principal"]
    )
    experience_range_pattern: str = r"^\d+\s*-\s*\d+\s*years?$"
    min_bullet_items: int = 1
    max_bullet_items: int = 25
    default_list_limit: int = 20
    max_list_limit: int = 100


class ApplicationRuntimeConfig(BaseModel):
    """Config options for candidate application validation."""

    allowed_resume_extensions: list[str] = Field(default_factory=lambda: [".pdf", ".doc", ".docx"])
    allowed_resume_content_types: list[str] = Field(
        default_factory=lambda: [
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/octet-stream",
        ]
    )
    max_pdf_size_mb: int = 10
    max_doc_size_mb: int = 10
    max_docx_size_mb: int = 10
    resume_chunk_size_bytes: int = 1_048_576
    resume_download_url_expire_seconds: int = 900
    offer_letter_s3_prefix: str = "offer-letters"
    offer_letter_download_url_expire_seconds: int = 900
    default_list_limit: int = 20
    max_list_limit: int = 100
    applications_not_open_message: str = "applications have not opened yet"
    application_paused_message: str = "Sorry, applications are currently paused for this role."
    application_closed_message: str = (
        "Sorry, the application has already been closed for this role."
    )
    invalid_resume_format_message: str = "Invalid resume format. Please upload a PDF or DOCX file."
    duplicate_application_message: str = (
        "duplicate application: this email has already applied to this job opening"
    )
    initial_screening_fail_reason: str = "Candidate failed in initial screening."
    ai_score_fail_reason: str = "Candidate did not meet the AI score threshold."
    ai_score_threshold: float = 70.0
    prefilter_min_keyword_length: int = 3
    prefilter_max_keywords: int = 24
    prefilter_min_keyword_matches: int = 5
    prefilter_min_skill_matches: int = 5
    prefilter_max_search_text_chars: int = 8000
    prefilter_stop_words: list[str] = Field(
        default_factory=lambda: [
            "and",
            "the",
            "for",
            "with",
            "from",
            "that",
            "this",
            "will",
            "have",
            "has",
            "are",
            "you",
            "your",
            "our",
            "their",
            "role",
            "team",
            "years",
            "year",
        ]
    )
    manager_selection_template: str = ""
    slack_invite_fallback_join_url: str = ""


class StorageRuntimeConfig(BaseModel):
    """Storage backend configuration (DB metadata + S3 resumes)."""

    backend: str = "postgres"
    resume_backend: str = "s3"
    auto_create_tables: bool = True

    @field_validator("backend")
    @classmethod
    def validate_backend(cls, value: str) -> str:
        """Allow only postgres metadata backend."""

        if value != "postgres":
            raise ValueError("storage.backend must be 'postgres'")
        return value

    @field_validator("resume_backend")
    @classmethod
    def validate_resume_backend(cls, value: str) -> str:
        """Allow only s3 resume backend."""

        if value != "s3":
            raise ValueError("storage.resume_backend must be 's3'")
        return value


class S3StorageRuntimeConfig(BaseModel):
    """S3-specific key prefixes and transfer tuning."""

    region: str = "us-east-1"
    bucket: str = "hireme-cv-bucket"
    force_path_style: bool = False
    job_openings_prefix: str = "job-openings"
    job_opening_role_index_prefix: str = "job-opening-role-index"
    applications_prefix: str = "applications"
    application_dedupe_prefix: str = "application-dedupe"
    resumes_prefix: str = "resumes"
    list_page_size: int = 1000
    upload_max_concurrency: int = 10
    upload_multipart_threshold_mb: int = 8
    upload_multipart_chunksize_mb: int = 8


class PostgresRuntimeConfig(BaseModel):
    """Postgres async engine pool and startup configuration."""

    pool_size: int = 5
    max_overflow: int = 10
    pool_timeout_seconds: float = 30.0
    pool_recycle_seconds: int = 1800
    pool_pre_ping: bool = True
    connect_timeout_seconds: float = 10.0
    command_timeout_seconds: float = 60.0
    echo_sql: bool = False
    ssl_mode: Literal["disable", "require"] = "require"
    ssl_root_cert_path: str | None = None


class ErrorRuntimeConfig(BaseModel):
    """Global API error response configuration."""

    request_validation_message: str = "Request validation failed."
    http_error_message: str = "HTTP error occurred."
    internal_error_message: str = "An unexpected error occurred."
    status_code_map: dict[int, str] = Field(
        default_factory=lambda: {
            400: "bad_request",
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            409: "conflict",
            422: "unprocessable_entity",
            429: "rate_limited",
            504: "request_timeout",
        }
    )


class SecurityRuntimeConfig(BaseModel):
    """JWT security configuration for admin-protected routes."""

    enabled: bool = True
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    required_role: str = "admin"
    issuer: str = "hireme-backend"
    audience: str = "hireme-admin"
    access_token_exp_minutes: int = 60
    leeway_seconds: int = 30


class ParseRuntimeConfig(BaseModel):
    """Queue/backfill runtime config for background resume parsing."""

    use_queue: bool = True
    provider: Literal["sqs", "redis", "local"] = "sqs"
    region: str = "us-east-1"
    queue_name: str = "hireme-resume-parse"
    queue_url: str | None = None
    worker_concurrency: int = 20
    max_in_flight_per_worker: int = 20
    receive_batch_size: int = 10
    receive_wait_seconds: int = 20
    enqueue_timeout_seconds: float = 2.0
    fail_submission_on_enqueue_error: bool = False
    max_extracted_chars: int = 50_000
    llm_fallback_min_chars: int = 400
    max_section_lines: int = 40
    parse_timeout_seconds: float = 60.0
    visibility_timeout_seconds: int = 300
    max_receive_count: int = 5
    link_rules: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "linkedin_domains": ["linkedin.com"],
            "github_domains": ["github.com"],
            "project_domains": [
                "github.com",
                "gitlab.com",
                "bitbucket.org",
                "dev.to",
                "behance.net",
                "dribbble.com",
                "kaggle.com",
            ],
            "excluded_personal_domains": [
                "linkedin.com",
                "github.com",
                "gitlab.com",
                "bitbucket.org",
                "dev.to",
                "medium.com",
                "behance.net",
                "dribbble.com",
                "kaggle.com",
                "x.com",
                "twitter.com",
            ],
        }
    )
    section_aliases: dict[str, list[str]] = Field(default_factory=dict)


class BedrockRuntimeConfig(BaseModel):
    """Runtime config for AWS Bedrock model invocation."""

    enabled: bool = True
    region: str = "us-east-1"
    primary_model_id: str = "us.anthropic.claude-sonnet-4-20250514-v1:0"
    fallback_model_id: str = "us.anthropic.claude-3-5-haiku-20241022-v1:0"
    max_tokens: int = 1200
    temperature: float = 0.1
    top_p: float = 0.9
    request_timeout_seconds: float = 20.0
    max_retries: int = 2
    max_concurrency: int = 20


class EvaluationRuntimeConfig(BaseModel):
    """Prompt and output constraints for candidate LLM evaluation."""

    enabled: bool = True
    use_queue: bool = True
    provider: Literal["sqs", "redis", "local"] = "sqs"
    region: str = "us-east-1"
    queue_name: str = "hireme-llm-evaluation"
    queue_url: str | None = None
    enqueue_timeout_seconds: float = 2.0
    worker_concurrency: int = 10
    max_in_flight_per_worker: int = 10
    receive_batch_size: int = 10
    receive_wait_seconds: int = 20
    visibility_timeout_seconds: int = 300
    max_receive_count: int = 5
    target_statuses: list[str] = Field(
        default_factory=lambda: [
            "screened",
            "shortlisted",
            "in_interview",
            "offer",
            "sent_to_manager",
        ]
    )
    prompt_template: str = ""
    summary_prompt_template: str = ""
    max_reason_chars: int = 500
    max_work_summary_chars: int = 1500
    offer_letter_generation_enabled: bool = True
    offer_letter_profile_json_max_chars: int = 4000
    offer_letter_max_chars: int = 8000
    offer_letter_prompt_template: str = ""
    slack_welcome_generation_enabled: bool = True
    slack_welcome_max_chars: int = 2000
    slack_welcome_prompt_template: str = ""


class ResearchRuntimeConfig(BaseModel):
    """Runtime config for external web research enrichment."""

    class LinkedInExtractRuntimeConfig(BaseModel):
        """Config for LinkedIn-only extraction and resume cross-reference."""

        query_templates: list[str] = Field(
            default_factory=lambda: [
                "site:linkedin.com/in {linkedin_handle}",
                '"{linkedin_url}"',
                'site:linkedin.com/in "{full_name}" "{role_selection}"',
            ]
        )
        results_per_query: int = 8
        max_linkedin_hits: int = 12
        max_evidence_lines: int = 10
        min_skill_token_length: int = 3
        min_position_token_length: int = 3
        max_output_skills: int = 30
        max_output_employers: int = 20
        max_output_positions: int = 20

    class LinkedInTextExtractRuntimeConfig(BaseModel):
        """Config for parsing pasted LinkedIn profile text into structured sections."""

        section_headings: dict[str, list[str]] = Field(
            default_factory=lambda: {
                "experience": ["experience"],
                "education": ["education"],
                "licenses_and_certifications": [
                    "licenses & certifications",
                    "licenses and certifications",
                ],
                "projects": ["projects"],
                "skills": ["skills"],
            }
        )
        stop_headings: list[str] = Field(
            default_factory=lambda: [
                "interests",
                "top voices",
                "companies",
                "groups",
                "newsletters",
                "schools",
                "profile language",
                "public profile & url",
                "who your viewers also viewed",
                "people you may know",
                "you might like",
            ]
        )
        ignore_line_patterns: list[str] = Field(
            default_factory=lambda: [
                r"^\s*show all\s*$",
                r"^\s*show credential\s*$",
                r"^\s*message\s*$",
                r"^\s*connect\s*$",
                r"^\s*follow\s*$",
                r"^\s*view\s*$",
                r"^\s*private to you\s*$",
                r".*\blogo\s*$",
                r"^\s*thumbnail for\s+.*$",
                r".*someone at .*",
                r".*someone in .*",
                r".*followers?$",
            ]
        )
        bullet_prefixes: list[str] = Field(default_factory=lambda: ["•", "-", "*"])
        month_names: list[str] = Field(
            default_factory=lambda: [
                "Jan",
                "Feb",
                "Mar",
                "Apr",
                "May",
                "Jun",
                "Jul",
                "Aug",
                "Sep",
                "Oct",
                "Nov",
                "Dec",
            ]
        )
        max_items_per_section: int = 100

    class GithubRuntimeConfig(BaseModel):
        """Config for GitHub profile enrichment via GitHub REST API."""

        api_base_url: str = "https://api.github.com"
        repos_per_user: int = 10
        max_repo_items: int = 30
        max_topics_per_repo: int = 5
        max_repos_in_summary: int = 5
        max_primary_languages: int = 5
        max_repo_description_chars: int = 220
        activity_active_within_days: int = 180
        request_timeout_seconds: float = 12.0
        user_agent: str = "hireme-candidate-research/1.0"

    class EnrichmentRuntimeConfig(BaseModel):
        """Config for shortlisted-candidate enrichment output shaping."""

        use_queue: bool = True
        provider: Literal["sqs", "redis", "local"] = "sqs"
        region: str = "us-east-1"
        queue_name: str = "hireme-candidate-research-enrichment"
        queue_url: str | None = None
        enqueue_timeout_seconds: float = 2.0
        worker_concurrency: int = 8
        max_in_flight_per_worker: int = 8
        receive_batch_size: int = 10
        receive_wait_seconds: int = 20
        visibility_timeout_seconds: int = 300
        max_receive_count: int = 5
        target_statuses: list[str] = Field(default_factory=lambda: ["shortlisted"])
        max_candidates_per_run: int = 200
        max_profile_hits: int = 6
        max_twitter_hits: int = 6
        max_portfolio_hits: int = 6
        max_discrepancies: int = 8
        max_brief_sentences: int = 5
        min_brief_sentences: int = 3
        llm_analysis_enabled: bool = True
        llm_max_tokens: int = 900
        llm_prompt_template: str = (
            "You are a hiring research analyst.\n"
            "Use ONLY the provided resume and extracted profile JSON.\n\n"
            "TASKS:\n"
            "1) Cross-reference resume vs online profiles.\n"
            "2) Detect factual discrepancies or missing corroboration.\n"
            "3) Write a 3-5 sentence hiring brief.\n\n"
            "RULES:\n"
            "- Do not hallucinate any facts.\n"
            "- If evidence is missing, say 'insufficient public evidence'.\n"
            "- Keep findings concise and actionable.\n"
            "- Return strict JSON only.\n\n"
            "CANDIDATE: {candidate_name}\n"
            "ROLE: {role_selection}\n"
            "RESUME_JSON: {resume_json}\n"
            "EXTRACTED_JSON: {extracted_json}\n\n"
            "OUTPUT JSON SCHEMA:\n"
            "{\n"
            '  "cross_reference": {\n'
            '    "employment_alignment": ["..."],\n'
            '    "skills_alignment": ["..."],\n'
            '    "project_alignment": ["..."]\n'
            "  },\n"
            '  "discrepancies": ["..."],\n'
            '  "summary": "3-5 sentence candidate brief"\n'
            "}"
        )
        max_research_json_chars: int = 3800

    enabled: bool = False
    provider: Literal["serpapi"] = "serpapi"
    google_search_url: str = "https://serpapi.com/search.json"
    engine: str = "google"
    always_web_retrieval_enabled: bool = True
    query_planner_use_llm: bool = False
    retrieval_loop_use_llm: bool = False
    request_timeout_seconds: float = 15.0
    max_concurrency: int = 8
    results_per_query: int = 5
    max_summary_chars: int = 4000
    only_when_missing_urls: bool = True
    target_statuses: list[str] = Field(
        default_factory=lambda: [
            "screened",
            "shortlisted",
            "in_interview",
            "offer",
            "sent_to_manager",
        ]
    )
    linkedin_query_template: str = 'site:linkedin.com/in "{full_name}" "{role_selection}"'
    twitter_query_template: str = (
        '(site:x.com OR site:twitter.com) "{full_name}" "{role_selection}"'
    )
    profile_query_template: str = '"{full_name}" "{role_selection}"'
    links_limit_per_query: int = 3
    linkedin_extract: LinkedInExtractRuntimeConfig = Field(
        default_factory=LinkedInExtractRuntimeConfig
    )
    linkedin_text_extract: LinkedInTextExtractRuntimeConfig = Field(
        default_factory=LinkedInTextExtractRuntimeConfig
    )
    github: GithubRuntimeConfig = Field(default_factory=GithubRuntimeConfig)
    enrichment: EnrichmentRuntimeConfig = Field(default_factory=EnrichmentRuntimeConfig)


class SchedulingRuntimeConfig(BaseModel):
    """Runtime config for interview slot orchestration and queue processing."""

    class FirefliesRuntimeConfig(BaseModel):
        """Runtime config for post-interview transcript/summary sync from Fireflies."""

        enabled: bool = False
        mock_mode: bool = False
        api_url: str = "https://api.fireflies.ai/graphql"
        owner_email: str | None = None
        request_timeout_seconds: float = 15.0
        poll_interval_seconds: int = 180
        batch_size: int = 50
        join_before_minutes: int = 10
        join_retry_cooldown_minutes: int = 5
        transcript_poll_delay_minutes: int = 20
        transcript_poll_interval_minutes: int = 15
        max_poll_attempts: int = 24
        transcript_lookup_hours: int = 72
        transcripts_page_limit: int = 20
        max_transcript_pages: int = 3
        update_schedule_status_on_complete: bool = True
        completed_schedule_status: str = "interview_done"

    enabled: bool = True
    use_queue: bool = True
    provider: Literal["sqs", "redis", "local"] = "sqs"
    region: str = "us-east-1"
    queue_name: str = "hireme-interview-scheduling"
    queue_url: str | None = None
    enqueue_timeout_seconds: float = 2.0
    worker_concurrency: int = 4
    max_in_flight_per_worker: int = 4
    receive_batch_size: int = 10
    receive_wait_seconds: int = 20
    visibility_timeout_seconds: int = 300
    max_receive_count: int = 5
    auto_enqueue_after_shortlist: bool = True
    target_statuses: list[str] = Field(default_factory=lambda: ["shortlisted"])
    min_slots: int = 3
    max_slots: int = 5
    slot_duration_minutes: int = 45
    slot_step_minutes: int = 30
    business_days_ahead: int = 5
    business_hours_start_hour: int = 9
    business_hours_end_hour: int = 17
    min_notice_hours: int = 4
    hold_expiry_hours: int = 24
    reminder_enabled: bool = True
    reminder_after_hours: int = 24
    reminder_target_statuses: list[str] = Field(
        default_factory=lambda: ["interview_options_sent", "interview_email_sent", "options_sent"]
    )
    reminder_batch_size: int = 100
    timezone: str = "UTC"
    use_domain_wide_delegation: bool = True
    candidate_confirmation_page_url: str = "http://localhost:3000/interview/confirm"
    interview_action_page_url: str = "http://localhost:3000/interview/action"
    manager_reschedule_action_page_url: str = "http://localhost:3000/interview/action"
    confirmable_statuses: list[str] = Field(
        default_factory=lambda: ["interview_options_sent", "interview_email_sent", "options_sent"]
    )
    manager_reschedule_confirmable_statuses: list[str] = Field(
        default_factory=lambda: ["interview_reschedule_options_sent"]
    )
    booked_status: str = "interview_booked"
    reschedule_requested_status: str = "interview_reschedule_requested"
    reschedule_options_sent_status: str = "interview_reschedule_options_sent"
    reschedule_confirming_status: str = "interview_reschedule_confirming"
    max_reschedule_rounds: int = 6
    action_link_expiry_hours: int = 72
    expired_status: str = "interview_expired"
    release_other_holds_on_confirm: bool = True
    require_confirmation_before_expiry: bool = True
    move_candidate_to_in_interview_on_booking: bool = True
    candidate_status_on_booking: str = "in_interview"
    calendar_send_updates_mode: Literal["all", "none", "externalOnly"] = "all"
    confirmed_event_title_template: str = (
        "HireMe Technical Interview - {candidate_name} ({role_title})"
    )
    confirmed_event_description_template: str = (
        "Confirmed interview scheduled by HireMe.\n"
        "Application ID: {application_id}\n"
        "Candidate: {candidate_name}\n"
        "Role: {role_title}\n"
        "Candidate email: {candidate_email}\n"
        "Selected option: {selected_option_number}\n"
    )
    auto_release_expired_holds: bool = True
    expiry_poll_interval_seconds: int = 60
    expiry_batch_size: int = 100
    expiry_target_statuses: list[str] = Field(
        default_factory=lambda: [
            "interview_options_sent",
            "interview_email_sent",
            "options_sent",
            "interview_reschedule_options_sent",
        ]
    )
    hold_event_title_template: str = "HireMe Interview Hold - {candidate_name} ({role_title})"
    hold_event_description_template: str = (
        "Tentative interview hold created by HireMe.\n"
        "Application ID: {application_id}\n"
        "Candidate: {candidate_name}\n"
        "Role: {role_title}\n"
        "Hold expires at: {hold_expires_at}\n"
        "Do not schedule conflicting meetings in this slot."
    )
    fireflies: FirefliesRuntimeConfig = Field(default_factory=FirefliesRuntimeConfig)


class NotificationRuntimeConfig(BaseModel):
    """Runtime config for application confirmation emails."""

    class SmtpConfig(BaseModel):
        """SMTP transport options (non-secret)."""

        host: str = "smtp.gmail.com"
        port: int = 587
        use_starttls: bool = True
        use_ssl: bool = False

    enabled: bool = True
    provider: Literal["smtp", "noop"] = "smtp"
    sender_name: str = "HireMe Team"
    sender_email: str = "no-reply@hireme.ai"
    smtp: SmtpConfig = Field(default_factory=SmtpConfig)
    confirmation_subject_template: str = "Application submitted - HireMe"
    confirmation_body_template: str = (
        "Hi {candidate_name},\n\n"
        "Thank you for applying to HireMe. Your application has been submitted.\n\n"
        "Regards,\nHireMe Team"
    )
    rejection_subject_template: str = "Application update - HireMe"
    rejection_body_template: str = (
        "Hi {candidate_name},\n\n"
        "Thank you for applying to HireMe for the {role_title} role. "
        "After review, we will not be moving ahead with your resume this time.\n\n"
        "Regards,\nHireMe Team"
    )
    interview_options_subject_template: str = "Interview slot options - HireMe ({role_title})"
    interview_options_body_template: str = (
        "Hi {candidate_name},\n\n"
        "You have been shortlisted for the {role_title} role.\n"
        "Please click one preferred option from the list below:\n\n"
        "{slot_options}\n\n"
        "If these options do not work for you, use one of these links:\n"
        "{action_links}\n\n"
        "These slots are held temporarily on the interviewer's calendar and will expire at "
        "{hold_expires_at}.\n\n"
        "Regards,\nHireMe Team"
    )
    interview_reminder_subject_template: str = (
        "Reminder: confirm interview slot in 24 hours - HireMe ({role_title})"
    )
    interview_reminder_body_template: str = (
        "Hi {candidate_name},\n\n"
        "This is a reminder to confirm your technical interview slot for the {role_title} role.\n"
        "Please click one preferred option below:\n\n"
        "{slot_options}\n\n"
        "If these options do not work for you, use one of these links:\n"
        "{action_links}\n\n"
        "If you are interested, please confirm within the next 24 hours. "
        "Unconfirmed slots will expire at {hold_expires_at}.\n\n"
        "Regards,\nHireMe Team"
    )
    interview_confirmed_subject_template: str = "Interview confirmed - HireMe ({role_title})"
    interview_confirmed_body_template: str = (
        "Hi {candidate_name},\n\n"
        "Your technical interview has been confirmed for:\n"
        "{confirmed_slot}\n\n"
        "{action_links}\n\n"
        "A calendar invitation has been sent.\n\n"
        "Regards,\nHireMe Team"
    )
    interview_thank_you_subject_template: str = (
        "Thank you for interviewing with HireMe ({role_title})"
    )
    interview_thank_you_body_template: str = (
        "Hi {candidate_name},\n\n"
        "Thank you for participating in the interview.\n"
        "We will let you know the outcome soon. Stay tuned.\n\n"
        "Regards,\nHireMe Team"
    )
    interview_reschedule_options_subject_template: str = (
        "Action required: approve alternative interview slots - HireMe ({role_title})"
    )
    interview_reschedule_options_body_template: str = (
        "Hi Hiring Manager,\n\n"
        "{candidate_name} requested to reschedule the technical interview for {role_title}.\n"
        "Please review the proposed options below and click one ACCEPT link.\n\n"
        "{slot_options}\n\n"
        "If none work, click REJECT to request the next set of options:\n"
        "{reject_link}\n\n"
        "Regards,\nHireMe Team"
    )
    offer_letter_subject_template: str = "Congratulations - Offer Letter for {role_title}"
    offer_letter_body_template: str = (
        "Hi {candidate_name},\n\n"
        "Congratulations! We are excited to see you on the team.\n"
        "Please find your offer letter attached as a PDF.\n\n"
        "Regards,\nHireMe Team"
    )
    manager_rejection_subject_template: str = "Application update - HireMe ({role_title})"
    manager_rejection_body_template: str = (
        "Hi {candidate_name},\n\n"
        "Thank you for your time and for interviewing with us. "
        "Sorry, we will not be able to move forward with your application.\n\n"
        "Regards,\nHireMe Team"
    )
    offer_signed_alert_subject_template: str = (
        "Signed: Offer Letter completed by {candidate_name} ({role_title})"
    )
    offer_signed_alert_body_template: str = (
        "Hi {manager_name},\n\n"
        "{candidate_name} ({candidate_email}) has signed the offer letter for {role_title}.\n"
        "Please review the candidate record in the admin dashboard.\n\n"
        "Regards,\nHireMe Team"
    )
    slack_invite_subject_template: str = "Join HireMe Slack - {role_title}"
    slack_invite_body_template: str = (
        "Hi {candidate_name},\n\n"
        "Congratulations again on signing your offer for the {role_title} role.\n"
        "Please join our Slack workspace using this invite link:\n"
        "{slack_invite_link}\n\n"
        "We are excited to see you in the team.\n\n"
        "Regards,\nHireMe Team"
    )
    slack_joined_alert_subject_template: str = (
        "Joined Slack: {candidate_name} is now onboarded ({role_title})"
    )
    slack_joined_alert_body_template: str = (
        "Hi {manager_name},\n\n"
        "{candidate_name} ({candidate_email}) has successfully joined the HireMe Slack workspace.\n"
        "Role: {role_title}\n"
        "Start date: {start_date}\n"
        "Joined at: {slack_joined_at}\n\n"
        "Regards,\nHireMe Team"
    )
    send_timeout_seconds: float = 5.0
    fail_submission_on_send_error: bool = False


class GoogleApiRuntimeConfig(BaseModel):
    """Non-secret Google API/OAuth metadata loaded from YAML."""

    project_id: str = ""
    auth_uri: str = "https://accounts.google.com/o/oauth2/auth"
    token_uri: str = "https://oauth2.googleapis.com/token"
    auth_provider_x509_cert_url: str = "https://www.googleapis.com/oauth2/v1/certs"


class TimeoutRuntimeConfig(BaseModel):
    """Request-timeout middleware configuration."""

    enabled: bool = True
    seconds: float = 30.0
    message: str = "request timed out"
    exempt_paths: list[str] = Field(default_factory=lambda: ["/health"])


class RateLimitRuntimeConfig(BaseModel):
    """Rate-limit middleware configuration."""

    enabled: bool = True
    window_seconds: int = 60
    max_requests: int = 60
    key_by_path: bool = True
    trust_x_forwarded_for: bool = False
    max_tracked_clients: int = 100_000
    cleanup_interval_seconds: int = 30
    message: str = "rate limit exceeded"
    exempt_paths: list[str] = Field(
        default_factory=lambda: [
            "/health",
            "/docs",
            "/openapi.json",
            "/redoc",
            "/docs/oauth2-redirect",
        ]
    )


class SecurityHeadersRuntimeConfig(BaseModel):
    """HTTP response header hardening configuration."""

    enabled: bool = True
    x_content_type_options: str = "nosniff"
    x_frame_options: str = "DENY"
    referrer_policy: str = "strict-origin-when-cross-origin"
    content_security_policy: str = "default-src 'none'; frame-ancestors 'none'; base-uri 'self';"
    include_hsts: bool = False
    hsts_max_age_seconds: int = 31_536_000
    hsts_include_subdomains: bool = True
    hsts_preload: bool = False
    csp_exempt_paths: list[str] = Field(
        default_factory=lambda: [
            "/docs",
            "/openapi.json",
            "/redoc",
            "/docs/oauth2-redirect",
        ]
    )


class CorsRuntimeConfig(BaseModel):
    """CORS configuration for browser-based frontend clients."""

    enabled: bool = True
    allow_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    allow_methods: list[str] = Field(default_factory=lambda: ["GET", "POST", "PATCH", "DELETE"])
    allow_headers: list[str] = Field(
        default_factory=lambda: ["Authorization", "Content-Type", "Accept"]
    )
    allow_credentials: bool = False
    expose_headers: list[str] = Field(default_factory=list)
    max_age_seconds: int = 600


class DocusignRuntimeConfig(BaseModel):
    """Runtime configuration for DocuSign e-signature workflow."""

    enabled: bool = False
    base_uri: str = "https://demo.docusign.net"
    oauth_base_uri: str = "https://account-d.docusign.com"
    account_id: str = ""
    webhook_url: str = "http://localhost:8000/api/v1/integrations/docusign/webhook"
    webhook_secret_query_param: str = "token"
    send_timeout_seconds: float = 20.0
    oauth_timeout_seconds: float = 15.0
    oauth_token_skew_seconds: int = 60
    envelope_subject_template: str = "Offer Letter - {role_title}"
    sign_here_x_position: str = "430"
    sign_here_y_position: str = "720"
    sign_here_page_number: str = "1"


class SlackRuntimeConfig(BaseModel):
    """Runtime configuration for Slack onboarding workflow."""

    enabled: bool = False
    api_base_url: str = "https://slack.com/api"
    send_timeout_seconds: float = 12.0
    signature_ttl_seconds: int = 300
    verify_event_signature: bool = True
    hr_channel_id: str = ""
    invite_team_id: str = ""
    invite_channel_ids: list[str] = Field(default_factory=list)
    onboarding_resource_links: list[str] = Field(default_factory=list)
    invite_custom_message_template: str = (
        "Hi {candidate_name}, welcome to the team. Please join the workspace to start onboarding."
    )


class RuntimeConfig(BaseModel):
    """Top-level runtime configuration model."""

    api: ApiRuntimeConfig = Field(default_factory=ApiRuntimeConfig)
    job_opening: JobOpeningRuntimeConfig = Field(default_factory=JobOpeningRuntimeConfig)
    application: ApplicationRuntimeConfig = Field(default_factory=ApplicationRuntimeConfig)
    storage: StorageRuntimeConfig = Field(default_factory=StorageRuntimeConfig)
    s3: S3StorageRuntimeConfig = Field(default_factory=S3StorageRuntimeConfig)
    postgres: PostgresRuntimeConfig = Field(default_factory=PostgresRuntimeConfig)
    error: ErrorRuntimeConfig = Field(default_factory=ErrorRuntimeConfig)
    security: SecurityRuntimeConfig = Field(default_factory=SecurityRuntimeConfig)
    parse: ParseRuntimeConfig = Field(default_factory=ParseRuntimeConfig)
    bedrock: BedrockRuntimeConfig = Field(default_factory=BedrockRuntimeConfig)
    evaluation: EvaluationRuntimeConfig = Field(default_factory=EvaluationRuntimeConfig)
    research: ResearchRuntimeConfig = Field(default_factory=ResearchRuntimeConfig)
    scheduling: SchedulingRuntimeConfig = Field(default_factory=SchedulingRuntimeConfig)
    notification: NotificationRuntimeConfig = Field(default_factory=NotificationRuntimeConfig)
    google_api: GoogleApiRuntimeConfig = Field(default_factory=GoogleApiRuntimeConfig)
    docusign: DocusignRuntimeConfig = Field(default_factory=DocusignRuntimeConfig)
    slack: SlackRuntimeConfig = Field(default_factory=SlackRuntimeConfig)
    cors: CorsRuntimeConfig = Field(default_factory=CorsRuntimeConfig)
    security_headers: SecurityHeadersRuntimeConfig = Field(
        default_factory=SecurityHeadersRuntimeConfig
    )
    timeout: TimeoutRuntimeConfig = Field(default_factory=TimeoutRuntimeConfig)
    rate_limit: RateLimitRuntimeConfig = Field(default_factory=RateLimitRuntimeConfig)


@lru_cache(maxsize=1)
def _get_yaml_module():
    """Import and cache the YAML module used by config loaders."""

    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PyYAML is not installed in the active Python environment. "
            "Install dependencies with `pip install -r requirements.txt` or run with "
            "`venv/bin/uvicorn app.main:app --reload`."
        ) from exc
    return yaml


@lru_cache(maxsize=32)
def _load_yaml_file(path_str: str) -> dict[str, Any]:
    """Load one YAML file and cache the parsed mapping by absolute path."""

    yaml = _get_yaml_module()
    path = Path(path_str)
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise RuntimeError(f"YAML config must be a mapping: {path}")
    return payload


@lru_cache(maxsize=1)
def get_runtime_config() -> RuntimeConfig:
    """Load and cache runtime configuration from YAML."""

    settings = get_settings()
    api_platform_config_path = settings.api_platform_config_path
    application_config_path = settings.application_config_path
    database_config_path = settings.database_config_path
    parse_config_path = settings.parse_config_path
    notification_config_path = settings.notification_config_path
    google_api_config_path = settings.google_api_config_path
    s3_config_path = settings.s3_config_path
    bedrock_config_path = settings.bedrock_config_path
    evaluation_config_path = settings.evaluation_config_path
    research_config_path = settings.research_config_path
    scheduling_config_path = settings.scheduling_config_path
    prompt_config_path = settings.prompt_config_path
    template_config_path = settings.template_config_path

    raw_api_platform_config = copy.deepcopy(
        _load_yaml_file(str(api_platform_config_path.resolve()))
    )
    raw_application_config = copy.deepcopy(_load_yaml_file(str(application_config_path.resolve())))
    raw_database_config = copy.deepcopy(_load_yaml_file(str(database_config_path.resolve())))
    raw_parse_config = copy.deepcopy(_load_yaml_file(str(parse_config_path.resolve())))
    raw_notification_config = copy.deepcopy(
        _load_yaml_file(str(notification_config_path.resolve()))
    )
    raw_google_api_config = copy.deepcopy(_load_yaml_file(str(google_api_config_path.resolve())))
    raw_s3_config = copy.deepcopy(_load_yaml_file(str(s3_config_path.resolve())))
    raw_bedrock_config = copy.deepcopy(_load_yaml_file(str(bedrock_config_path.resolve())))
    raw_evaluation_config = copy.deepcopy(_load_yaml_file(str(evaluation_config_path.resolve())))
    raw_research_config = copy.deepcopy(_load_yaml_file(str(research_config_path.resolve())))
    raw_scheduling_config = copy.deepcopy(_load_yaml_file(str(scheduling_config_path.resolve())))
    raw_prompt_config = copy.deepcopy(_load_yaml_file(str(prompt_config_path.resolve())))
    raw_template_config = copy.deepcopy(_load_yaml_file(str(template_config_path.resolve())))

    def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for key, value in override.items():
            existing = merged.get(key)
            if isinstance(existing, dict) and isinstance(value, dict):
                merged[key] = _merge_dicts(existing, value)
            else:
                merged[key] = value
        return merged

    def _extract_section(raw: dict[str, Any], section: str) -> dict[str, Any]:
        value = raw.get(section)
        if isinstance(value, dict):
            return value
        return raw

    def _unwrap_overlay(raw: dict[str, Any], wrapper: str) -> dict[str, Any]:
        value = raw.get(wrapper)
        if isinstance(value, dict):
            return value
        return raw

    combined_config = _merge_dicts(raw_api_platform_config, raw_application_config)
    combined_config = _merge_dicts(combined_config, raw_database_config)

    section_files = [
        ("parse", raw_parse_config),
        ("notification", raw_notification_config),
        ("s3", raw_s3_config),
        ("google_api", raw_google_api_config),
        ("bedrock", raw_bedrock_config),
        ("evaluation", raw_evaluation_config),
        ("research", raw_research_config),
        ("scheduling", raw_scheduling_config),
    ]
    for section, raw_config in section_files:
        combined_config = _merge_dicts(
            combined_config,
            {section: _extract_section(raw_config, section)},
        )

    combined_config = _merge_dicts(combined_config, _unwrap_overlay(raw_prompt_config, "prompts"))
    combined_config = _merge_dicts(
        combined_config, _unwrap_overlay(raw_template_config, "templates")
    )

    return RuntimeConfig.model_validate(combined_config)
