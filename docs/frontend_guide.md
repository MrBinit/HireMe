# Frontend Guide

## Purpose
This document explains the frontend stack, page structure, and API integration for the HireMe admin and candidate flows.

## 1) Stack
From `frontend/package.json`:
- Next.js `14.2.15`
- React `18.3.1`
- TypeScript `5.7.2`
- ESLint + `eslint-config-next`

## 2) App Structure

## Admin login page
- Route: `/admin`
- Purpose:
  - Admin credential login
  - Receives JWT token from backend
  - Stores token in browser localStorage for session use

## Admin dashboard page
- Route: `/admin/dashboard`
- Main features:
  - Create job opening
  - Pause/resume opening
  - Delete opening
  - Summary cards:
    - Job Positions
    - Open Jobs
    - Applicants
  - Candidate table columns:
    - Name (clickable to profile)
    - Position
    - Status
    - Evaluation Status
    - Email
    - Submission Date
    - AI Score
    - Resume Download
  - Filters:
    - Role
    - Status
    - Date range

## Candidate detail page
- Route: `/admin/candidates/[id]`
- Main features:
  - Candidate profile and contact details
  - Resume metadata + secure resume download action
  - AI scoring section:
    - evaluation status
    - AI score
    - AI screening summary
    - online research summary
  - Parsed profile sections:
    - Skills
    - Education
    - Work experience
    - Prior offices/roles
    - Key responsibilities/achievements
  - Status history timeline
  - Human screening decision controls for manual-review candidates:
    - Move Ahead (sets `shortlisted`)
    - Don't Move Ahead (sets `rejected`)
    - Optional decision note
  - Manual review/override form for admin updates

## 3) Backend API Endpoints Used by Frontend
- `POST /api/v1/admin/login`
- `GET /api/v1/job-openings`
- `POST /api/v1/job-openings`
- `PATCH /api/v1/job-openings/{id}/pause`
- `DELETE /api/v1/job-openings/{id}`
- `GET /api/v1/admin/candidates`
- `GET /api/v1/admin/candidates/{id}`
- `GET /api/v1/admin/candidates/{id}/resume-download`
- `PATCH /api/v1/admin/candidates/{id}/status`
- `PATCH /api/v1/admin/candidates/{id}/review`
- `POST /api/v1/admin/candidates/{id}/evaluate`
- `POST /api/v1/admin/candidates/{id}/evaluate/queue` (alias)
- `POST /api/v1/admin/candidates/{id}/research`
- `POST /api/v1/admin/candidates/{id}/research/queue` (alias)
- `POST /api/v1/admin/candidates/{id}/schedule`

## 4) Frontend Security/Operational Notes
- Admin APIs are called with `Authorization: Bearer <token>`.
- Resume downloads use backend-generated short-lived pre-signed S3 URLs.
- Candidate evaluation is async/queue-backed; UI should poll/read `evaluation_status`.
- Interview scheduling is async/queue-backed; UI can poll/read `interview_schedule_status`.
- Candidate detail view parses `online_research_summary` JSON and shows:
  - 3-5 sentence brief
  - strengths/risks
  - cross-check flags and issue flags
- Frontend keeps UI/session state only; source of truth is backend.
- Token persistence is currently localStorage-based for this assessment build.

## 5) Environment
- Frontend API base URL uses:
  - `NEXT_PUBLIC_API_BASE_URL`
- Default fallback in code:
  - `http://127.0.0.1:8000`

## 6) Run and Verify
From project root:

```bash
cd frontend
npm install
npm run lint
npm run build
npm run dev
```

Then open:
- `http://localhost:3000/admin`
