# Parse, Models, and Filtering Guide

## Purpose
This document explains the backend candidate-processing pipeline:
- parse flow,
- LLM models used for summary and scoring,
- filtering logic,
- and exactly how data is saved/updated in PostgreSQL.

## 1) End-to-End Processing Flow
1. Admin creates a job opening in `job_openings`.
2. Candidate submits application + resume.
3. API stores candidate data in PostgreSQL (`applicant_applications`) and uploads resume file to S3.
4. A parse job is published to SQS.
5. Parse worker extracts resume text and writes structured parse data to `parse_result`.
6. Worker computes parse projections used for filtering:
   - `parsed_total_years_experience`
   - `parsed_search_text`
7. Worker runs initial screening (experience + keyword match).
8. If initial screening fails:
   - `applicant_status = rejected`
   - `rejection_reason = Candidate failed in initial screening.`
9. If initial screening passes:
   - `applicant_status = screened`
10. Admin triggers LLM evaluation for screened candidates via async queue endpoints:
    - `/admin/candidates/{id}/evaluate`
    - `/admin/candidates/{id}/evaluate/queue` (alias)
   - API enqueues and marks `evaluation_status=queued`
11. Evaluation worker consumes queued jobs and persists AI score + summary.
   - worker transitions `evaluation_status` as `in_progress -> completed|failed`
12. Below-threshold scores can auto-reject.

## 2) LLM Models and Usage
Model config source: `app/config/bedrock_config.yaml`.

## Primary model (final scoring)
- `us.anthropic.claude-sonnet-4-20250514-v1:0`
- Used for strict JSON score generation (`score`, `breakdown`, `reason`).
- Prompt source: `app/config/evaluation_config.yaml` -> `evaluation.prompt_template`.

## Fallback model (work summary)
- `us.anthropic.claude-3-5-haiku-20241022-v1:0`
- Used to summarize work history before scoring.
- Prompt source: `app/config/evaluation_config.yaml` -> `evaluation.summary_prompt_template`.
- If fallback call fails/timeouts, service uses deterministic local summary instead.

## Runtime controls
- Timeout: `bedrock.request_timeout_seconds`
- Retries: `bedrock.max_retries`
- Concurrency: `bedrock.max_concurrency` enforced via semaphore in evaluation service.

## Async scoring queue controls
Source: `app/config/evaluation_config.yaml` -> `evaluation`
- `use_queue`
- `provider`
- `queue_name`
- `enqueue_timeout_seconds`
- `max_in_flight_per_worker`
- `receive_batch_size`
- `receive_wait_seconds`
- `visibility_timeout_seconds`
- Queue URL is provided from `.env` as `SQS_EVALUATION_QUEUE_URL`.

## 3) Filtering Logic

## Submission-time checks (`ApplicationService.submit`)
- Role must exist.
- Application must be within open/close window.
- Job must not be paused.
- File extension/content-type must be allowed.
- File size must be within configured limit.

## Parse-time initial screening (`ResumeParseProcessor`)
- Experience check:
  - candidate parsed years vs job `experience_range` (e.g., `2-4 years`)
- Skills check:
  - extracts skill-focused keywords from job requirements
  - compares against candidate parsed skills
  - requires minimum skill matches (`application.prefilter_min_skill_matches`)
- Keyword check:
  - extracts keywords from job requirements/responsibilities
  - compares against candidate `parsed_search_text`
  - requires minimum matches (`application.prefilter_min_keyword_matches`)
- Final pass rule:
  - `experience_pass AND (skills_pass OR keyword_pass)`

Screening metadata is saved in:
- `parse_result.initial_screening`

## Configured thresholds (current values)
Source: `app/config/application_config.yaml` -> `application`

- Keyword screening threshold:
  - `prefilter_min_keyword_matches: 5`
  - Effective rule in code:
    - `required_keyword_matches = min(total_extracted_keywords, max(1, prefilter_min_keyword_matches))`
  - Practical meaning:
    - Candidate must match at least 5 extracted keywords (or all keywords if fewer than threshold).

- Skills screening threshold:
  - `prefilter_min_skill_matches: 5`
  - Effective rule in code:
    - `required_skill_matches = min(total_required_skill_keywords, max(1, prefilter_min_skill_matches))`
  - Practical meaning:
    - Candidate must match at least 5 required skill keywords (or all if fewer than threshold).

- AI score threshold:
  - `ai_score_threshold: 70.0`
  - Practical meaning:
    - If `ai_score < 70.0`, candidate is marked rejected with:
      - `rejection_reason = Candidate did not meet the AI score threshold.`
    - If `ai_score >= 70.0`, no auto-rejection is applied by threshold logic.

## Admin-side candidate filtering (`GET /api/v1/admin/candidates`)
Supported filters:
- `job_opening_id`
- `role_selection`
- `applicant_status`
- `submitted_from`, `submitted_to`
- `keyword_search`
- `experience_within_range`
- `prefilter_by_job_opening=true` (derives keyword + experience filters from opening)

## 4) Database Behavior

## Table: `job_openings`
Stores employer posting data:
- `id` (UUID)
- `role_title` (unique)
- `team`
- `location`
- `experience_level`
- `experience_range`
- `application_open_at`, `application_close_at`
- `paused`
- `responsibilities` (JSON)
- `requirements` (JSON)

## Table: `applicant_applications`
Stores candidate, parse, and screening lifecycle:
- identity/profile:
  - `id`, `job_opening_id`, `full_name`, `email`, `role_selection`, profile URLs
- resume metadata:
  - `resume_original_filename`
  - `resume_stored_filename`
  - `resume_storage_path`
  - `resume_content_type`
  - `resume_size_bytes`
- parse pipeline:
  - `parse_status`
  - `parse_result` (JSON)
  - `parsed_total_years_experience`
  - `parsed_search_text`
- screening/review:
  - `applicant_status`
  - `evaluation_status` (`queued`, `in_progress`, `completed`, `failed`)
  - `ai_score`
  - `ai_screening_summary`
  - `online_research_summary`
  - `rejection_reason`
  - `status_history`
- references:
  - `reference_status`

## Constraints and relations
- Unique: `(job_opening_id, email)` prevents duplicate application per role.
- Foreign key: `job_opening_id -> job_openings.id`.

## S3 + DB split
- Resume file bytes are stored only in S3.
- DB stores only metadata and S3 path.

## 5) Key Config Files
- `app/config/api_platform_config.yaml`
- `app/config/application_config.yaml`
- `app/config/parse_config.yaml`
- `app/config/bedrock_config.yaml`
- `app/config/evaluation_config.yaml`
- `app/config/database_config.yaml`
- `app/config/s3_config.yaml`

Sensitive credentials belong in `.env` (DB URL, AWS creds, JWT secret, SMTP creds).
