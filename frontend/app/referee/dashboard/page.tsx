"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import {
  API_BASE,
  CandidateRecord,
  ReferenceListResponse,
  ReferenceRecord,
  readApiError,
} from "../../../lib/api";

interface CandidateListResponse {
  items: CandidateRecord[];
  total: number;
  offset: number;
  limit: number;
}

function parseBrief(candidate: CandidateRecord | null): string | null {
  if (!candidate) return null;
  if (candidate.candidate_brief && candidate.candidate_brief.trim()) {
    return candidate.candidate_brief.trim();
  }
  const raw = candidate.online_research_summary;
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as { brief?: string; llm_analysis?: { summary?: string } };
    if (parsed.brief && parsed.brief.trim()) return parsed.brief.trim();
    if (parsed.llm_analysis?.summary && parsed.llm_analysis.summary.trim()) {
      return parsed.llm_analysis.summary.trim();
    }
  } catch {
    return null;
  }
  return null;
}

export default function RefereeDashboardPage() {
  const router = useRouter();
  const [token, setToken] = useState("");
  const [candidates, setCandidates] = useState<CandidateRecord[]>([]);
  const [selectedCandidate, setSelectedCandidate] = useState<CandidateRecord | null>(null);
  const [references, setReferences] = useState<ReferenceRecord[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isSubmittingReference, setIsSubmittingReference] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const managerBrief = useMemo(() => parseBrief(selectedCandidate), [selectedCandidate]);

  useEffect(() => {
    const stored = localStorage.getItem("hireme_referee_token");
    if (!stored) {
      router.replace("/referee");
      return;
    }
    setToken(stored);
  }, [router]);

  const withAuth = (init: RequestInit = {}): RequestInit => ({
    ...init,
    headers: {
      Accept: "application/json",
      ...(init.headers || {}),
      Authorization: `Bearer ${token}`,
    },
  });

  const loadCandidates = async () => {
    if (!token) return;
    setIsLoading(true);
    setError("");
    try {
      const response = await fetch(`${API_BASE}/api/v1/referee/candidates?offset=0&limit=100`, withAuth());
      const payload = (await response.json().catch(() => null)) as CandidateListResponse | null;
      if (!response.ok || !payload) {
        throw new Error(readApiError(payload as never, "Failed to load candidates"));
      }
      const items = Array.isArray(payload.items) ? payload.items : [];
      setCandidates(items);
      if (!selectedCandidate && items.length > 0) {
        setSelectedCandidate(items[0]);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load candidates");
    } finally {
      setIsLoading(false);
    }
  };

  const loadReferences = async (applicationId: string) => {
    if (!token) return;
    try {
      const response = await fetch(
        `${API_BASE}/api/v1/referee/references?application_id=${applicationId}&offset=0&limit=50`,
        withAuth(),
      );
      const payload = (await response.json().catch(() => null)) as ReferenceListResponse | null;
      if (!response.ok || !payload) {
        throw new Error(readApiError(payload as never, "Failed to load references"));
      }
      setReferences(Array.isArray(payload.items) ? payload.items : []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load references");
      setReferences([]);
    }
  };

  const loadCandidateDetails = async (applicationId: string) => {
    if (!token) return;
    try {
      const response = await fetch(`${API_BASE}/api/v1/referee/candidates/${applicationId}`, withAuth());
      const payload = (await response.json().catch(() => null)) as CandidateRecord | null;
      if (!response.ok || !payload) {
        throw new Error(readApiError(payload as never, "Failed to load candidate details"));
      }
      setSelectedCandidate(payload);
      await loadReferences(applicationId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load candidate details");
    }
  };

  useEffect(() => {
    void loadCandidates();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  useEffect(() => {
    if (!selectedCandidate?.id) return;
    void loadReferences(selectedCandidate.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedCandidate?.id, token]);

  const onLogout = () => {
    localStorage.removeItem("hireme_referee_token");
    router.push("/referee");
  };

  const onSubmitReference = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!token || !selectedCandidate) return;
    const formData = new FormData(event.currentTarget);

    const payload = {
      application_id: selectedCandidate.id,
      candidate_email: selectedCandidate.email,
      referee_name: String(formData.get("referee_name") || "").trim(),
      referee_email: String(formData.get("referee_email") || "").trim() || null,
      referee_phone: String(formData.get("referee_phone") || "").trim() || null,
      referee_linkedin_url: String(formData.get("referee_linkedin_url") || "").trim() || null,
      referee_company: String(formData.get("referee_company") || "").trim() || null,
      referee_position: String(formData.get("referee_position") || "").trim() || null,
      relationship: String(formData.get("relationship") || "").trim() || null,
      notes: String(formData.get("notes") || "").trim() || null,
    };

    setIsSubmittingReference(true);
    setMessage("");
    setError("");
    try {
      const response = await fetch(
        `${API_BASE}/api/v1/referee/references`,
        withAuth({
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        }),
      );
      const body = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(readApiError(body, "Failed to submit reference"));
      }
      setMessage("Reference submitted successfully.");
      event.currentTarget.reset();
      await loadReferences(selectedCandidate.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to submit reference");
    } finally {
      setIsSubmittingReference(false);
    }
  };

  return (
    <main className="stack">
      <div className="page-head">
        <h1>Referee Dashboard</h1>
        <div className="row">
          <Link href="/" className="badge">
            Home
          </Link>
          <button type="button" className="secondary" onClick={onLogout}>
            Logout
          </button>
        </div>
      </div>

      {message ? <p className="ok">{message}</p> : null}
      {error ? <p className="error">{error}</p> : null}

      <section className="panel stack">
        <h2>Existing Applicants</h2>
        {isLoading ? <p>Loading applicants...</p> : null}
        {!isLoading && candidates.length === 0 ? (
          <p className="muted">No applicants found.</p>
        ) : null}
        {candidates.length > 0 ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Role</th>
                  <th>Status</th>
                  <th>AI Score</th>
                  <th>Email</th>
                </tr>
              </thead>
              <tbody>
                {candidates.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <button type="button" className="secondary" onClick={() => void loadCandidateDetails(item.id)}>
                        {item.full_name}
                      </button>
                    </td>
                    <td>{item.role_selection}</td>
                    <td>{item.applicant_status}</td>
                    <td>{item.ai_score ?? "-"}</td>
                    <td>{item.email}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      {selectedCandidate ? (
        <>
          <section className="panel stack">
            <h2>Applicant Details</h2>
            <p>
              <strong>Name:</strong> {selectedCandidate.full_name}
            </p>
            <p>
              <strong>Email:</strong> {selectedCandidate.email}
            </p>
            <p>
              <strong>Role:</strong> {selectedCandidate.role_selection}
            </p>
            <p>
              <strong>Status:</strong> {selectedCandidate.applicant_status}
            </p>
            <p>
              <strong>AI Score:</strong> {selectedCandidate.ai_score ?? "-"}
            </p>
            <p>
              <strong>Manager Brief:</strong> {managerBrief || "-"}
            </p>
            <p>
              <strong>LinkedIn:</strong>{" "}
              {selectedCandidate.linkedin_url ? (
                <a href={selectedCandidate.linkedin_url}>{selectedCandidate.linkedin_url}</a>
              ) : (
                "-"
              )}
            </p>
            <p>
              <strong>GitHub:</strong> <a href={selectedCandidate.github_url}>{selectedCandidate.github_url}</a>
            </p>
            <p>
              <strong>Portfolio:</strong>{" "}
              {selectedCandidate.portfolio_url ? (
                <a href={selectedCandidate.portfolio_url}>{selectedCandidate.portfolio_url}</a>
              ) : (
                "-"
              )}
            </p>
            <p>
              <strong>Twitter:</strong>{" "}
              {selectedCandidate.twitter_url ? (
                <a href={selectedCandidate.twitter_url}>{selectedCandidate.twitter_url}</a>
              ) : (
                "-"
              )}
            </p>
          </section>

          <section className="panel stack">
            <h2>Submit Reference</h2>
            <form className="stack" onSubmit={onSubmitReference}>
              <div className="form-grid">
                <label>
                  Referee Name
                  <input name="referee_name" required minLength={2} />
                </label>
                <label>
                  Referee Email
                  <input name="referee_email" type="email" />
                </label>
                <label>
                  Referee Phone
                  <input name="referee_phone" />
                </label>
                <label>
                  Referee LinkedIn
                  <input name="referee_linkedin_url" type="url" />
                </label>
                <label>
                  Company
                  <input name="referee_company" />
                </label>
                <label>
                  Position
                  <input name="referee_position" />
                </label>
                <label>
                  Relationship
                  <input name="relationship" />
                </label>
                <label className="full">
                  Notes
                  <textarea name="notes" />
                </label>
              </div>
              <button type="submit" disabled={isSubmittingReference}>
                {isSubmittingReference ? "Submitting..." : "Submit Reference"}
              </button>
            </form>
          </section>

          <section className="panel stack">
            <h2>Existing Referee Entries</h2>
            {references.length === 0 ? <p className="muted">No references submitted yet.</p> : null}
            {references.length > 0 ? (
              <ul>
                {references.map((item) => (
                  <li key={item.id}>
                    {item.referee_name}
                    {item.relationship ? ` (${item.relationship})` : ""}
                    {item.referee_email ? ` - ${item.referee_email}` : ""}
                  </li>
                ))}
              </ul>
            ) : null}
          </section>
        </>
      ) : null}
    </main>
  );
}
