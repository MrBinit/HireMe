# HireMe - AI Hiring Pipeline

HireMe is an end-to-end hiring workflow that moves a candidate from application submission to interview scheduling, offer signature, and Slack onboarding.

## Project Summary
This project includes:
- Candidate-facing career flow with job listings and application form
- Resume parsing and first-layer screening
- LLM-based candidate scoring and shortlisting
- Candidate research enrichment (LinkedIn/X/GitHub/portfolio)
- Interview scheduling orchestration with Google Calendar holds
- Offer-letter e-signature flow with DocuSign
- Post-signature Slack onboarding automation with AI-personalized welcome message

## Tech Stack
- Backend: FastAPI (Python)
- Frontend: Next.js
- Database: PostgreSQL
- Object storage: AWS S3 (resume + offer artifacts)
- Queues/async workers: AWS SQS
- AI inference: AWS Bedrock
- Email: SMTP-based notification service
- Runtime/orchestration: Docker + docker-compose

## Deployment (AWS)
- Container registry: **AWS ECR** stores versioned Docker images for backend/frontend worker services.
- Secrets handling: **AWS Secrets Manager** stores API keys, tokens, DB credentials, and integration secrets (DocuSign/Slack/Fireflies/etc.) instead of hardcoding in code or images.
- Compute runtime: **AWS EC2 VM** runs pulled Docker images (from ECR) and starts application services.
- Frontend delivery: **CloudFront** is placed in front of the frontend origin for caching, HTTPS delivery, and lower-latency global access.

### Deployment flow
1. Build Docker images for services.
2. Push images to AWS ECR.
3. Provision/update secrets in AWS Secrets Manager.
4. On EC2, pull latest images from ECR.
5. Inject secrets into runtime environment.
6. Run backend/worker services on EC2; run frontend service on EC2 as frontend origin.
7. Route frontend traffic through CloudFront (EC2 frontend origin) for edge delivery.

## AI Tools and Models Used
- Primary LLM tasks (screening/research synthesis): AWS Bedrock primary model
- Secondary LLM tasks (cost-optimized summarization/welcome): AWS Bedrock fallback model
- Prompt-based scoring for candidate-job fit (0-100)
- Structured JSON-constrained prompts for deterministic parsing of model output
- Coding support used during project implementation: **OpenAI Codex coding agent**

## External APIs and Integrations
- Google Calendar API:
  - Reads interviewer availability
  - Creates tentative hold slots
  - Confirms one selected slot and generates Google Meet link
- Fireflies API (GraphQL):
  - Attempts live interview capture and transcript retrieval
  - Stores transcript URL + summary on candidate profile
- DocuSign API:
  - Sends offer letter envelope for digital signature
  - Webhook/callback updates signed/declined state
- Slack API:
  - Sends workspace invite after offer signature
  - Handles first join event and sends AI-personalized welcome DM
  - Sends HR/internal onboarding confirmation message
- SerpAPI + profile extractors:
  - LinkedIn discovery and cross-reference
  - Portfolio discovery
- GitHub API:
  - Repository/activity/language extraction for profile enrichment
- X/Twitter API (v2):
  - Best-effort profile/post extraction when handle is available
  - Falls back safely when identity resolution is weak or API data is unavailable

## How Each Major Integration Works
1. Application + screening:
- Candidate submits resume and metadata.
- Resume is parsed asynchronously.
- Lightweight pre-filter runs before LLM to reduce cost/latency.
- If pre-filter passes, LLM score is computed; threshold controls shortlist.

2. Research enrichment:
- For shortlisted candidates, enrichment workers gather LinkedIn/X/GitHub/portfolio evidence.
- System cross-checks against resume and generates discrepancies + 3-5 sentence brief.

3. Scheduling:
- System finds 3-5 manager slots (45 min) in next business window.
- All offered slots are held immediately to prevent conflicts.
- On candidate confirmation, one slot is finalized and others are released.
- Candidate can accept directly in Google Calendar (`Yes`) without replying to email.
- If candidate declines in Google Calendar (`No`), interview should be canceled and moved to cancellation/reschedule handling.

4. Offer + onboarding:
- Offer letter is generated and sent via DocuSign.
- On signature completion, Slack invite flow starts.
- On first Slack join, AI-generated personalized welcome is sent and HR is notified.

## Deliberate Trade-offs (Requirement)
This project intentionally made trade-offs to deliver a working end-to-end system within limited time.

1. Pre-filter before LLM scoring
- What we changed:
  - Added deterministic prefilter gates before LLM scoring (`prefilter_min_*`, keyword/skill matches, bounded prefilter text).
- Why:
  - To reduce LLM cost, lower queue pressure, and speed up screening.
- Trade-off:
  - Some strong but non-standard profiles can be filtered out early (false negatives).

2. Smaller/secondary model for selected AI tasks
- What we changed:
  - Used `fallback_model_id` for selected tasks (for example work-summary/welcome-style generation) instead of always using the primary model.
- Why:
  - To control inference cost and reduce latency.
- Trade-off:
  - Lower accuracy/consistency ceiling compared to always using the primary model.

3. Fireflies transcript fallback when extraction is incomplete
- What we changed:
  - Kept the interview pipeline moving when Fireflies transcript payload was incomplete by using fallback/mock summary paths instead of hard-failing.
- Why:
  - To avoid blocking downstream stages (admin review, status progression) while integration was still unstable.
- Trade-off:
  - Transcript fidelity is lower in some edge cases until extraction reliability is hardened.

4. Research payload compaction before persistence/LLM synthesis
- What we changed:
  - Capped and compacted enrichment payloads (hit limits + clipped fields + `max_research_json_chars`) before storage/synthesis.
- Why:
  - To keep token usage bounded and prevent oversized noisy prompts.
- Trade-off:
  - Some long-tail evidence is dropped, which can reduce context depth.

## Known Limitations
- LLM score is not perfectly consistent run-to-run.
- LLM dependencies can introduce hallucination risk when external evidence is noisy.
- Research payloads from LinkedIn/X/GitHub can become large/noisy and lose signal after trimming.
- Some flows still rely on broad scans where incremental/indexed patterns are needed for scale.
- Proper backpressure controls are not fully implemented across all queue + LLM paths.
- A robust LLM circuit-breaker strategy is not fully implemented yet.
- Full load testing/performance characterization has not been completed yet.

## What I Would Improve With More Time
1. AI scoring and evaluation reliability:
- Build a formal evaluation harness for scoring consistency and fairness.
- Add calibration datasets and repeated-run variance checks.
- Add confidence scoring and stronger human-in-the-loop override guidance.

2. Hallucination detection and guardrails:
- Add evidence-grounding checks before persisting AI claims.
- Reject/flag responses that do not map to extracted evidence.
- Add automated contradiction checks between resume and external profiles.

3. Database and scalability:
- Replace broad/full scans with indexed query patterns and incremental processing windows.
- Improve worker partitioning and back-pressure handling for high throughput.
- Add deeper observability on queue lag, retry reasons, and processing latency.

4. Security and code quality hardening:
- Expand security review (input validation, secret handling, webhook hardening, auth boundaries).
- Run deeper static analysis and quality gates with tools like **SonarQube**.
- Add production-style load tests + failure-injection tests for deployment confidence.

5. LLM/Queue resilience:
- Implement proper backpressure strategy (bounded concurrency, queue-depth-aware throttling).
- Add LLM circuit breaker (timeout/error thresholds, open/half-open/closed states, controlled fallback routing).

## LLM Evaluation (Recommended Next Step)
Because this system uses LLMs in screening, research synthesis, offer drafting, and onboarding communication, the next major improvement would be a task-specific LLM evaluation harness. The goal would not be to measure generic model quality, but to measure whether the model is helping the hiring pipeline make better, safer, and more consistent decisions.

### What should be evaluated
1. Screening accuracy
- Compare AI shortlist/reject outcomes against a small human-labeled evaluation set for each role.
- Track precision, recall, and false reject rate, especially for candidates near the shortlist threshold.

2. Score consistency
- Run the same candidate through the scoring flow multiple times and measure score variance.
- Check whether repeated runs cause unstable threshold flips around the shortlist cutoff.

3. Evidence grounding
- Evaluate whether candidate brief claims are supported by resume data or extracted external evidence.
- Flag unsupported or weakly supported claims to reduce hallucination risk in manager-facing summaries.

4. Fairness smoke tests
- Test matched candidate pairs with similar qualifications but different names, schools, or resume phrasing.
- Check whether score differences are driven by job-relevant evidence rather than irrelevant background signals.

### Suggested evaluation dataset
With more time, I would add a small structured dataset containing:
- target role,
- candidate identifier,
- resume summary or parsed candidate payload,
- expected decision (`reject`, `borderline`, `shortlist`),
- expected score band,
- evaluator notes.

This would allow repeated offline evaluation without depending on live submissions.

### Success metrics
The most useful metrics for this system would be:
- shortlist precision,
- shortlist recall,
- false reject rate,
- average score variance across repeated runs,
- grounding pass rate for generated briefs,
- fairness observations from matched-profile tests.

### Why this matters
This would improve performance in three ways:
- better screening quality by catching weak prompt/model behavior early,
- better operational trust by making score and brief outputs more consistent,
- better risk control by identifying hallucination and bias issues before they affect hiring decisions.

### Planned implementation approach
If extended further, I would add:
- a versioned evaluation dataset in the repository,
- a script to run batch scoring and summarization evaluations,
- regression reporting for prompt/model changes,
- confidence-based routing or human-review escalation when evidence quality is weak.

## Documentation Index
- [docs/candidate_career_page_implementation.md](docs/candidate_career_page_implementation.md)
- [docs/phase_02_admin_screening_research_documentation.md](docs/phase_02_admin_screening_research_documentation.md)
- [docs/phase_03_calendar_orchestration.md](docs/phase_03_calendar_orchestration.md)
- [docs/phase_04_live_interview_ai_notetaker.md](docs/phase_04_live_interview_ai_notetaker.md)
- [docs/edge_case_documentation.md](docs/edge_case_documentation.md)
- [docs/assumptions_and_tradeoffs.md](docs/assumptions_and_tradeoffs.md)
