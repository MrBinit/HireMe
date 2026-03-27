# Edge Case Documentation (Top 5)

This document explains the top 5 edge cases handled in the hiring pipeline, with implementation details, current status, and remaining gaps.

## 1) Critical Edge Case: Slot Conflict Prevention (Interview Scheduling)

### Risk
When 3 candidate slots are offered but only 1 can be finalized, if the other 2 are not blocked, multiple candidates can select the same time and cause double booking.

### What we implemented
- On slot generation, we immediately create **real hold events** on the manager's Google Calendar for all offered options (3-5 slots).
- Each offered option stores a `hold_event_id`; confirmation is done against that exact held event.
- During confirmation, we perform an atomic status transition to `interview_confirming` to lock the candidate record and prevent concurrent confirm requests.
- We promote only the selected hold into the final confirmed event (with Google Meet link + attendees).
- We immediately release all unselected holds.
- If candidate does not confirm in time, hold-expiry worker auto-releases expired holds.

### Status
**Implemented and active**.

### Remaining gap / improvement
- Current design is strong for normal operations; extra hardening can add a distributed lock keyed by `(manager_email, slot_start)` for extreme multi-worker race scenarios.

---

## 2) Duplicate Application (Same Email + Same Role)

### Risk
A candidate can apply repeatedly for the same role (manual retries, refreshes, bot submissions), polluting pipeline quality and inflating recruiter workload.

### What we implemented
- Duplicate detection is enforced at persistence level by unique email+job opening protection.
- Email is normalized case-insensitively before duplicate checks.
- Repository raises duplicate error; service converts it into user-facing validation message:
  `duplicate application: this email has already applied to this job opening`.

### Status
**Implemented and active**.

### Remaining gap / improvement
- Does not merge alias emails (`name+tag@domain` vs `name@domain`) or cross-email duplicates for same person.

---

## 3) Invalid Resume Upload (Format and Size)

### Risk
Unrestricted uploads create parsing failures, storage abuse, and security exposure.

### What we implemented
- File type validation at submission time using extension + MIME allow-list.
- Size caps enforced during streaming upload; oversized files are rejected with explicit error.
- Configured limits are per file type (currently 10 MB caps).
- Invalid format returns a clear message (`Invalid resume format...`).

### Status
**Implemented and active**.

### Remaining gap / improvement
- Current validation is metadata-based (extension/MIME). Stronger content-sniffing and malware scanning can be added.
- Product requirement is PDF/DOCX-centric; `.doc` is still enabled for backward compatibility and can be disabled if strict compliance is required.

---

## 4) Role Closed or Paused During Application

### Risk
Candidate may open the form while a role is open, but submit after role is paused/closed. Without runtime validation, this creates invalid applications and poor candidate UX.

### What we implemented
- At submit time, backend re-checks role state:
  - not yet open,
  - paused,
  - closed (past close date).
- Submission is blocked with graceful role-specific messages.
- Public role listing only returns roles currently open and unpaused.

### Status
**Implemented and active**.

### Remaining gap / improvement
- Optional enhancement: show countdown / real-time state revalidation in UI before final submit to reduce failed attempts.

---

## 5) AI Reliability Under Noisy or Excessive External Data (Research + Scoring)

### Risk
Research enrichment from LinkedIn/X/GitHub can produce noisy, ambiguous, or very large payloads. This can increase hallucination risk, token costs, and unstable scoring outcomes. AI scoring can also vary and introduce bias.

### What we implemented
- **Cost-control gate before LLM:** initial keyword/skills prefilter reduces unnecessary LLM calls.
- **Payload limiting:** caps on hits/items plus clipping/compaction before persistence to control token and storage pressure.
- **Prompt constraints:** scoring/research prompts enforce strict JSON and "do not hallucinate" behavior.
- **Low-variance settings:** deterministic/low-temperature inference pattern for synthesis/scoring steps.
- **Decision gate:** threshold-based shortlist logic (`70`) plus admin override path for human correction.
- **Fallback behavior:** deterministic fallback summaries when external extractors are missing or weak.

### Status
**Partially implemented (core controls present, deeper robustness pending)**.

### Known limitations
- Identity resolution remains hard for common names across LinkedIn/X profiles.
- Very high-volume profile data can still drop signal after truncation.
- LLM score variance and fairness/bias risks are reduced but not eliminated.

### Improvement plan
1. Add stronger entity-resolution scoring (email/domain/company/time overlap).
2. Introduce semantic chunking + rank-and-select preprocessing before final LLM prompt.
3. Add evaluation harness for score stability/bias monitoring across candidate cohorts.
4. Add confidence score + "needs human review" flags when evidence quality is low.

---

## Summary Status Snapshot
- Slot conflict prevention: **Implemented**
- Duplicate application guard: **Implemented**
- Resume validation (format/size): **Implemented**
- Closed/paused role handling: **Implemented**
- AI noisy-data + scoring reliability: **Partially implemented; hardening planned**
