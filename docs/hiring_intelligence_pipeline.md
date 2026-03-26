# Phase 02 Hiring Intelligence Pipeline

## 1) Scope
This document is the single source of truth for Phase 02:
- resume intake,
- AI screening,
- candidate research enrichment,
- manager-facing brief generation.

It covers implementation details, queue behavior under load, extractor logic, LLM scoring criteria, and known limitations.

## 2) What Is Built

### 2A. Admin Hiring Dashboard
- Admin list view includes:
  - candidate name,
  - role applied,
  - submission time,
  - applicant status,
  - evaluation status,
  - AI score,
  - resume download.
- Filters:
  - role,
  - applicant status (`applied`, `screened`, `shortlisted`, `in_interview`, `offer`, `rejected`),
  - submitted date range.
- Candidate detail view includes:
  - candidate overview,
  - public profile links (LinkedIn, GitHub, portfolio, Twitter link),
  - manager brief,
  - referees,
  - resume download.
- Manual override endpoint is implemented:
  - `PATCH /api/v1/admin/candidates/{application_id}/review`

### 2B. AI Resume Screening
- Resume is parsed into structured data.
- Candidate is screened against the specific job opening they applied to.
- AI evaluation generates:
  - score (`0-100`),
  - scoring rationale,
  - category breakdown.
- Threshold-based state transition is automatic:
  - pass -> `shortlisted`,
  - fail -> `rejected`.

### 2C. Candidate Research & Enrichment
- Runs only for eligible shortlisted candidates.
- Uses profile links already stored in candidate table:
  - LinkedIn URL,
  - GitHub URL,
  - optional portfolio URL,
  - optional Twitter URL (Twitter enrichment currently mocked in this pipeline).
- Performs:
  - resume vs LinkedIn cross-reference,
  - resume vs GitHub cross-reference,
  - discrepancy flagging,
  - 3-5 sentence manager brief generation.

## 3) End-to-End Runtime Flow
1. Candidate submits application (`POST /api/v1/applications`) with resume file.
2. API writes candidate metadata to PostgreSQL and resume object to S3.
3. Parse job is published to SQS parse queue.
4. Parse worker consumes job:
   - extracts structured resume fields,
   - computes initial screening metadata,
   - sets status to `screened` or `rejected`.
5. If eligible, parse worker enqueues LLM evaluation job.
6. Evaluation worker consumes job:
   - runs LLM scoring,
   - writes `ai_score`, `ai_screening_summary`, `evaluation_status`,
   - auto-updates status (`shortlisted` or `rejected`),
   - enqueues research job if score passes threshold.
7. Research worker consumes job:
   - runs LinkedIn/GitHub/portfolio extractors + Twitter mock,
   - builds cross-check and issue flags,
   - calls synthesis LLM (primary, fallback on failure),
   - stores compact structured research JSON + `candidate_brief`.

## 4) Queue and Async Design (Traffic Spike Handling)
All expensive work is asynchronous and queue-backed so submission latency remains low.

### Queues
- Parse: `SQS_PARSE_QUEUE_URL`
- Evaluation: `SQS_EVALUATION_QUEUE_URL`
- Research: `SQS_RESEARCH_QUEUE_URL`

### Worker controls
Each worker uses long polling and bounded concurrency:
- `receive_wait_seconds` (long polling),
- `receive_batch_size`,
- `max_in_flight_per_worker`,
- `visibility_timeout_seconds`.

### Why this handles spikes
- API path stays fast (store + enqueue only).
- Backlog accumulates in SQS instead of timing out HTTP requests.
- Throughput scales by running more worker processes/containers.
- Worker eligibility checks reduce wasted work:
  - skip duplicate evaluation/research if already completed/in-progress.

## 5) Resume Parsing and Initial Screening
Implementation: `app/services/parse_processor.py`

### Extracted fields
- `skills`
- `total_years_experience`
- `education`
- `work_experience`
- `old_offices`
- `key_achievements`

Persisted as:
- `parse_result` (JSON),
- `parsed_total_years_experience`,
- `parsed_search_text`.

### Initial screening rule
`passed = experience_pass AND (skills_pass OR keyword_pass)`

### Experience pass
- Candidate experience compared with job opening `experience_range`.

### Skills pass
- Required skill keywords are extracted from job requirements.
- Candidate parsed skills are matched.
- Minimum matches enforced by:
  - `application.prefilter_min_skill_matches`.

### Keyword pass
- Keywords extracted from requirements/responsibilities.
- Matched against normalized parsed resume search text.
- Minimum matches enforced by:
  - `application.prefilter_min_keyword_matches`.

## 6) AI Evaluation (LLM Scoring)
Implementation: `app/services/candidate_evaluation_service.py`

### LLM input used for scoring
- Candidate parsed skills,
- computed years of experience,
- condensed work-history summary,
- education summary,
- job role, requirements, experience range, and JD text.

### Scoring rubric (from `app/config/evaluation_config.yaml`)
- Skills Match: `0-40`
- Experience Match: `0-30`
- Education Match: `0-10`
- Role Alignment: `0-20`
- Total: `0-100`

### Models
- Primary scoring model:
  - `us.anthropic.claude-sonnet-4-20250514-v1:0`
- Work-summary model (fallback model slot in scoring service):
  - `us.anthropic.claude-3-5-haiku-20241022-v1:0`

### Output persisted
- `ai_score`
- `ai_screening_summary`
- `evaluation_status`
- applicant status transition (`shortlisted` / `rejected`)

## 7) Research Enrichment Extractors
Orchestrator: `app/scripts/enrich_shortlisted_llm_profiles.py`

### LinkedIn extractor: how it works
Implementation:
- `app/scripts/extract_linkedin_cross_reference.py`

Pipeline:
1. Build SerpAPI queries from provided LinkedIn URL + candidate name + role.
2. Retrieve top Google hits and filter LinkedIn profile candidates.
3. Parse LinkedIn evidence into structured sections (experience, education, projects, skills).
4. Build cross-reference payload against resume signals:
   - matched/unmatched employers,
   - matched/unmatched positions,
   - matched/unmatched skills.
5. Return structured evidence and cross-reference output.

### GitHub extractor: how it works
Implementation:
- `app/scripts/extract_github_profile.py`

Pipeline:
1. Parse username from URL/handle.
2. Call GitHub REST API for user/repositories.
3. Rank top repositories by stars/forks/recency.
4. Extract:
   - top repos,
   - stars,
   - primary languages,
   - activity status (recency-based),
   - README summary for top repos.
5. Build aggregate signals used in cross-checks and final synthesis.

### Portfolio extractor: how it works
Implementation:
- `app/scripts/extract_portfolio_profile.py`

Pipeline:
1. Build SerpAPI queries from portfolio URL/domain (+ optional name/role hints).
2. Fetch and dedupe hits.
3. Pick primary hits by exact URL/prefix/domain match.
4. Extract:
   - technology signals,
   - project signals,
   - top evidence hits.

### Twitter extractor strategy
- A standalone extractor exists (`app/scripts/extract_twitter_profile.py`) and can use Twitter API v2 when an exact handle is known.
- In the current enrichment orchestrator, Twitter is intentionally mocked.
- Reason:
  - automatic identity matching by name is unreliable and high-risk.
  - we avoid producing incorrect profile attributions.

## 8) How Data Is Combined Before Final LLM Synthesis
For each shortlisted candidate, the orchestrator creates:
- `resume_snapshot` (from parsed resume),
- `extractors`:
  - LinkedIn output,
  - GitHub output,
  - portfolio output,
  - Twitter mock block,
- `cross_checks`:
  - `resume_vs_linkedin`,
  - `resume_vs_github`,
- `issue_flags`:
  - `experience_mismatch`,
  - `missing_projects`,
  - `skill_differences`.

These are injected into a strict JSON-generation prompt (no hallucination policy), then sent to Bedrock.

## 9) Final Synthesis LLM

### Prompt contract
LLM is instructed to:
- validate cross-checks,
- keep/adjust issue flags,
- output strengths and risks,
- produce a 3-5 sentence brief.

### Model policy
- Primary: `us.anthropic.claude-sonnet-4-20250514-v1:0`
- Fallback: `us.anthropic.claude-3-5-haiku-20241022-v1:0`
- If both fail: deterministic fallback summary is used.

### Output persistence
- `candidate_brief` (manager-facing 3-5 sentence summary),
- `online_research_summary` (compacted structured JSON).

## 10) Limitations and Improvements

### Limitation: Twitter profile ambiguity
- Problem:
  - many users share same/similar names.
  - automatic lookup risks wrong identity linkage.
- Current behavior:
  - Twitter enrichment is mocked in the orchestration flow.
- Improvement:
  - require applicant-provided exact X handle in form,
  - verify returned profile URL matches submitted handle before ingestion,
  - optionally add manual confirmation when confidence is low.

### Limitation: LinkedIn public data coverage
- Problem:
  - no private LinkedIn API usage; visibility depends on indexed public pages.
- Improvement:
  - collect candidate-provided LinkedIn text export/paste,
  - add stronger structured parser and evidence confidence scoring.

### Limitation: GitHub project relevance scoring
- Problem:
  - top-star repos are not always most role-relevant.
- Improvement:
  - add JD-aware project ranking using embeddings + README similarity,
  - weight by recency, ownership, and contribution depth.

### Limitation: Portfolio signal noise
- Problem:
  - web search snippets can include unrelated pages.
- Improvement:
  - crawl only verified portfolio domain paths,
  - add page-level dedupe + relevance classifier.

### Limitation: API and rate constraints
- Problem:
  - external API limits can reduce throughput.
- Improvement:
  - add retries with jitter, circuit-breaker behavior, and caching layers.

## 11) Operational Requirements

### Required workers
- `venv/bin/python -m app.scripts.sqs_worker`
- `venv/bin/python -m app.scripts.sqs_evaluation_worker`
- `venv/bin/python -m app.scripts.sqs_research_enrichment_worker`

### Required credentials
- `SERPAPI_API_KEY`
- `GITHUB_API_TOKEN`
- Bedrock credentials/config
- SQS queue URLs

Twitter API credentials are optional for the current orchestrated flow because Twitter is mocked there.

## 12) Why This Is Production-Oriented
- Submission path is non-blocking and queue-first.
- Each stage has isolated worker responsibility.
- Status transitions are explicit and persisted.
- Model usage has fallback paths.
- Profile enrichment is structured, auditable JSON with explicit discrepancy flags.
- Manager-facing output is concise and directly usable in interview prep.
