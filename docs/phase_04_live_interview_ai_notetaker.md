# Phase 04 - Live Interview & AI Notetaker Integration

## What We Built
We implemented an interview notetaker pipeline that attempts to capture live interview transcripts and summaries after interview booking.

When an interview is confirmed in Phase 03, the system prepares transcript tracking for that candidate. After the meeting, a worker tries to fetch transcript details and save them to the candidate profile.

## Notetaker Decision and Reasoning
We evaluated these options for transcript integration:
- Read.ai
- Fathom
- Fireflies.ai
- Otter.ai

We chose **Fireflies.ai** for this project because:
- it exposed API access that was workable for our backend integration,
- it supported transcript retrieval and summary fields,
- and it had a free usage path suitable for implementation/testing.

## Tools Used
- Fireflies GraphQL API
- Fireflies live meeting capture trigger (`addToLiveMeeting`)
- Backend polling worker for transcript sync
- Candidate profile DB fields for transcript URL + summary

## How the Flow Works
1. Interview gets confirmed from scheduling flow.
2. System stores Fireflies tracking metadata in candidate interview payload.
3. Worker attempts to request live capture for the meeting link.
4. After meeting, worker polls Fireflies transcript APIs.
5. System tries to match the correct transcript using meeting link, manager email, candidate email, and interview time.
6. If transcript is found, transcript URL and summary are written to candidate record.

## What Is Currently Working
- Fireflies integration is wired end to end.
- Transcript sync worker runs and updates interview transcript status.
- Candidate record has fields for:
  - `interview_transcript_status`
  - `interview_transcript_url`
  - `interview_transcript_summary`
  - `interview_transcript_synced_at`
- These fields are available for Admin candidate review.

## Current Issue (Important)
For current submission, transcript extraction is not fully reliable from Fireflies response in all cases. Because of this issue:
- transcript URL and summary are currently using mock/fallback values in the pipeline,
- actual rich transcript content is not always extracted successfully.

So the architecture is complete, but transcript quality/output is partially mocked due to API extraction mismatch issues.

## Why Mock Was Used
Mock transcript URL and summary were used so Phase 04 flow could continue and be demonstrated end to end:
- scheduling -> interview booked -> transcript sync status -> candidate profile update.

Without this fallback, the pipeline would stop whenever Fireflies returned incomplete/empty transcript fields for a meeting.

## Limitations
- Transcript matching can fail when Fireflies data is delayed or incomplete.
- Some meetings return empty summary/transcript URL fields, causing fallback behavior.
- Current implementation needs deeper reliability tuning for production-level transcript extraction.

## How We Would Improve With More Time
1. Add deeper transcript-ID-first matching via webhook + transcript-by-id prioritization.
2. Improve retry strategy and backoff windows for delayed transcript availability.
3. Add richer parser normalization for Fireflies summary variants.
4. Add better error observability (separate failure reasons in admin UI).
5. Replace fallback mocks with strict real transcript-only completion rules.

## Final Note
If more time is available, the integration direction remains **Fireflies API**. The next step is to harden extraction/matching so transcript URL and summary always come from real meeting output instead of fallback mock data.

## 5A - AI Offer Letter Generation

### Manager Input Collection (Implemented)
Before generating the offer letter, manager decision intake captures:
- confirmed job title and start date
- base salary and compensation structure
- equity/bonus (optional)
- reporting manager
- custom terms/conditions (optional)

This is provided via manager selection details in the admin decision flow.

### Generation Flow (Implemented)
1. Manager marks candidate as `select` after interview completion.
2. Backend validates required manager selection fields.
3. Offer-letter generator builds prompt input using:
   - `MANAGER_INPUT` JSON (manager-provided terms),
   - `CANDIDATE_PROFILE` JSON (candidate profile, score, parse/research context).
4. Prompt rules enforce: use only provided facts, no invented clauses, plain-text professional format.
5. LLM returns a draft letter text.
6. Draft is stored and converted to PDF, then persisted in storage with `offer_letter_created` status.
7. Human review happens before send; manager approval endpoint is required to dispatch via e-sign/email flow.

### Model/Prompt Notes
- Uses Bedrock offer-letter prompt template (`offer_letter_prompt_template`).
- Uses secondary model (`fallback_model_id`) with deterministic settings for cost control.
- If LLM generator is unavailable, backend can fall back to deterministic template rendering.

### Why this satisfies 5A
- Manager questions are explicitly captured.
- AI drafts a complete offer letter from manager input + candidate profile.
- Output is routed through human approval before being sent.

## 5B - E-Signature Workflow

### Chosen Approach
We implemented **Option A (Third-Party API)** using **DocuSign**.

### Why We Chose DocuSign
- Faster and safer than building a custom signing UI.
- Provides a standard legally auditable signature process.
- Gives envelope status lifecycle events (`sent`, `delivered`, `completed`, `declined`, `voided`).
- Reduced implementation risk for signature validity and document tracking.

### API Flow (Implemented)
1. Manager finalizes selection and offer letter PDF is generated/stored.
2. Manager approves sending offer letter.
3. Backend sends the PDF to DocuSign as an envelope for candidate signature.
4. Candidate receives DocuSign email and signs there.
5. DocuSign calls our webhook callback.
6. Backend validates webhook token, parses payload, and updates candidate status.
7. On `completed`, system marks offer as signed and triggers immediate alert.

### Webhook / Callback Handling
- Callback endpoint: `POST /api/v1/integrations/docusign/webhook`
- Webhook contains `application_id` and token in callback URL query.
- Payload supports JSON/XML parsing and status normalization.
- Signed state updates:
  - `offer_letter_status = signed`
  - `offer_letter_signed_at = <timestamp>`
  - `applicant_status = offer_letter_sign`

### Signed Alert Requirement (Implemented)
The moment signature completion is received (`completed`), the system sends an immediate manager alert email and updates admin-visible candidate state.

### Additional Reliability in Our Implementation
- If webhook is delayed/missed, admin can trigger signature status sync from DocuSign API (`offer-letter/sync-signature`).
- If DocuSign is not enabled/configured, the system falls back to normal offer-letter email delivery (without e-sign completion lifecycle).

### Why We Did Not Choose Option B (Custom Signing UI)
Option B requires signature capture + timestamp + IP capture + signature storage/compliance hardening. For this phase timeline, third-party DocuSign integration was the more reliable path.

### Limitations and Next Improvements
- Current webhook protection uses shared token; next hardening can include stronger signature verification.
- Improve admin troubleshooting UI for envelope failures (`declined`/`voided`) and retry guidance.

## Phase 06 - Slack Onboarding Trigger

### What We Built
After offer signature is completed, the system automatically starts Slack onboarding. This closes the loop from candidate to active team member.

### Implemented Flow
1. Candidate signs offer letter (DocuSign status `completed`).
2. Backend updates signed status and immediately triggers Slack invite flow.
3. Candidate is invited to Slack workspace using Slack API.
4. On first Slack join event (`team_join`), backend matches candidate by email.
5. AI generates a personalized welcome message from candidate profile data.
6. System sends DM welcome message to candidate.
7. System posts an onboarding confirmation message to HR Slack channel.
8. System stores onboarding state in candidate record (`slack_invite_status`, `slack_joined_at`, `slack_welcome_sent_at`, `slack_onboarding_status`).

### AI Personalization Approach
- The welcome message is AI-generated (not static template text).
- It is generated from candidate profile context: name, role, start date, manager greeting, and onboarding links.
- To optimize cost, we used the **secondary model** (`fallback_model_id`) for this welcome-message generation, not the primary model.

### Why Secondary Model Was Used
- Welcome DM generation is short-form and structured.
- Using the smaller/secondary model significantly reduced LLM cost while keeping acceptable quality for onboarding communication.

### Enterprise Limitation and Practical Handling
We did not have full Slack enterprise-grade admin capability during implementation. Because of that, some invite operations can fail with token-type restrictions (for example `not_allowed_token_type` on admin invite APIs).

To keep onboarding working despite this:
- We implemented normal Slack API invite path first (`admin.users.invite`, then legacy fallback).
- If invite API cannot be used due token/admin limitations, system falls back to sending a Slack join-link email to candidate.
- Admin fields are updated with actionable status (`action_required` / `failed`) for follow-up.

### Requirement Coverage
- Offer-signature trigger for onboarding: implemented.
- Slack invitation to candidate: implemented (with fallback when admin API is restricted).
- AI-personalized Slackbot-style welcome message on first join: implemented.
- HR internal notification on successful join/onboarding: implemented via HR channel post.

### Current Limitations
- Some Slack invite behaviors depend on workspace/admin token permissions that are not always available in non-enterprise setups.
- In those cases, fallback email invite is used to keep onboarding unblocked.
