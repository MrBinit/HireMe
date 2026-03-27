# Edge Case Documentation (Top 5)

This document captures the highest-impact edge cases in the hiring pipeline using a production-oriented format: severity, impact, prevention, detection, recovery, and next hardening steps.

## 1) Critical: Slot Conflict Prevention (Interview Scheduling)

Severity: **HIGH**
Impact: **Double booking, interviewer conflict, trust loss in scheduling reliability**

### What we implemented
- Create real hold events for all offered slots (3-5) before candidate decision.
- Persist `hold_event_id` for each option and confirm using that exact hold.
- Use atomic state transition to `interview_confirming` before finalization.
- Promote only the selected hold to confirmed interview.
- Release all unselected holds immediately after confirmation.
- Expiry worker auto-releases stale holds.

### Detection and monitoring
- Metric: `scheduling_double_booking_incidents`
- Metric: `interview_confirm_lock_contention`
- Log/trace: confirm attempts by `application_id`, `manager_email`, `hold_event_id`

### Recovery / fallback
- If confirmation fails after lock, reset stale `interview_confirming` state and retry safely.
- If stale holds exist, expiry worker reconciles and releases them.

### Scalability note
- Current lock is candidate-level atomic transition.
- For multi-worker hardening, add distributed lock keyed by `(manager_email, slot_start)`.

Status: **Implemented; additional distributed-lock hardening planned**

---

## 2) Duplicate Application (Same Email + Same Role)

Severity: **MEDIUM**
Impact: **Data quality degradation, recruiter time waste, repeated processing costs**

### What we implemented
- Enforce uniqueness at persistence layer (email + job opening).
- Normalize email case for duplicate checks.
- Return deterministic user-facing duplicate message.

### Detection and monitoring
- Metric: `duplicate_application_rate`
- Metric: `duplicate_application_by_role`
- Log: duplicate attempt metadata (`email`, `job_opening_id`, timestamp)

### Recovery / fallback
- Reject duplicate insert cleanly without corrupting existing record.
- Optional next step: allow update/resubmission flow instead of hard reject.

Status: **Implemented**

---

## 3) Invalid Resume Upload (Format / Oversize)

Severity: **MEDIUM**
Impact: **Parse failures, storage abuse, poor candidate UX**

### What we implemented
- Validate extension + MIME against allow-list.
- Enforce streaming size cap per file type.
- Return explicit validation errors for unsupported or oversized files.

### Detection and monitoring
- Metric: `resume_upload_rejection_rate`
- Metric: `oversized_resume_rejections`
- Log: rejected upload reason (`format`, `mime`, `size`)

### Recovery / fallback
- Candidate receives actionable error and can re-upload valid file.
- Upload path fails fast before downstream parse/queue processing.

### Scalability note
- Current metadata validation is fast and inexpensive.
- Next hardening: content sniffing + malware scan pipeline.

Status: **Implemented**

---

## 4) Role Closed or Paused During Submission

Severity: **MEDIUM**
Impact: **Invalid applications, candidate frustration, policy inconsistency**

### What we implemented
- Re-check role state at submit time (`not_open`, `paused`, `closed`).
- Block submission with role-specific graceful message.
- Only show active/unpaused roles in available roles response.

### Detection and monitoring
- Metric: `apply_blocked_paused_role`
- Metric: `apply_blocked_closed_role`
- Metric: `apply_blocked_not_open_yet`

### Recovery / fallback
- Candidate gets immediate reason and can re-apply when role reopens.
- Prevents stale client-side role states from creating invalid backend data.

Status: **Implemented**

---

## 5) AI Reliability Under Noisy Data (Research + Scoring)

Severity: **HIGH**
Impact: **Hallucination risk, unstable scoring, bias risk, shortlist quality drift**

### What we implemented
- Prefilter gate before expensive scoring to reduce unnecessary LLM calls.
- Payload compaction and clipping to constrain token size/noise.
- Strict prompt constraints and JSON-shaped outputs.
- Threshold gate (`70`) + admin manual override path.
- Deterministic fallback outputs when extractors or model outputs are weak.

### Detection and monitoring
- Metric: `score_variance_std` (repeat-run stability)
- Metric: `hallucination_flag_rate` (unsupported claims)
- Metric: `avg_tokens_per_candidate`
- Metric: `manual_override_rate_after_ai_shortlist`

### Recovery / fallback
- If enrichment extraction fails, use deterministic fallback brief/issue flags.
- If evidence quality is low, route candidate to manual review path.

### Scalability note
- Current truncation keeps cost controlled but may drop long-tail evidence.
- Next step: chunk-rank-ground pipeline instead of hard clipping.

### AI hardening plan
1. Add model confidence score per recommendation.
2. Enforce evidence grounding (each claim linked to extracted evidence chunks).
3. Add offline evaluation loop with human feedback and bias/stability checks.

Status: **Partially implemented; reliability and evaluation hardening pending**

---

## Summary
- Slot conflict prevention: **Implemented (high-confidence core controls)**
- Duplicate protection: **Implemented**
- Resume upload validation: **Implemented**
- Closed/paused role handling: **Implemented**
- AI reliability controls: **Partially implemented, with explicit monitoring and hardening plan**
