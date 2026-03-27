# Phase 02 Documentation: Admin Dashboard, AI Screening, and Candidate Research

## 1) Goal
After a resume is submitted, the system should automatically:
- run first-layer screening (cheap filter),
- run AI scoring against the exact job description,
- enrich shortlisted candidates with online research,
- surface results to hiring users so they can review faster.

This is the intelligence layer that reduces manual recruiter/interviewer effort.

## 2) High-Level Flow (How We Built It)
1. Candidate submits application + resume (`POST /api/v1/applications`).
2. Resume is saved in S3 and candidate metadata is saved in PostgreSQL.
3. Parse job goes to SQS parse queue.
4. Parse worker extracts structured resume fields and runs initial screening filter.
5. If initial screening passes, evaluation job is queued.
6. Evaluation worker runs LLM scoring prompt and writes score + rationale.
7. If score passes threshold (`70.0`), status becomes `shortlisted`.
8. Research enrichment job is queued for shortlisted candidates.
9. Research worker runs profile extractors + discrepancy checks + brief generation.
10. Admin/referee portals read candidate data from backend APIs.

## 3) 2A - Admin Hiring Dashboard

### What admin can see in list view
Admin table shows:
- name
- role applied
- status
- evaluation status
- email
- submission date
- AI score
- resume download action

### Filters available
Dashboard filters:
- role
- status
- submitted from date
- submitted to date

Status options include at least:
- `applied`
- `screened`
- `shortlisted`
- `in_interview`
- `offer`
- `rejected`

### Candidate profile view
Candidate detail view currently includes:
- core profile (email, role, status, score, links)
- resume download
- hiring manager brief (3-5 sentence summary)
- interview/offer lifecycle fields
- referee entries

Also available from candidate API payload (`ApplicationRecord`):
- `ai_screening_summary`
- `online_research_summary`
- `status_history`

### Manual override support
Manual override API is implemented:
- `PATCH /api/v1/admin/candidates/{application_id}/review`

Admin can override applicant status and attach a note; note/history is persisted in `status_history` in DB.

## 4) Referee Flow (Real-World Reference Validation)
Referee is treated as a first-class actor:
- referee login endpoint: `POST /api/v1/referee/login`
- referee candidate list/detail:
  - `GET /api/v1/referee/candidates`
  - `GET /api/v1/referee/candidates/{application_id}`
- referee reference submit:
  - `POST /api/v1/referee/references`

### Important validation
Referee payload requires `candidate_email`.  
Backend validates:
- `application_id` exists
- `candidate_email` exactly matches that application's email

This prevents attaching a reference to the wrong applicant.

### Duplicate protection for referee entries
Duplicate reference protection is enforced with unique constraint:
- `(application_id, referee_email)`

## 5) 2B - AI Resume Screening

## Stage 1: Initial low-cost screening (to reduce LLM cost)
Done in parse processor before LLM scoring.

### Structured fields extracted from resume
- skills
- total years of experience
- education
- work experience
- old offices/employers
- key achievements

### First-layer screening rule
`passed = experience_pass AND (skills_pass OR keyword_pass)`

### How initial screening is executed (step-by-step)
1. Parse worker extracts structured resume signals (`skills`, `work_experience`, `education`, `key_achievements`).
2. System loads the exact job opening the candidate applied to.
3. Experience gate:
- parse JD `experience_range` (example `2-4 years`)
- compare with parsed total years of experience
4. Skill gate:
- extract normalized skill keywords from JD requirements
- count matches against parsed candidate skills
- require minimum matches (`prefilter_min_skill_matches`)
5. Keyword gate:
- build keyword set from JD requirements + responsibilities
- compare against normalized `parsed_search_text`
- require minimum matches (`prefilter_min_keyword_matches`)
6. Decision:
- pass only if `experience_pass` and (`skills_pass` or `keyword_pass`)
7. Persist screening metadata in `parse_result.initial_screening`:
- pass/fail booleans
- matched counts
- sample matched skills/keywords
- rule used (`experience_and_(skills_or_keywords)`)
8. Auto status update:
- pass -> `screened`
- fail -> `rejected` (with rejection reason and optional email)

### Filters used in this stage
- experience range check against JD (`experience_range`)
- skill keyword match count from JD requirements
- general keyword match count from JD requirements + responsibilities

Configured defaults:
- `prefilter_min_keyword_length = 3`
- `prefilter_max_keywords = 24`
- `prefilter_min_keyword_matches = 5`
- `prefilter_min_skill_matches = 5`
- `prefilter_max_search_text_chars = 8000`

If this stage fails:
- candidate is set to `rejected`
- rejection reason: `Candidate failed in initial screening.`
- optional rejection email is sent

This saves LLM cost by not scoring clearly weak matches.

## Stage 2: LLM score against the applied job
Only candidates that pass initial screening reach this stage.

### LLM scoring prompt design
Prompt is rubric-based and strict JSON output.

Scoring categories:
- Skills Match: `0-40`
- Experience Match: `0-30`
- Education Match: `0-10`
- Role Alignment: `0-20`
- Total: `0-100`

LLM input includes:
- parsed skills
- parsed years experience
- compact work summary
- education summary
- target role
- required skills
- experience range
- combined JD text (responsibilities + requirements)

LLM output required:
- `score`
- category `breakdown`
- short `reason`

Prompt rules enforced:
- do not exceed per-category limits
- do not hallucinate missing data
- be strict and realistic
- prefer conservative scoring

### How the AI score prompt is constructed
The evaluation service builds one prompt by injecting runtime candidate + JD values into template placeholders.

Template placeholders used:
- `{skills}`
- `{years}`
- `{work_summary}`
- `{education}`
- `{role}`
- `{required_skills}`
- `{min_exp}`
- `{max_exp}`
- `{job_description}`

The resulting prompt gives the model:
- candidate parsed profile summary
- target role requirements
- strict category scoring rubric
- strict JSON output contract

### JSON output contract for scoring prompt
The model is required to return:
- `score` (0-100)
- `breakdown.skills` (0-40)
- `breakdown.experience` (0-30)
- `breakdown.education` (0-10)
- `breakdown.role_alignment` (0-20)
- `reason` (short rationale)

Backend validation then enforces:
- numeric bounds per category
- `score` consistency with breakdown total (within tolerance)

If output is invalid JSON or fails schema checks, evaluation is treated as failed.

### Supporting prompt used before scoring
Before main scoring, a summary prompt (`summary_prompt_template`) is used to compress work history into concise text (`work_summary`) for more stable scoring input.

### Threshold behavior
Configured threshold:
- `ai_score_threshold = 70.0`

If score is `>= 70`:
- applicant status auto-updates to `shortlisted`

If score is `< 70`:
- applicant status auto-updates to `rejected`

## 6) 2C - Candidate Research and Profile Enrichment
Research runs for shortlisted candidates to reduce interviewer prep time.

### Sources and tools used
- LinkedIn search/extraction: SerpAPI (Google results)
- Portfolio search/extraction: SerpAPI
- GitHub profile/repo enrichment: GitHub API
- X/Twitter extraction module: Twitter API v2 extractor exists

### Current runtime behavior note
In current queued shortlist pipeline (`enrich_shortlisted_llm_profiles.py`), Twitter is intentionally mocked by default to avoid false profile attribution risk.  
Standalone Twitter API extractor exists and can be used where handle confidence is high.

### Why identity matching is hard
Name-only matching is noisy:
- many candidates share same/similar names
- wrong profile attribution can create false signals

Identity confidence strategy used:
- prefer candidate-provided URLs from application form
- for LinkedIn, prefer exact handle match from provided URL
- fallback to name-congruent hits only if needed
- keep unmatched evidence explicit instead of guessing

### What each extractor produces
LinkedIn extraction/cross-check:
- matched profile URL
- matched/unmatched resume skills
- matched/unmatched employers
- matched/unmatched positions
- evidence lines from search snippets

GitHub extraction:
- username/profile info
- top repositories
- languages
- stars/forks/activity signals
- README-derived summaries

Portfolio extraction:
- matched portfolio URL/domain
- technology signals
- project signals
- top hits and evidence

### Cross-reference and discrepancy flags
System compares resume signals against extracted online signals and flags:
- `experience_mismatch`
- `missing_projects`
- `skill_differences`

### Final research synthesis (LLM + fallback)
Inputs to synthesis step:
- resume snapshot
- extractor outputs
- cross-check results
- issue flags

Expected output:
- concise cross-reference summary
- discrepancy list
- 3-5 sentence candidate brief (readable under ~60 seconds)

Synthesis prompt is strict about:
- use only provided JSON evidence
- no invented facts
- explicit "insufficient public evidence" when data is weak
- strict JSON response shape for downstream storage

Stored fields:
- `online_research_summary` (structured compact JSON)
- `candidate_brief` (manager-friendly brief)

## 7) Prompt and Scoring Summary (Quick Reference)
- Screening prompt: rubric-based, strict JSON, conservative scoring.
- Research synthesis prompt: strict JSON, no hallucination, discrepancy-aware summary.
- Shortlist cutoff: `70`.

### Other prompts used in this phase
- Evaluation work-summary prompt (`evaluation.summary_prompt_template`)
- Evaluation scoring prompt (`evaluation.prompt_template`)
- Research enrichment prompt (`research.enrichment.llm_prompt_template`) for cross-reference/discrepancy/brief JSON
- In shortlisted LLM pipeline, a strict in-code synthesis prompt is also used for:
  - resume vs LinkedIn/GitHub validation
  - issue flag normalization
  - strengths/risks + 3-5 sentence manager brief

## 8) Evaluation Prompt (Planned, not fully implemented)
Due to time constraints, a dedicated offline evaluation stage is documented but not fully implemented in code yet.

### Why this is needed
- AI score can vary across runs for borderline candidates.
- Noisy social/profile evidence can increase hallucination risk.
- Hiring decisions need traceable quality checks, not only runtime prompts.

### Planned evaluation prompt design
Evaluator model input:
- candidate parsed payload
- target JD
- model score output (`score`, `breakdown`, `reason`)
- optional human label (`reject` / `borderline` / `shortlist`)

Planned evaluator prompt objective:
- check scoring consistency and rubric adherence
- check evidence grounding of rationale
- flag potential bias-sensitive reasoning patterns
- produce machine-readable evaluation JSON

Planned output contract:
- `agreement_with_label` (0-1)
- `score_variance_risk` (`low`/`medium`/`high`)
- `grounding_issues` (list)
- `bias_risk_flags` (list)
- `evaluator_note` (short summary)

## 9) Limitations and Improvement Plan

### Current limitations
- Twitter identity resolution is high risk with name-only search.
- LinkedIn/portfolio rely on public web indexing quality.
- Candidate detail UI currently prioritizes brief; some raw analysis fields are available in API but can be surfaced more directly in UI.
- A dedicated human evaluation checklist/approval checkpoint is still needed in this process; due to time constraints, this manual-evaluation workflow was not fully added as a separate stage.

### Improvements with more time
1. Require verified X handle input and enforce exact URL match before ingestion.
2. Add confidence scoring per source (LinkedIn/GitHub/portfolio/X).
3. Show full `ai_screening_summary`, `online_research_summary`, and `status_history` blocks directly in candidate detail UI.
4. Add stronger evidence ranking for JD relevance (embedding/rerank).
5. Add explicit source confidence + discrepancy severity for better hiring decisions.
