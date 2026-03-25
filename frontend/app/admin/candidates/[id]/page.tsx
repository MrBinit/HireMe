"use client";

import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";

import { API_BASE, ApplicantStatus, CandidateRecord, readApiError } from "../../../../lib/api";

const statuses: ApplicantStatus[] = [
  "applied",
  "screened",
  "shortlisted",
  "in_interview",
  "offer",
  "rejected",
];

export default function CandidateProfilePage() {
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const [token, setToken] = useState<string>("");
  const [candidate, setCandidate] = useState<CandidateRecord | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => {
    const stored = localStorage.getItem("hireme_admin_token");
    if (!stored) {
      router.replace("/admin");
      return;
    }
    setToken(stored);
  }, [router]);

  const loadCandidate = async () => {
    if (!token) return;
    setIsLoading(true);
    setError("");
    try {
      const response = await fetch(`${API_BASE}/api/v1/admin/candidates/${params.id}`, {
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${token}`,
        },
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(readApiError(payload, "Failed to load candidate profile"));
      }
      setCandidate(payload as CandidateRecord);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load candidate profile");
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    loadCandidate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, params.id]);

  const onSubmitReview = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!token || !candidate) return;

    const formData = new FormData(event.currentTarget);
    const status = String(formData.get("applicant_status") || "");
    const note = String(formData.get("note") || "").trim();
    const aiScoreRaw = String(formData.get("ai_score") || "").trim();
    const aiScreeningSummary = String(formData.get("ai_screening_summary") || "").trim();
    const onlineResearchSummary = String(formData.get("online_research_summary") || "").trim();

    const payload: Record<string, unknown> = {};
    if (status) payload.applicant_status = status;
    if (note) payload.note = note;
    if (aiScoreRaw) payload.ai_score = Number(aiScoreRaw);
    if (aiScreeningSummary) payload.ai_screening_summary = aiScreeningSummary;
    if (onlineResearchSummary) payload.online_research_summary = onlineResearchSummary;

    setError("");
    setMessage("");
    try {
      const response = await fetch(`${API_BASE}/api/v1/admin/candidates/${candidate.id}/review`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(payload),
      });
      const responsePayload = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(readApiError(responsePayload, "Failed to save review update"));
      }
      setMessage("Candidate review updated.");
      setCandidate(responsePayload as CandidateRecord);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save review update");
    }
  };

  if (isLoading) {
    return (
      <main className="stack">
        <p>Loading candidate profile...</p>
      </main>
    );
  }

  if (!candidate) {
    return (
      <main className="stack">
        <p className="error">{error || "Candidate not found."}</p>
        <Link href="/admin/dashboard">Back to dashboard</Link>
      </main>
    );
  }

  return (
    <main className="stack">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1>{candidate.full_name}</h1>
        <Link href="/admin/dashboard" className="badge">
          Back
        </Link>
      </div>

      {message ? <p className="ok">{message}</p> : null}
      {error ? <p className="error">{error}</p> : null}

      <section className="panel stack">
        <h2>Profile</h2>
        <p>
          <strong>Email:</strong> {candidate.email}
        </p>
        <p>
          <strong>Role Applied:</strong> {candidate.role_selection}
        </p>
        <p>
          <strong>Status:</strong> {candidate.applicant_status}
        </p>
        <p>
          <strong>Submitted:</strong> {new Date(candidate.created_at).toLocaleString()}
        </p>
        <p>
          <strong>LinkedIn:</strong>{" "}
          {candidate.linkedin_url ? <a href={candidate.linkedin_url}>{candidate.linkedin_url}</a> : "-"}
        </p>
        <p>
          <strong>Portfolio:</strong> <a href={candidate.portfolio_url}>{candidate.portfolio_url}</a>
        </p>
        <p>
          <strong>GitHub:</strong> <a href={candidate.github_url}>{candidate.github_url}</a>
        </p>
      </section>

      <section className="panel stack">
        <h2>Resume</h2>
        <p>
          <strong>File:</strong> {candidate.resume.original_filename}
        </p>
        <p>
          <strong>S3 Path:</strong> <code>{candidate.resume.storage_path}</code>
        </p>
      </section>

      <section className="panel stack">
        <h2>AI Screening</h2>
        <p>
          <strong>AI Score:</strong> {candidate.ai_score ?? "-"}
        </p>
        <p>
          <strong>AI Screening Summary:</strong> {candidate.ai_screening_summary || "-"}
        </p>
        <p>
          <strong>Online Research:</strong> {candidate.online_research_summary || "-"}
        </p>
      </section>

      <section className="panel stack">
        <h2>Status History</h2>
        {candidate.status_history.length === 0 ? (
          <p className="muted">No status history available.</p>
        ) : (
          <ul>
            {candidate.status_history.map((entry, index) => (
              <li key={`${entry.changed_at}-${index}`}>
                {new Date(entry.changed_at).toLocaleString()} - {entry.status}
                {entry.note ? ` (${entry.note})` : ""} [{entry.source}]
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="panel stack">
        <h2>Manual Override / Review Update</h2>
        <form className="stack" onSubmit={onSubmitReview}>
          <label>
            Applicant Status
            <select name="applicant_status" defaultValue={candidate.applicant_status}>
              <option value="">No change</option>
              {statuses.map((status) => (
                <option key={status} value={status}>
                  {status}
                </option>
              ))}
            </select>
          </label>
          <label>
            AI Score (0-100)
            <input type="number" step="0.1" min="0" max="100" name="ai_score" />
          </label>
          <label>
            AI Screening Summary
            <textarea name="ai_screening_summary" defaultValue={candidate.ai_screening_summary || ""} />
          </label>
          <label>
            Online Research Summary
            <textarea
              name="online_research_summary"
              defaultValue={candidate.online_research_summary || ""}
            />
          </label>
          <label>
            Override Note
            <textarea name="note" placeholder="Reason for manual shortlist/status override" />
          </label>
          <button type="submit">Save Review Update</button>
        </form>
      </section>
    </main>
  );
}
