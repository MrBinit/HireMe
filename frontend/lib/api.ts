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
  | "sent_to_manager"
  | "offer_letter_created"
  | "offer_letter_sent"
  | "offer_letter_sign";

export type EvaluationStatus = "queued" | "in_progress" | "completed" | "failed";
export type ManagerDecision = "select" | "reject";

export interface ManagerSelectionDetails {
  confirmed_job_title: string;
  start_date: string;
  base_salary: string;
  compensation_structure: string;
  equity_or_bonus?: string | null;
  reporting_manager: string;
  custom_terms?: string | null;
}

export interface JobOpening {
  id: string;
  role_title: string;
  manager_email?: string | null;
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

export interface ResumeDownloadResponse {
  download_url: string;
  expires_in_seconds: number;
  filename: string;
}

export interface CandidateRecord {
  id: string;
  job_opening_id: string;
  full_name: string;
  email: string;
  linkedin_url: string | null;
  portfolio_url?: string | null;
  github_url: string;
  twitter_url?: string | null;
  role_selection: string;
  parse_result?: Record<string, unknown> | null;
  parse_status: string;
  evaluation_status?: EvaluationStatus | null;
  applicant_status: ApplicantStatus;
  ai_score?: number | null;
  ai_screening_summary?: string | null;
  candidate_brief?: string | null;
  online_research_summary?: string | null;
  interview_schedule_status?: string | null;
  interview_schedule_options?: Record<string, unknown> | null;
  interview_schedule_sent_at?: string | null;
  interview_hold_expires_at?: string | null;
  interview_calendar_email?: string | null;
  interview_schedule_error?: string | null;
  interview_transcript_status?: string | null;
  interview_transcript_url?: string | null;
  interview_transcript_summary?: string | null;
  interview_transcript_synced_at?: string | null;
  manager_decision?: ManagerDecision | null;
  manager_decision_at?: string | null;
  manager_decision_note?: string | null;
  manager_selection_details?: ManagerSelectionDetails | null;
  manager_selection_template_output?: string | null;
  offer_letter_status?: string | null;
  offer_letter_storage_path?: string | null;
  offer_letter_signed_storage_path?: string | null;
  offer_letter_generated_at?: string | null;
  offer_letter_sent_at?: string | null;
  offer_letter_signed_at?: string | null;
  offer_letter_error?: string | null;
  docusign_envelope_id?: string | null;
  slack_invite_status?: string | null;
  slack_invited_at?: string | null;
  slack_user_id?: string | null;
  slack_joined_at?: string | null;
  slack_welcome_message?: string | null;
  slack_welcome_sent_at?: string | null;
  slack_onboarding_status?: string | null;
  slack_error?: string | null;
  status_history: StatusHistoryEntry[];
  reference_status: boolean;
  resume: ResumeMeta;
  created_at: string;
}

export interface PublicApplicationStatus {
  application_id: string;
  applicant_status: ApplicantStatus;
  parse_status: "pending" | "in_progress" | "completed" | "failed";
  evaluation_status?: EvaluationStatus | null;
  interview_schedule_status?: string | null;
  ai_score?: number | null;
  role_selection: string;
  submitted_at: string;
  research_ready: boolean;
}

export interface InterviewSlotConfirmResponse {
  application_id: string;
  interview_schedule_status: string;
  applicant_status: ApplicantStatus;
  selected_option_number: number;
  confirmed_event_id: string;
  confirmed_event_link?: string | null;
  confirmed_meeting_link?: string | null;
  confirmed_at: string;
}

export interface InterviewActionResponse {
  application_id: string;
  interview_schedule_status: string;
  applicant_status: ApplicantStatus;
  message: string;
  confirmed_event_link?: string | null;
  confirmed_meeting_link?: string | null;
}

export interface ManagerDecisionRequest {
  decision: ManagerDecision;
  note?: string | null;
  selection_details?: ManagerSelectionDetails | null;
}

export interface ReferenceRecord {
  id: string;
  application_id: string;
  candidate_email: string;
  referee_name: string;
  referee_email?: string | null;
  referee_phone?: string | null;
  referee_linkedin_url?: string | null;
  referee_company?: string | null;
  referee_position?: string | null;
  relationship?: string | null;
  notes?: string | null;
  created_at: string;
}

export interface ReferenceListResponse {
  items: ReferenceRecord[];
  total: number;
  offset: number;
  limit: number;
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

export async function getAdminResumeDownloadUrl(
  applicationId: string,
  token: string,
): Promise<ResumeDownloadResponse> {
  return requestJson<ResumeDownloadResponse>(
    `/api/v1/admin/candidates/${applicationId}/resume-download`,
    {
      headers: {
        Authorization: `Bearer ${token}`,
      },
    },
  );
}

export async function getAdminOfferLetterDownloadUrl(
  applicationId: string,
  token: string,
): Promise<ResumeDownloadResponse> {
  return requestJson<ResumeDownloadResponse>(
    `/api/v1/admin/candidates/${applicationId}/offer-letter-download`,
    {
      headers: {
        Authorization: `Bearer ${token}`,
      },
    },
  );
}

export async function confirmInterviewSlot(
  applicationId: string,
  payload: { email: string; option_number: number },
): Promise<InterviewSlotConfirmResponse> {
  return requestJson<InterviewSlotConfirmResponse>(
    `/api/v1/applications/${applicationId}/interview/confirm`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    },
  );
}

export async function confirmInterviewSlotByToken(
  token: string,
): Promise<InterviewSlotConfirmResponse> {
  return requestJson<InterviewSlotConfirmResponse>(
    `/api/v1/applications/interview/confirm-token`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ token }),
    },
  );
}

export async function processInterviewActionByToken(
  token: string,
): Promise<InterviewActionResponse> {
  return requestJson<InterviewActionResponse>(
    `/api/v1/applications/interview/action-token`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ token }),
    },
  );
}

export async function submitManagerDecision(
  applicationId: string,
  token: string,
  payload: ManagerDecisionRequest,
): Promise<CandidateRecord> {
  return requestJson<CandidateRecord>(
    `/api/v1/admin/candidates/${applicationId}/manager-decision`,
    {
      method: "PATCH",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    },
  );
}

export async function approveOfferLetter(
  applicationId: string,
  token: string,
): Promise<CandidateRecord> {
  return requestJson<CandidateRecord>(
    `/api/v1/admin/candidates/${applicationId}/offer-letter/approve`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
      },
    },
  );
}

export async function syncOfferLetterSignatureStatus(
  applicationId: string,
  token: string,
): Promise<CandidateRecord> {
  return requestJson<CandidateRecord>(
    `/api/v1/admin/candidates/${applicationId}/offer-letter/sync-signature`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
      },
    },
  );
}

export async function retrySlackInvite(
  applicationId: string,
  token: string,
): Promise<CandidateRecord> {
  return requestJson<CandidateRecord>(
    `/api/v1/admin/candidates/${applicationId}/slack/retry-invite`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
      },
    },
  );
}
