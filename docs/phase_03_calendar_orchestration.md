# Phase 03 - Calendar Orchestration & Scheduling

## What We Built
We built an automated interview scheduling flow that works from real calendar data, offers multiple time options to candidates, and prevents double booking.

The process starts after a candidate is shortlisted. A scheduling job is pushed to AWS SQS and handled by a background worker.

## Tools and Services Used
- Google Calendar API (free/busy lookup, hold events, final event confirmation)
- Google Meet generation through Google Calendar `conferenceData`
- AWS SQS queue + worker for asynchronous scheduling
- Email notification service for slot options, reminders, and updates
- Database state machine to track scheduling status and prevent race conditions

## How Scheduling Works (Monday to Friday)
1. We read the assigned manager's calendar.
2. We search availability in the next 5 business days (Monday to Friday).
3. We generate 45-minute interview slots during configured business hours.
4. We pick 3-5 valid slots (minimum 3).
5. Before sending anything to the candidate, we create tentative hold events on the manager calendar for those slots.
6. We email the candidate the held options.

This means the candidate sees real available times, and those times are already protected.

## Candidate Selection Flow
1. Candidate selects one offered slot.
2. We lock the application in `interview_confirming` so two requests cannot confirm at the same time.
3. We convert the selected hold into the final interview event.
4. We add candidate + manager attendees.
5. We generate the Google Meet link.
6. We send calendar updates/invites.
7. We release the remaining held slots.

Once the interview time is confirmed, the system sends a proper calendar invite. The candidate can accept directly from Google Calendar (the `Yes` button). The system does not wait for an email reply to treat the interview as scheduled, so it will not stay stuck waiting indefinitely.

## If Candidate Wants a Different Time
If the candidate is not comfortable with offered slots:
1. Candidate requests reschedule.
2. System finds alternative slots.
3. Alternatives are sent to the manager for approval (accept/reject links).
4. If manager approves, we confirm that slot and send updated invite to candidate.
5. If manager rejects, system finds next best alternatives and asks again.

This loop continues until a final time is fixed (or max reschedule rounds is reached).

## Follow-Up and Hold Expiry
- If candidate does not respond, the system sends an automated follow-up reminder.
- Holds expire automatically and are released by the expiry worker.
- Requirement target is 48-hour nudges; current reminder timing is configurable (currently set to 24h in config, with 48h hold expiry).

## Critical Edge Case: Slot Conflict Prevention
This is the core protection against double booking:
1. Offered slots are immediately blocked as hold events.
2. Hold event IDs are stored and used for confirmation.
3. Confirmation uses an atomic status transition (`interview_confirming`) as a lock.
4. Only one slot is promoted to final confirmed interview.
5. Unselected holds remain blocked until final selection or expiry, then are released.
6. Because holds are real calendar events, other scheduling attempts see those times as busy and cannot offer them.

## Admin Visibility
Admin can track interview scheduling lifecycle through statuses such as:
- `interview_options_sent`
- `interview_confirming`
- `interview_booked`
- `interview_reschedule_requested`
- `interview_reschedule_options_sent`
- `interview_expired`

This gives clear visibility into who is pending, confirmed, or needs re-scheduling action.
