# E-Signature Workflow (5B)

## Chosen Approach

Option A: Third-Party API (DocuSign).

### Why

- DocuSign provides a legally-auditable signature workflow.
- No need to build/secure custom signature capture infrastructure.
- Built-in envelope lifecycle events make webhook-driven status updates reliable.

## End-to-End Flow

1. Manager selects candidate after interview is done.
2. Offer letter is generated and converted to PDF.
3. PDF is stored in S3 and candidate moves to `offer_letter_created`.
4. Manager approves offer letter.
5. Backend sends PDF to DocuSign as an envelope for candidate signature.
6. Candidate receives DocuSign email and signs externally.
7. DocuSign webhook callback notifies backend.
8. Backend marks offer as signed (`offer_letter_sign`) and immediately sends manager alert email.
9. Backend triggers Slack invite flow for the signed candidate.
10. On first Slack join (`team_join` event), backend sends AI-personalized welcome DM and HR channel alert.

## API Touchpoints

### 1) Manager decision (select/reject)

- `PATCH /api/v1/admin/candidates/{application_id}/manager-decision`
- On `select`, offer PDF is prepared and stored.

### 2) Manager approval and send for e-sign

- `POST /api/v1/admin/candidates/{application_id}/offer-letter/approve`
- If DocuSign is enabled, status becomes:
  - `applicant_status = offer_letter_sent`
  - `offer_letter_status = sent_for_signature`
  - `docusign_envelope_id` saved

### 3) DocuSign webhook callback

- `POST /api/v1/integrations/docusign/webhook?application_id=<uuid>&token=<shared_secret>`
- Verifies webhook token.
- Parses DocuSign event payload (JSON/XML).
- On `completed`:
  - `offer_letter_status = signed`
  - `offer_letter_signed_at` set
  - `applicant_status = offer_letter_sign`
  - immediate manager alert email is sent
  - Slack invite is triggered
- On `declined` / `voided`:
  - status updated accordingly
  - error note stored

### 4) Slack events callback

- `POST /api/v1/integrations/slack/events`
- Validates Slack request signature (`X-Slack-Signature`, `X-Slack-Request-Timestamp`).
- Handles Slack URL verification challenge.
- Handles `event_callback` for `team_join`.
- On first join:
  - candidate is matched by Slack profile email
  - AI welcome message is generated from candidate profile + manager info + onboarding links
  - Slack DM is sent to candidate
  - HR channel notification confirms onboarding completion
  - Slack onboarding fields are persisted in candidate record

## Signed Alert Requirement

Implemented via immediate notifications:

- The moment webhook status is `completed`, backend sends alert to hiring manager.
- Candidate dashboard/admin view reflects signed status (`signed`, `offer_letter_sign`) and Slack onboarding state.

## Runtime Configuration

DocuSign settings live in `app/config/application_config.yaml` under `docusign`.

Secrets live in `.env`:

- `DOCUSIGN_INTEGRATION_KEY`
- `DOCUSIGN_USER_ID`
- `DOCUSIGN_PRIVATE_KEY_PATH` (or `DOCUSIGN_PRIVATE_KEY`)
- `DOCUSIGN_WEBHOOK_SECRET`

JWT OAuth host (demo): `https://account-d.docusign.com`

## Slack Setup (Required for Onboarding)

1. In Slack App settings, enable Events API and set:
   - Request URL: `/api/v1/integrations/slack/events` on your backend domain.
   - Subscribe to bot event: `team_join`.
2. Install app to workspace and configure scopes:
   - `chat:write`
   - `users:read`
   - `users:read.email`
   - `admin.users:write` (preferred for invite) or workspace-admin equivalent.
3. Configure environment variables:
   - `SLACK_BOT_TOKEN`
   - `SLACK_ADMIN_USER_TOKEN` (optional but recommended for invite)
   - `SLACK_SIGNING_SECRET`
   - Optional rotation: `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_BOT_REFRESH_TOKEN`, `SLACK_ADMIN_REFRESH_TOKEN`
