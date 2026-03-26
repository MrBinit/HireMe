# HireMe DB Setup

## Scope
This document covers data/storage infrastructure for:
- PostgreSQL (metadata)
- S3 (resume files)
- SQS (parse queue + LLM evaluation queue)
- shortlisted research enrichment storage/output

## Storage Split
- PostgreSQL stores job openings and applicant metadata.
- S3 stores resume file objects only.
- SQS carries async parse and evaluation jobs.

## PostgreSQL
### Tables
- `job_openings`
- `applicant_applications`
  - includes `job_opening_id` foreign key
  - includes parse lifecycle fields: `parse_status`, `parse_result`, `applicant_status`
  - includes evaluation lifecycle field: `evaluation_status` (`queued`, `in_progress`, `completed`, `failed`)
  - includes `reference_status` (boolean) updated to `true` after reference submission
  - includes projection columns for search/scoring:
    - `parsed_total_years_experience` (double precision)
    - `parsed_search_text` (text)
  - includes `rejection_reason` (text) for screening/AI-threshold rejection
  - `parse_result` now stores:
    - structured extraction (`skills`, `total_years_experience`, `education`, `work_experience`, `old_offices`, `key_achievements`)
    - initial screening metadata (`initial_screening`)
- `applicant_references`
  - one-to-many with `applicant_applications` via `application_id` foreign key
  - stores reference contact details and relationship
  - candidate email in reference payload must match application email

### Connection
- Set in `.env`:
  - `DATABASE_URL=postgresql+asyncpg://<user>:<password>@<host>:5432/hireme`

### Pool Settings
- Set in `app/config/database_config.yaml` under `postgres`:
  - `pool_size`
  - `max_overflow`
  - `pool_timeout_seconds`
  - `pool_recycle_seconds`
  - `pool_pre_ping`
  - `connect_timeout_seconds`
  - `command_timeout_seconds`
  - `ssl_mode` (`require` for RDS with forced SSL, `disable` for local non-SSL Postgres)
  - `ssl_root_cert_path` (optional CA bundle path)

### Common RDS SSL Error
- If you see: `no pg_hba.conf entry ... no encryption`
  - your DB is reachable but SSL is required by RDS.
  - set `postgres.ssl_mode: require` in `app/config/database_config.yaml`.

## S3
### Purpose
- Resume uploads are written to S3 only.
- Application/job metadata is not stored in S3.

### Config
- Set in `app/config/s3_config.yaml`:
  - `bucket`
  - `region`
  - `resumes_prefix`
  - upload tuning values

## SQS Parse Queue
### Purpose
- Decouples parse work from submit API.
- Backend submits quickly and parser runs in worker process.

### Config
- `.env`:
  - `SQS_PARSE_QUEUE_URL`
- `app/config/parse_config.yaml` under `parse`:
  - `use_queue`
  - `provider`
  - `region`
  - `receive_batch_size`
  - `receive_wait_seconds`
  - `max_in_flight_per_worker`
  - `visibility_timeout_seconds`
  - `enqueue_timeout_seconds`
  - `fail_submission_on_enqueue_error`
  - `section_aliases` (heading map for section-based extraction)
  - `link_rules` (domain rules for `linkedin`, `github`, `project_links`, `personal_website`)

## SQS Evaluation Queue
### Purpose
- Decouples LLM scoring from request/response cycle.
- Admin evaluate endpoint is fast and enqueues scoring work.

### Config
- `.env`:
  - `SQS_EVALUATION_QUEUE_URL`
- `app/config/evaluation_config.yaml` under `evaluation`:
  - `use_queue`
  - `provider`
  - `region`
  - `queue_name`
  - `enqueue_timeout_seconds`
  - `max_in_flight_per_worker`
  - `receive_batch_size`
  - `receive_wait_seconds`
  - `visibility_timeout_seconds`

## Runtime Flow
1. Candidate submits application.
2. Resume uploads to S3.
3. Applicant row saves to Postgres.
4. Parse message publishes to SQS.
5. Worker consumes SQS message and extracts text from PDF/DOCX using LangChain `UnstructuredFileLoader`.
6. Worker updates parse state/result and projection columns in Postgres.
7. Worker runs initial screening against job opening requirements/range:
   - fail -> `applicant_status=rejected`, `rejection_reason=Candidate failed in initial screening.`
8. Admin triggers evaluation endpoint, API enqueues scoring job, and sets `evaluation_status=queued`.
9. Evaluation worker updates `evaluation_status` (`in_progress -> completed|failed`) and persists AI outputs.
10. AI fail threshold handling:
   - fail threshold -> `rejection_reason=Candidate did not meet the AI score threshold.`
11. AI pass threshold handling:
   - pass threshold -> `applicant_status=shortlisted`
12. Optional online-research enrichment script updates:
   - `linkedin_url` (if missing and discovered)
   - `twitter_url` (if missing and discovered)
   - `online_research_summary`
13. Strict shortlisted research enrichment (`enrich_shortlisted_llm_profiles`) writes compact structured JSON to `online_research_summary`:
   - `extractors` (`linkedin`, `github`, `twitter` mock block)
   - `cross_checks` (`resume_vs_linkedin`, `resume_vs_github`)
   - `issue_flags` (`experience_mismatch`, `missing_projects`, `skill_differences`)
   - `llm_analysis` (primary/fallback model output)
   - `brief` (3-5 sentence manager summary)
14. Optional async queue-backed research enrichment:
   - enqueue endpoint: `POST /api/v1/admin/candidates/{id}/research/queue`
   - worker: `venv/bin/python -m app.scripts.sqs_research_enrichment_worker`
   - queue URL env: `SQS_RESEARCH_QUEUE_URL`

## Research Enrichment Queue Note
- Queue mode is implemented for strict enrichment via SQS.
- Batch/cron execution is still supported for backfill runs.
- Worker-level concurrency is controlled by `research.enrichment.*` queue settings.

## High-Traffic Note
- PgBouncer is not configured in this repository.
- RDS Proxy is not configured in this repository.
- Current protection: async DB pool + queue worker separation.
- API rate limit is currently in-process memory (not distributed across instances).
- For heavy sustained traffic, add PgBouncer or RDS Proxy in front of RDS.
- For multi-instance global throttling, move rate limiting to Redis/API Gateway/WAF.
