# HireMe Frontend (Next.js)

## Setup

```bash
cd frontend
cp .env.example .env.local
npm install
npm run dev
```

Frontend runs on [http://localhost:3000](http://localhost:3000).

## Pages

- `/` candidate application form
- `/admin` admin login
- `/admin/dashboard` job management + candidate table + filters
- `/admin/candidates?id=<candidate_id>` candidate profile and manual AI/status override

## Required backend

Run FastAPI on `http://127.0.0.1:8000` (or update `NEXT_PUBLIC_API_BASE_URL`).

## Deployment

Set `NEXT_PUBLIC_API_BASE_URL` in your frontend deployment environment to your backend public URL.

Example:

```bash
NEXT_PUBLIC_API_BASE_URL=https://api.hireme.com
```

Notes:
- If `NEXT_PUBLIC_API_BASE_URL` is not set in production, frontend uses same-origin requests.
- For local dev, fallback is `http://localhost:8000`.
