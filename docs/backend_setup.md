# HireMe Backend Setup

## Related Doc
- DB/S3/SQS setup: `docs/db_setup.md`
- Phase 02 implementation write-up: `docs/phase_02_hiring_intelligence_pipeline.md`

## Features implemented
- Async FastAPI REST API for job openings and candidate applications.
- Config split:
  - Runtime secrets/endpoints in `.env` (`DATABASE_URL`, `SQS_PARSE_QUEUE_URL`, `SQS_EVALUATION_QUEUE_URL`).
  - API/middleware/security behavior in `app/config/api_platform_config.yaml`.
  - Application/runtime behavior in `app/config/application_config.yaml`.
  - Database/storage behavior in `app/config/database_config.yaml`.
  - Parse runtime/heading mapping in `app/config/parse_config.yaml`.
  - Bedrock runtime in `app/config/bedrock_config.yaml`.
  - LLM evaluation prompt in `app/config/evaluation_config.yaml`.
  - Email notification runtime in `app/config/notification_config.yaml`.
  - Google API non-secret metadata in `app/config/google_api.yaml`.
  - S3 settings in `app/config/s3_config.yaml`.
  - Operational values in YAML (pool sizes, limits, middleware, etc.).
  - Keep credentials out of code and out of YAML.
- Job opening creation for:
  - `role_title`, `team`, `location`, `experience_level`, `experience_range`
  - `application_open_at`, `application_close_at`
  - `responsibilities`, `requirements`
  - runtime `status` field in response (`open`, `closed`, `paused`)
  - admin pause toggle endpoint blocks submissions when paused
- `location` is a single field: use `remote`, `onsite`, or a city/location name.
- `experience_level` values: `intern`, `junior`, `mid`, `senior`, `staff`, `principal`.
- `experience_range` format: `X-Y years` (example: `2-3 years`, `4-8 years`).
- Candidates can apply only to role titles that exist in created job openings.
- Application submit requires `role_selection` (must match existing opening `role_title`).
- Application submit is accepted only inside opening window (`application_open_at` to `application_close_at`).
- Duplicate protection:
  - same email cannot apply twice to the same job opening
  - same email can apply to different job openings
- UUID-based application records.
- UUID-based job opening records.
- Applicant record includes:
  - submitted fields (`full_name`, `email`, optional `linkedin_url`, required `portfolio_url`, required `github_url`, optional `twitter_url`)
  - `role_selection`
  - `resume.storage_path` (S3 URI such as `s3://hireme-cv-bucket/hireme/resumes/<file>.pdf` when using S3 backend)
  - `parse_result` (single JSON column, default `null`) with:
    - `skills`
    - `total_years_experience`
    - `education`
    - `work_experience` (position, company/old office, duration, job_description)
    - `old_offices`
    - `key_achievements`
  - `parse_status` (default `pending`)
  - `applicant_status` (default `applied`)
  - `applicant_status` is auto-updated to `shortlisted` when AI score is `>= ai_score_threshold`
  - `ai_score`, `ai_screening_summary`, `online_research_summary`
  - `rejection_reason` (`Candidate failed in initial screening.` or `Candidate did not meet the AI score threshold.`)
  - `parsed_total_years_experience`, `parsed_search_text` (fast prefilter columns)
  - `status_history` (for status timeline and admin override notes)
  - `reference_status` (default `false`, turns `true` when references are created via endpoint)
- PostgreSQL persistence for:
  - `job_openings` table
  - `applicant_applications` table (includes `job_opening_id` UUID foreign key)
- S3 object storage for resumes only.
- DB tables are auto-created on app startup when `storage.auto_create_tables: true`.
- No local JSON/file persistence path is used for job/applicant data.
- Resume size limits are configured in YAML by file type:
  - `application.max_pdf_size_mb`
  - `application.max_doc_size_mb`
  - `application.max_docx_size_mb`
- Timeout and rate-limit are configured in YAML:
  - `timeout.seconds`
  - `rate_limit.window_seconds`
  - `rate_limit.max_requests`
- JWT auth for admin routes is configured in YAML:
  - `security.enabled`
  - `security.jwt_algorithm`
  - `security.required_role`
  - `security.issuer`
  - `security.audience`
- HTTP hardening headers are configured in YAML:
  - `security_headers.*`
  - `security_headers.csp_exempt_paths` keeps Swagger docs usable

## Endpoints
- `POST /api/v1/admin/login` (admin username/password -> JWT bearer token)
- `GET /api/v1/admin/candidates` (admin list candidates, optional `job_opening_id`)
- `GET /api/v1/admin/candidates/{application_id}` (admin candidate details)
- `PATCH /api/v1/admin/candidates/{application_id}/status` (admin updates applicant status)
- `PATCH /api/v1/admin/candidates/{application_id}/review` (admin AI fields + override note)
- `POST /api/v1/admin/candidates/{application_id}/evaluate` (enqueue async LLM evaluation job, returns accepted response)
- `POST /api/v1/admin/candidates/{application_id}/evaluate/queue` (alias for queue submit)
- `POST /api/v1/job-openings`
- `GET /api/v1/job-openings`
- `DELETE /api/v1/job-openings/{job_opening_id}`
- `PATCH /api/v1/job-openings/{job_opening_id}/pause`
- `GET /api/v1/roles`
- `POST /api/v1/applications` (multipart form with resume file)
  - saves applicant metadata to Postgres
  - saves resume file to S3
  - enqueues parse job to SQS (`parse_status=pending`)
- `GET /api/v1/applications` (legacy admin list applicants, optional `job_opening_id`)
- `POST /api/v1/references` (create reference for a candidate application)
- `GET /api/v1/references?application_id=<uuid>` (list references for one candidate application)
- `GET /health`

## Online Research Script
- Config file: `app/config/research_config.yaml`
- Secrets in `.env`:
  - `SERPAPI_API_KEY`
  - `GITHUB_API_TOKEN`
  - `TWITTER_CONSUMER_KEY` (optional; used by standalone Twitter extractor, not by mocked enrichment flow)
  - `TWITTER_CONSUMER_SECRET` (optional; used by standalone Twitter extractor)
  - `TWITTER_BEARER_TOKEN` (optional; used by standalone Twitter extractor)
- Optional env overrides:
  - `SERPAPI_ENABLED`
  - `SERPAPI_GOOGLE_SEARCH_URL`
  - `SERPAPI_ENGINE`
  - `SERPAPI_ALWAYS_WEB_RETRIEVAL_ENABLED`
  - `SERPAPI_QUERY_PLANNER_USE_LLM`
  - `SERPAPI_RETRIEVAL_LOOP_USE_LLM`
- Run:
  ```bash
  venv/bin/python -m app.scripts.run_online_research --limit 100
  ```
- Shortlisted enrichment run:
  ```bash
  venv/bin/python -m app.scripts.enrich_shortlisted_candidates --limit 50
  ```
- Full shortlisted enrichment (structured extractors + cross-checks + primary/fallback LLM synthesis):
  ```bash
  venv/bin/python -m app.scripts.enrich_shortlisted_llm_profiles --limit 50
  ```
- `enrich_shortlisted_candidates` now runs in two phases per candidate:
  - phase 1 (no LLM): LinkedIn/X/portfolio search + GitHub API extraction into structured JSON
  - phase 2 (single LLM call, primary Bedrock model): cross-reference resume vs profiles, discrepancy detection, and 3-5 sentence candidate brief
- `enrich_shortlisted_llm_profiles` follows strict structured flow:
  - uses `parse_result` from candidate table
  - uses `linkedin_url` + `github_url` from candidate table (no guessed links)
  - mocks Twitter block intentionally in this pipeline
  - runs explicit resume-vs-LinkedIn and resume-vs-GitHub cross-checks
  - emits issue flags (`experience_mismatch`, `missing_projects`, `skill_differences`)
  - calls Bedrock primary model first and fallback model on failure
  - persists compact structured JSON in `online_research_summary`
- Prompt/config for phase 2 is in `app/config/research_config.yaml` under:
  - `research.enrichment.llm_analysis_enabled`
  - `research.enrichment.llm_prompt_template`

## Admin JWT Protection
- Protected with bearer JWT:
  - `GET /api/v1/admin/candidates`
  - `GET /api/v1/admin/candidates/{application_id}`
  - `PATCH /api/v1/admin/candidates/{application_id}/status`
  - `PATCH /api/v1/admin/candidates/{application_id}/review`
  - `POST /api/v1/admin/candidates/{application_id}/evaluate`
  - `POST /api/v1/admin/candidates/{application_id}/evaluate/queue`
  - `POST /api/v1/admin/candidates/{application_id}/research`
  - `POST /api/v1/admin/candidates/{application_id}/research/queue`
  - `POST /api/v1/job-openings`
  - `DELETE /api/v1/job-openings/{job_opening_id}`
  - `PATCH /api/v1/job-openings/{job_opening_id}/pause`
  - `GET /api/v1/applications`
  - `POST /api/v1/references`
  - `GET /api/v1/references`
- Public:
  - `POST /api/v1/admin/login` (admin-only credentials endpoint)
  - `GET /api/v1/job-openings`
  - `GET /api/v1/roles`
  - `POST /api/v1/applications`
  - There is no candidate login endpoint.

## Email Confirmation
- After successful `POST /api/v1/applications`, backend sends a confirmation email to candidate.
- Email templates and behavior are in `app/config/notification_config.yaml`.
- SMTP transport (`host`, `port`, `use_starttls`, `use_ssl`) is configured in `app/config/notification_config.yaml`.
- SMTP credentials are read from `.env`:
  - `SMTP_USERNAME`
  - `SMTP_PASSWORD`
- Google OAuth secrets are separate and optional for future Google API/OAuth use:
  - `GOOGLE_CLIENT_ID`
  - `GOOGLE_CLIENT_SECRET`

## Error format
- All API errors use a standard payload:
  - `error.code`
  - `error.message`
  - optional `error.details`
  - Configurable under `error` in `app/config/api_platform_config.yaml`.

## Pool Config
- DB connection pool is configured under `postgres` in `app/config/database_config.yaml`:
  - `pool_size`
  - `max_overflow`
  - `pool_timeout_seconds`
  - `pool_recycle_seconds`
  - `pool_pre_ping`
  - `connect_timeout_seconds`
  - `command_timeout_seconds`

## Queue Config
- Background parse queue/runtime knobs and section-heading aliases are under `parse` in `app/config/parse_config.yaml`.
- Parse queue endpoint is read from `.env` as `SQS_PARSE_QUEUE_URL`.
- LLM evaluation queue runtime knobs are under `evaluation` in `app/config/evaluation_config.yaml`.
- Evaluation queue endpoint is read from `.env` as `SQS_EVALUATION_QUEUE_URL`.
- Research enrichment queue runtime knobs are under `research.enrichment` in `app/config/research_config.yaml`.
- Research enrichment queue endpoint is read from `.env` as `SQS_RESEARCH_QUEUE_URL`.

## Workers
- Run API and workers as separate processes:
  ```bash
  venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
  ```
  ```bash
  venv/bin/python -m app.scripts.sqs_worker
  ```
  ```bash
  venv/bin/python -m app.scripts.sqs_evaluation_worker
  ```
  ```bash
  venv/bin/python -m app.scripts.sqs_research_enrichment_worker
  ```
- Submission path is fast and non-blocking for parsing:
  1) API stores applicant row in Postgres + resume in S3.
  2) API enqueues parse message to SQS.
  3) Worker consumes message, extracts text with LangChain `UnstructuredFileLoader`, and updates `parse_status`/`parse_result` in Postgres.
- Evaluation path is async and queue-backed:
  1) Admin triggers evaluation endpoint.
  2) API enqueues evaluation message to SQS and sets `evaluation_status=queued`.
  3) Evaluation worker sets `evaluation_status` through `in_progress -> completed|failed` and persists AI fields.
- Research enrichment path is async and queue-backed:
  1) Admin triggers research endpoint.
  2) API enqueues enrichment message to SQS.
  3) Research worker runs LinkedIn/GitHub extractors + cross-check + LLM synthesis and writes compact JSON to `online_research_summary`.
- Parse-first strategy:
  - first stage extracts raw text from PDF/DOCX
  - parse result stores compact structured fields only:
    - `skills`
    - `total_years_experience`
    - `education`
    - `work_experience`
    - `old_offices`
    - `key_achievements`

## Run locally
1. Create and activate virtualenv.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy env file:
   ```bash
   cp .env.example .env
   ```
4. Set `DATABASE_URL` in `.env` to your RDS PostgreSQL URL.
5. Set `SQS_PARSE_QUEUE_URL` in `.env` to your parse queue URL.
6. Set `SQS_EVALUATION_QUEUE_URL` in `.env` to your LLM evaluation queue URL.
7. Set `SQS_RESEARCH_QUEUE_URL` in `.env` to your research enrichment queue URL.
8. Set AWS credentials in `.env` (for S3/SQS/Bedrock):
   - `AWS_ACCESS_KEY_ID`
   - `AWS_SECRET_ACCESS_KEY`
   - optional: `AWS_SESSION_TOKEN`
   - optional: `BEDROCK_ENDPOINT_URL` (local/test override)
9. Set SMTP credential keys in `.env` for confirmation emails:
   - `SMTP_USERNAME`
   - `SMTP_PASSWORD`
10. Set JWT secret in `.env`:
   - `ADMIN_JWT_SECRET`
11. Set admin login credentials in `.env`:
   - `ADMIN_USERNAME`
   - `ADMIN_PASSWORD_HASH` (recommended) or `ADMIN_PASSWORD` (fallback)
12. Start API:
   ```bash
   venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
   ```
13. Start parse worker:
   ```bash
   venv/bin/python -m app.scripts.sqs_worker
   ```
14. Start evaluation worker:
  ```bash
  venv/bin/python -m app.scripts.sqs_evaluation_worker
  ```
15. Start research enrichment worker:
   ```bash
   venv/bin/python -m app.scripts.sqs_research_enrichment_worker
   ```
16. Ensure your app host can reach the RDS endpoint on `5432` (same VPC, VPN, or SSH tunnel).

## Tests
Run the test suite:
```bash
venv/bin/pytest -q
```

Lint and format checks:
```bash
venv/bin/black --check app tests
venv/bin/flake8 app tests
```

Generate admin JWT from terminal (optional helper if you do not use `/api/v1/admin/login`):
```bash
ADMIN_JWT_TOKEN="$(venv/bin/python -m app.scripts.generate_admin_jwt --subject hireme-admin)"
echo "$ADMIN_JWT_TOKEN"
```

Backfill parse summary columns for existing applications:
```bash
venv/bin/python -m app.scripts.backfill_parse_projection
```

## Example submit request
Create opening first:
```bash
ADMIN_JWT_TOKEN="$(venv/bin/python -m app.scripts.generate_admin_jwt --subject hireme-admin)"

curl -X POST http://localhost:8000/api/v1/job-openings \
  -H "Authorization: Bearer ${ADMIN_JWT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "role_title": "Backend Engineer",
    "team": "Platform",
    "location": "remote",
    "experience_level": "mid",
    "experience_range": "2-3 years",
    "application_open_at": "2026-03-25T09:00:00Z",
    "application_close_at": "2026-04-25T23:59:59Z",
    "responsibilities": ["Build APIs", "Design services"],
    "requirements": ["Python", "FastAPI", "PostgreSQL"]
  }'
```

Then submit application:
```bash
curl -X POST http://localhost:8000/api/v1/applications \
  -F "full_name=Jane Doe" \
  -F "email=jane@example.com" \
  -F "linkedin_url=https://www.linkedin.com/in/janedoe" \
  -F "portfolio_url=https://janedoe.dev" \
  -F "github_url=https://github.com/janedoe" \
  -F "twitter_url=https://x.com/janedoe" \
  -F "role_selection=Backend Engineer" \
  -F "resume=@/absolute/path/to/resume.pdf"
```

Admin login (preferred for frontend):
```bash
curl -X POST http://localhost:8000/api/v1/admin/login \
  -H "Content-Type: application/json" \
  -d '{
    "username": "admin",
    "password": "CHANGE_ME"
  }'
```

List applicants (admin):
```bash
curl -X GET http://localhost:8000/api/v1/admin/candidates \
  -H "Authorization: Bearer ${ADMIN_JWT_TOKEN}"
```

Pause one job opening (admin):
```bash
curl -X PATCH http://localhost:8000/api/v1/job-openings/<JOB_ID>/pause \
  -H "Authorization: Bearer ${ADMIN_JWT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"paused": true}'
```

## Frontend (Next.js)
- App is in `frontend/`.
- Candidate flow:
  - role dropdown sourced from `GET /api/v1/roles`
  - submit form to `POST /api/v1/applications`
  - shows success/error, including configured max-size guidance from backend.
- Admin flow:
  - login `POST /api/v1/admin/login`
  - dashboard table + filters from `GET /api/v1/admin/candidates`
  - candidate profile from `GET /api/v1/admin/candidates/{application_id}`
  - manual override + AI notes via `PATCH /api/v1/admin/candidates/{application_id}/review`
  - create/delete/pause openings via job-opening admin endpoints.
- Run:
```bash
cd frontend
cp .env.example .env.local
npm install
npm run dev
```
