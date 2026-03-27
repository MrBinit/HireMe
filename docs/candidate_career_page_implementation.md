# Candidate Career Page Implementation (Simple Write-up)

## What we built
We built a candidate-facing application flow where open roles are managed from the backend and candidates can submit one application with resume upload.

Each job opening is created with a full JD structure:
- role title
- team
- location
- experience level and range
- responsibilities
- requirements
- opening/closing time window
- paused/open/closed runtime status

For delivery, the admin flow supports creating at least 3 distinct openings in the job-opening module and exposing them through public APIs so candidates can apply to active roles.

## Tools and stack used
- Frontend: Next.js + React + TypeScript
- Backend API: FastAPI + Pydantic
- Database: PostgreSQL (candidate applications + job openings)
- Resume storage: AWS S3
- Traffic buffering/background processing: AWS SQS workers
- Auth/security: JWT + password hashing (Passlib pbkdf2/bcrypt)
- Mail automation: SMTP through Gmail (`smtp.gmail.com`)
- Google integration: Google Calendar API for interview scheduling/orchestration flows

## How we built the flow
1. Admin creates job openings using `/api/v1/job-openings` with full JD fields.
2. Candidate page loads available roles from `/api/v1/roles` (and `/api/v1/job-openings` as fallback).
3. Candidate submits form data + resume via multipart `POST /api/v1/applications`.
4. Backend validates role, role status window, file type, and file size.
5. Backend stores:
- candidate/application metadata in PostgreSQL
- resume binary in S3
6. Backend sends confirmation email to candidate.
7. Backend enqueues parse/evaluation/scheduling-related work to SQS so API stays responsive.
8. Admin Hiring Dashboard (Phase 02) reads submissions from `/api/v1/admin/candidates`.

## Security approach
- Admin routes are protected by bearer JWT tokens.
- JWT claims include subject, role, issuer, audience, and expiry.
- Admin password is verified using hashed passwords (`pbkdf2_sha256`/`bcrypt`) via Passlib.
- Plaintext password fallback exists only when explicitly configured.

## How we handled traffic and reliability
To avoid blocking API requests during heavy traffic:
- Candidate submission returns quickly after core persistence.
- Expensive tasks are offloaded to AWS SQS workers (parse/evaluation/research/scheduling queues).
- Queue publish has timeout/error handling and configurable fail/continue behavior.

This keeps candidate submission fast even when worker load is high.

## Automation email flow
- After successful application submission, the system sends a confirmation email automatically.
- Templates are centralized in config and rendered with candidate/role variables.
- Transport is configured using Gmail SMTP credentials from environment variables.

## Edge cases and how we solved them

### 1) Duplicate application (same email + same role)
How solved:
- We enforce uniqueness at the database level for `(job_opening_id, email)`.
- On duplicate insert conflict, repository raises a duplicate error.
- Service returns a clear validation message: candidate already applied for that role with that email.
- Same email can still apply to different roles.

### 2) Invalid file format or oversized uploads
How solved:
- Backend validates extension + MIME type before saving.
- Resume max size is enforced per file type from runtime config.
- Oversized files return a clear error like `max allowed is X MB`.
- Frontend `accept` filter limits selectable resume file types.

### 3) Role closed or paused at submit time
How solved:
- On submit, backend fetches selected role and checks:
- role exists
- current time is within `application_open_at` and `application_close_at`
- role is not paused
- If closed/paused/not-open-yet, submission is rejected with a graceful message.

This prevents stale role submissions even if candidate loaded the page earlier.

## Requirement mapping summary
- At least 3 roles with full JD: supported by job-opening schema + admin creation flow.
- Required listing fields: stored and served in `job_openings`.
- Application form fields: full name, email, LinkedIn, optional portfolio, GitHub, role selection, resume upload.
- Automated confirmation email: sent after successful submission.
- Admin dashboard visibility: submissions are persisted and available via admin candidate APIs.
- Edge cases: duplicate apply, invalid upload/size, and closed/paused role handling are implemented in service + repository validations.
