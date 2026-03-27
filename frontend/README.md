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
- `/admin/candidates/[id]` candidate profile and manual AI/status override

## Required backend

Run FastAPI on `http://127.0.0.1:8000` (or update `NEXT_PUBLIC_API_BASE_URL`).
