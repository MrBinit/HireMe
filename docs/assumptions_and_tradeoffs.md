# Assumptions & Trade-offs

This section documents deliberate engineering trade-offs made to ship a working end-to-end system on time.

## 1) Fireflies Integration: Delivery Speed vs Transcript Fidelity

### Decision
We chose **Fireflies API** (free-tier friendly) for notetaker integration and kept the pipeline moving even when transcript extraction was inconsistent.

### Assumption
A partially reliable transcript source is still better than blocking the full interview workflow.

### Trade-off
- We prioritized **faster delivery** of the full flow (booked interview -> transcript sync status -> admin visibility).
- When Fireflies returned incomplete transcript fields, we used fallback/mock summary behavior so downstream steps would not fail.

### Impact
- End-to-end workflow works.
- But transcript URL/summary quality is not always real/complete in every case.

### If more time
- Move to webhook-first transcript ID matching + stronger retries.
- Remove fallback mocks once extraction reliability is stable.

---

## 2) Pre-filter Before LLM: Cost/Latency vs Recall

### Decision
We added a lightweight initial screening gate (keyword/skills/experience matching) before running expensive LLM scoring.

### Assumption
Most clearly unqualified applications can be filtered by deterministic rules without needing LLM reasoning.

### Trade-off
- Reduced LLM calls, cost, and queue pressure.
- Risk of false negatives for candidates with non-standard wording or transferable experience.

### Impact
- Pipeline is faster and cheaper at scale.
- Some borderline candidates may need manual override.

### If more time
- Add semantic retrieval layer before reject decisions.
- Add evaluation set to measure pre-filter false-negative rate.

---

## 3) Parse Only Essential Fields: Throughput vs Depth

### Decision
Resume parsing stores and uses only high-signal fields first (skills, education, work history, years, search text), instead of full deep semantic extraction.

### Assumption
Core hiring decisions in early rounds can be made from condensed, structured resume signals.

### Trade-off
- Faster parse pipeline and smaller downstream payloads.
- Less nuance captured (context, quality of achievements, domain-specific depth).

### Impact
- Good speed and stable operations for screening.
- Rich profile interpretation may be underpowered for edge profiles.

### If more time
- Add staged parsing (fast pass + deep pass only for shortlisted candidates).

---

## 4) Research Data Compaction: Token Control vs Evidence Completeness

### Decision
For LinkedIn/X/GitHub/portfolio enrichment, we cap hits and aggressively clip/compact text before persistence and LLM synthesis.

### Assumption
Top-ranked evidence is usually sufficient for hiring brief generation.

### Trade-off
- Lower token usage and lower hallucination pressure from huge noisy payloads.
- Potential loss of useful long-tail evidence.

### Impact
- Better runtime and lower LLM spend.
- Some candidate signals can be dropped, affecting confidence and consistency.

### If more time
- Add chunking + ranking + confidence scoring instead of hard clipping.

---

## 5) Secondary Model for Short-Form Generation: Cost vs Message Quality Ceiling

### Decision
For Slack onboarding welcome messages, we use the **secondary model** (`fallback_model_id`) rather than the primary model.

### Assumption
Short, structured onboarding text does not require the strongest model.

### Trade-off
- Significant cost reduction and lower latency.
- Slightly lower quality ceiling for style/nuance in personalized tone.

### Impact
- Requirement is met (AI-personalized, profile-aware message) with efficient inference.

### If more time
- Add quality routing (auto-upgrade to primary model only for low-confidence outputs).

---

## 6) API Fast-ACK Background Tasks: Response Latency vs Durability

### Decision
We shifted heavy webhook work (Slack team-join onboarding and Fireflies transcript post-processing) and application confirmation email off the immediate request path and onto durable SQS jobs.

### Assumption
Fast ACK + queue handoff should be the default contract for webhook reliability and operational safety.

### Trade-off
- Better request reliability and lower timeout risk during external API calls.
- Added queue/worker complexity and a requirement to run an additional worker process.

### Impact
- API endpoints respond faster and avoid blocking on long network workflows.
- Side effects survive API restarts because processing is decoupled into queue workers.

### Implemented hardening
- Deferred webhook/email side effects now run via queue-backed worker (`sqs_webhook_event_worker`).
- Added persisted idempotency keys/state (`processed_webhook_events`) for replay-safe processing.
- Added queue-depth warning/reject thresholds for webhook enqueue backpressure.

### Remaining infra hardening
- Enable DLQ + replay runbook in deployment infrastructure.

---

## Overall Rationale
The core strategy was: **ship reliable workflow first, then harden precision and quality**. We optimized for operational continuity, cost control, and reviewability, while documenting the areas that need deeper robustness work.
