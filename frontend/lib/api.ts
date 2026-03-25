export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") || "http://127.0.0.1:8000";

export type ApplicantStatus =
  | "applied"
  | "screened"
  | "shortlisted"
  | "in_interview"
  | "offer"
  | "rejected"
  | "received"
  | "in_progress"
  | "interview"
  | "accepted"
  | "sent_to_manager";

export interface JobOpening {
  id: string;
  role_title: string;
  team: string;
  location: string;
  experience_level: string;
  experience_range: string;
  application_open_at: string;
  application_close_at: string;
  paused: boolean;
  status: "open" | "closed" | "paused";
  responsibilities: string[];
  requirements: string[];
  created_at: string;
  updated_at: string;
}

export interface ResumeMeta {
  original_filename: string;
  stored_filename: string;
  storage_path: string;
  content_type: string;
  size_bytes: number;
}

export interface StatusHistoryEntry {
  status: string;
  note?: string | null;
  changed_at: string;
  source: string;
}

export interface CandidateRecord {
  id: string;
  job_opening_id: string;
  full_name: string;
  email: string;
  linkedin_url?: string | null;
  portfolio_url: string;
  github_url: string;
  twitter_url?: string | null;
  role_selection: string;
  parse_result?: Record<string, unknown> | null;
  parse_status: string;
  applicant_status: ApplicantStatus;
  ai_score?: number | null;
  ai_screening_summary?: string | null;
  online_research_summary?: string | null;
  status_history: StatusHistoryEntry[];
  reference_status: boolean;
  resume: ResumeMeta;
  created_at: string;
}

export interface ApiErrorPayload {
  error?: {
    code?: string;
    message?: string;
    details?: unknown;
  };
  detail?: unknown;
}

export function readApiError(payload: ApiErrorPayload | null, fallback = "Request failed"): string {
  if (!payload) return fallback;
  if (payload.error?.message) return payload.error.message;
  if (typeof payload.detail === "string") return payload.detail;
  return fallback;
}

export async function requestJson<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.headers || {}),
    },
    cache: "no-store",
  });

  const maybeJson = (await response
    .json()
    .catch(() => null)) as ApiErrorPayload | T | null;

  if (!response.ok) {
    const errorMessage = readApiError(
      (maybeJson as ApiErrorPayload | null) || null,
      `Request failed with ${response.status}`,
    );
    throw new Error(errorMessage);
  }

  return maybeJson as T;
}
