"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";

import {
  API_BASE,
  CandidateRecord,
  getAdminResumeDownloadUrl,
  ReferenceListResponse,
  ReferenceRecord,
  readApiError,
} from "../../../../lib/api";

export default function CandidateProfilePage() {
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const [token, setToken] = useState<string>("");
  const [candidate, setCandidate] = useState<CandidateRecord | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isDownloadingResume, setIsDownloadingResume] = useState(false);
  const [references, setReferences] = useState<ReferenceRecord[]>([]);
  const [isLoadingReferences, setIsLoadingReferences] = useState(false);
  const [error, setError] = useState("");
  const candidateId = candidate?.id || "";
  const hasBrief = Boolean(candidate?.candidate_brief);
  const isRejected = candidate?.applicant_status === "rejected";
  const evalFailed = candidate?.evaluation_status === "failed";

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

  useEffect(() => {
    if (!token || !candidateId) return;

    let stopped = false;
    const loadReferences = async () => {
      setIsLoadingReferences(true);
      try {
        const response = await fetch(
          `${API_BASE}/api/v1/references?application_id=${candidateId}&offset=0&limit=50`,
          {
            headers: {
              Accept: "application/json",
              Authorization: `Bearer ${token}`,
            },
          },
        );
        const payload = (await response.json().catch(() => null)) as ReferenceListResponse | null;
        if (!response.ok || !payload || stopped) return;
        setReferences(Array.isArray(payload.items) ? payload.items : []);
      } catch {
        if (!stopped) setReferences([]);
      } finally {
        if (!stopped) setIsLoadingReferences(false);
      }
    };

    void loadReferences();
    return () => {
      stopped = true;
    };
  }, [token, candidateId]);

  useEffect(() => {
    if (!token || !candidateId) return;
    if (hasBrief || isRejected || evalFailed) return;

    let stopped = false;
    const poll = async () => {
      try {
        const response = await fetch(`${API_BASE}/api/v1/admin/candidates/${params.id}`, {
          headers: {
            Accept: "application/json",
            Authorization: `Bearer ${token}`,
          },
        });
        if (!response.ok) return;
        const payload = await response.json().catch(() => null);
        if (stopped || !payload) return;
        setCandidate(payload as CandidateRecord);
      } catch {
        // Polling is best-effort; keep UI usable when refresh call fails.
      }
    };

    const intervalId = window.setInterval(() => {
      void poll();
    }, 6000);
    return () => {
      stopped = true;
      window.clearInterval(intervalId);
    };
  }, [token, params.id, candidateId, hasBrief, isRejected, evalFailed]);

  const onDownloadResume = async () => {
    if (!token || !candidate) return;
    setIsDownloadingResume(true);
    setError("");
    try {
      const payload = await getAdminResumeDownloadUrl(candidate.id, token);
      window.open(payload.download_url, "_blank", "noopener,noreferrer");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch resume download link");
    } finally {
      setIsDownloadingResume(false);
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
      <div className="page-head">
        <h1>{candidate.full_name}</h1>
        <Link href="/admin/dashboard" className="badge">
          Back
        </Link>
      </div>

      {error ? <p className="error">{error}</p> : null}

      <section className="panel stack">
        <h2>Candidate Overview</h2>
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
          <strong>AI Score:</strong> {candidate.ai_score ?? "-"}
        </p>
        <p>
          <strong>Evaluation:</strong> {candidate.evaluation_status || "-"}
        </p>
        <p>
          <strong>Interview Scheduling:</strong> {candidate.interview_schedule_status || "-"}
        </p>
        <p>
          <strong>Interview Email Sent At:</strong>{" "}
          {candidate.interview_schedule_sent_at
            ? new Date(candidate.interview_schedule_sent_at).toLocaleString()
            : "-"}
        </p>
        <p>
          <strong>Interview Scheduler Error:</strong> {candidate.interview_schedule_error || "-"}
        </p>
      </section>

      <section className="panel stack">
        <h2>Public Profiles</h2>
        <p>
          <strong>LinkedIn:</strong>{" "}
          {candidate.linkedin_url ? (
            <a href={candidate.linkedin_url} target="_blank" rel="noreferrer">
              {candidate.linkedin_url}
            </a>
          ) : (
            "-"
          )}
        </p>
        <p>
          <strong>Portfolio:</strong>{" "}
          {candidate.portfolio_url ? (
            <a href={candidate.portfolio_url} target="_blank" rel="noreferrer">
              {candidate.portfolio_url}
            </a>
          ) : (
            "-"
          )}
        </p>
        <p>
          <strong>GitHub:</strong>{" "}
          <a href={candidate.github_url} target="_blank" rel="noreferrer">
            {candidate.github_url}
          </a>
        </p>
        <p>
          <strong>Twitter:</strong>{" "}
          {candidate.twitter_url ? (
            <a href={candidate.twitter_url} target="_blank" rel="noreferrer">
              {candidate.twitter_url}
            </a>
          ) : (
            "-"
          )}
        </p>
      </section>

      <section className="panel stack">
        <h2>Resume</h2>
        <div className="row">
          <button type="button" onClick={onDownloadResume} disabled={isDownloadingResume}>
            {isDownloadingResume ? "Preparing download..." : "Download Resume"}
          </button>
        </div>
      </section>

      <section className="panel stack">
        <h2>Hiring Manager Brief</h2>
        <p>{candidate.candidate_brief || "-"}</p>
        {candidate.candidate_brief ? null : (
          <p className="muted">
            Brief is still generating in the background. This page refreshes automatically.
          </p>
        )}
      </section>

      <section className="panel stack">
        <h2>Referees</h2>
        {isLoadingReferences ? <p className="muted">Loading referee entries...</p> : null}
        {!isLoadingReferences && references.length === 0 ? (
          <p className="muted">No referee details submitted yet.</p>
        ) : null}
        {references.length > 0 ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Email</th>
                  <th>Company</th>
                  <th>Position</th>
                  <th>Relationship</th>
                  <th>Notes</th>
                </tr>
              </thead>
              <tbody>
                {references.map((item) => (
                  <tr key={item.id}>
                    <td>{item.referee_name}</td>
                    <td>{item.referee_email || "-"}</td>
                    <td>{item.referee_company || "-"}</td>
                    <td>{item.referee_position || "-"}</td>
                    <td>{item.relationship || "-"}</td>
                    <td>{item.notes || "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
    </main>
  );
}
