# Phase 03: Calendar Orchestration (3A)

## Goal
Implement real interviewer-calendar orchestration so shortlisted candidates receive interview options without manual back-and-forth, while avoiding slot collisions in the pipeline.

## Implemented Flow
1. Candidate is parsed and evaluated.
2. If AI score passes threshold, status becomes `shortlisted`.
3. Evaluation worker auto-enqueues interview scheduling job (`SQS_SCHEDULING_QUEUE_URL`).
4. Scheduling worker:
   - reads manager email from job opening (`manager_email`),
   - queries Google Calendar free/busy for next 5 business days,
   - finds 3-5 free **45-minute** slots,
   - immediately creates opaque hold events for those slots,
   - sends shortlisted candidate an email with numbered options and one-click signed links,
   - stores structured scheduling payload on candidate row.
5. Candidate clicks confirmation link and lands on frontend `/interview/confirm?token=...`.
6. Frontend calls backend token endpoint; backend validates token, expiry, and current hold state.
7. Backend acquires scheduling lock (status transition to `interview_confirming`) to avoid double booking.
8. System confirms selected hold event on manager calendar (with candidate attendee + Google Meet creation), sends invite updates, and releases other holds.
9. Expired unconfirmed holds are auto-released by expiry worker.

## Critical Edge Case: Slot Conflict Prevention
When 3 slots are offered and only 1 can be finalized, the other 2 remain blocked as holds until selection or expiry. The implementation prevents same-slot double booking as follows:

1. **Immediate hold creation**: we create opaque Google Calendar hold events (`events.insert`) for all offered options before candidate selection.
2. **Hold IDs persisted**: each option stores `hold_event_id` in `interview_schedule_options`; confirmation operates on these exact hold IDs.
3. **Atomic confirm lock**: confirmation first performs an atomic DB transition to `interview_confirming` via `transition_interview_schedule_status(...)`, so only one confirmation request can win for a candidate.
4. **Single-event promotion**: the selected hold is patched into confirmed interview; system does not create a second event for the same option.
5. **Release non-selected holds**: after successful confirm, unselected hold events are deleted immediately.
6. **Expiry cleanup**: if candidate does not choose, hold-expiry worker releases all held options at `interview_hold_expires_at`.
7. **Cross-candidate protection**: because holds are placed directly on manager calendar, subsequent free/busy checks see those times as busy and avoid offering them again.

Operational note: this design removes the practical conflict scenario in normal queue flow. For extreme simultaneous scheduling races across multiple workers, a distributed lock keyed by `(manager_email, slot_start)` is an additional hardening option.

## DB Updates After Scheduling
Yes, DB is updated immediately during scheduling lifecycle. Key fields:

- `interview_schedule_status`
  - `in_progress` (worker started)
  - `interview_options_sent` (holds created + options emailed)
  - `interview_confirming` (lock during confirm)
  - `interview_booked` (selected slot finalized)
  - `interview_expired` (holds auto-released on expiry)
  - `failed` (worker error)
  - reschedule states: `interview_reschedule_requested`, `interview_reschedule_options_sent`, `interview_reschedule_confirming`
- `interview_schedule_options` (selected option number, event ids, links, released holds, reschedule metadata)
- `interview_schedule_sent_at`
- `interview_hold_expires_at`
- `interview_calendar_email`
- `interview_schedule_error`
- `applicant_status` moves to `in_interview` on successful booking (`move_candidate_to_in_interview_on_booking=true`).

## Calendar Invite Acceptance Handling (Google YES/NO)
- System does **not** wait for an email reply to finalize scheduling.
- Scheduling is finalized at confirmation API time (token click / confirm endpoint), then calendar invite is sent.
- Candidate can accept invite from Google Calendar (`Yes`) without replying to email; booking is already finalized in DB (`interview_booked`).
- Current implementation does not yet persist attendee `responseStatus` (`accepted`/`declined`) back into DB from Google Calendar events.
- Recommended improvement: add calendar response sync (polling or `events.watch`) and store normalized attendee response status on applicant row.

## Queue + Async Design
- Queue-backed by default (`scheduling.use_queue=true`) to protect API latency and absorb spikes.
- Candidate submission path remains non-blocking.
- Scheduling runs in dedicated worker process:
  - `venv/bin/python -m app.scripts.sqs_scheduling_worker`
- Auto-enqueue is controlled by:
  - `scheduling.enabled`
  - `scheduling.auto_enqueue_after_shortlist`
  - `scheduling.use_queue`

## Google Calendar Integration
### Auth strategy
- Uses Google service account credentials from:
  - `GOOGLE_SERVICE_ACCOUNT_FILE`, or
  - `GOOGLE_SERVICE_ACCOUNT_JSON`
- OAuth fallback is supported with:
  - `GOOGLE_CLIENT_ID`
  - `GOOGLE_CLIENT_SECRET`
  - `GOOGLE_REFRESH_TOKEN`
- For manager calendars, service uses delegated subject = `manager_email` from job opening.

### API calls
- `freebusy.query` to retrieve busy windows.
- `events.insert` to create hold events (opaque blocks).
- `events.patch` to confirm chosen hold, add candidate attendee, and request Google Meet (`conferenceData`).
- `events.delete` for best-effort cleanup on downstream failures.

### Key implementation files
- Calendar client: `app/infra/google_calendar_client.py`
- Scheduling service: `app/services/interview_scheduling_service.py`
- Scheduling queue contract: `app/services/scheduling_queue.py`
- Scheduling worker: `app/scripts/sqs_scheduling_worker.py`
- Hold-expiry worker: `app/scripts/interview_hold_expiry_worker.py`
- Auto-enqueue from evaluation: `app/scripts/sqs_evaluation_worker.py`

## Slot Selection Strategy
- Window: next `business_days_ahead=5` business days.
- Duration: `slot_duration_minutes=45`.
- Step: `slot_step_minutes=30` (configurable).
- Working hours: `business_hours_start_hour` to `business_hours_end_hour`.
- Minimum lead time: `min_notice_hours`.
- Returns first available 3-5 slots and creates hold events immediately.

## Persisted Candidate Fields
Stored in `applicant_applications`:
- `interview_schedule_status` (`queued`, `in_progress`, `interview_options_sent`, `interview_booked`, `interview_expired`, `failed`)
  - legacy values `interview_email_sent` and `options_sent` are still recognized for backward compatibility
- `interview_schedule_options` (JSON payload with held options/event ids)
- `interview_schedule_sent_at`
- `interview_hold_expires_at`
- `interview_calendar_email`
- `interview_schedule_error`

## API Endpoint
- Manual enqueue (admin):
  - `POST /api/v1/admin/candidates/{application_id}/schedule`
- Candidate slot confirm (public, guarded by application_id + email):
  - `POST /api/v1/applications/{application_id}/interview/confirm`
- Candidate slot confirm (one-click signed token from email):
  - `POST /api/v1/applications/interview/confirm-token`

## Required Config
### `.env`
- `SQS_SCHEDULING_QUEUE_URL`
- `INTERVIEW_CONFIRMATION_TOKEN_SECRET` (optional; falls back to `ADMIN_JWT_SECRET`)
- `GOOGLE_SERVICE_ACCOUNT_FILE` or `GOOGLE_SERVICE_ACCOUNT_JSON`
  - OR OAuth set: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`
- Existing SMTP creds for candidate email:
  - `SMTP_USERNAME`
  - `SMTP_PASSWORD`

### YAML
- Scheduling runtime:
  - `app/config/scheduling_config.yaml`
  - includes `candidate_confirmation_page_url` and 24h expiry/release knobs
- Email templates:
  - `app/config/notification_config.yaml`

## Operational Commands
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
```bash
venv/bin/python -m app.scripts.sqs_scheduling_worker
```
```bash
venv/bin/python -m app.scripts.interview_hold_expiry_worker
```

### Queue bootstrap helper
```bash
./setup_scheduling_queue.sh
```

## Current Limitations
- Assumes service-account delegation is correctly configured in Google Workspace.
- Calendar race windows are reduced by immediate holds + confirmation lock, but strict cross-worker distributed slot locking is not yet implemented.
- Candidate self-service endpoint currently validates by `application_id + email`; signed one-time confirmation links would be stronger.

## Next Hardening Steps
- Add signed, expiring confirmation tokens in email links instead of plain `application_id + email`.
- Add distributed reservation lock keyed by `(manager_email, slot_start)` for stronger multi-worker collision guarantees.
- Add explicit reschedule endpoint that re-opens slot generation and revokes previous confirmed event.
