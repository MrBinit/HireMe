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
   - sends shortlisted candidate an email with numbered options,
   - stores structured scheduling payload on candidate row.

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
- `events.delete` for best-effort cleanup on downstream failures.

### Key implementation files
- Calendar client: `app/infra/google_calendar_client.py`
- Scheduling service: `app/services/interview_scheduling_service.py`
- Scheduling queue contract: `app/services/scheduling_queue.py`
- Scheduling worker: `app/scripts/sqs_scheduling_worker.py`
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
- `interview_schedule_status` (`queued`, `in_progress`, `interview_options_sent`, `failed`)
  - legacy values `interview_email_sent` and `options_sent` are still recognized for backward compatibility
- `interview_schedule_options` (JSON payload with held options/event ids)
- `interview_schedule_sent_at`
- `interview_hold_expires_at`
- `interview_calendar_email`
- `interview_schedule_error`

## API Endpoint
- Manual enqueue (admin):
  - `POST /api/v1/admin/candidates/{application_id}/schedule`

## Required Config
### `.env`
- `SQS_SCHEDULING_QUEUE_URL`
- `GOOGLE_SERVICE_ACCOUNT_FILE` or `GOOGLE_SERVICE_ACCOUNT_JSON`
  - OR OAuth set: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`
- Existing SMTP creds for candidate email:
  - `SMTP_USERNAME`
  - `SMTP_PASSWORD`

### YAML
- Scheduling runtime:
  - `app/config/scheduling_config.yaml`
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

### Queue bootstrap helper
```bash
./setup_scheduling_queue.sh
```

## Current Limitations
- Assumes service-account delegation is correctly configured in Google Workspace.
- Calendar race windows are reduced by pre-create recheck and immediate holds, but strict cross-worker distributed locking is not yet implemented.
- Hold expiry cleanup is not yet a scheduled janitor process; expiry is stored and enforced procedurally for next phases.

## Next Hardening Steps
- Add hold-expiry cleanup worker to release stale holds automatically.
- Add distributed reservation lock keyed by `(manager_email, slot_start)` for stronger multi-worker collision guarantees.
- Add candidate selection endpoint for choosing one held option and converting hold to confirmed interview event.
