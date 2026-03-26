"use client";

import Link from "next/link";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { useParams, useRouter } from "next/navigation";

import {
  API_BASE,
  ApplicantStatus,
  CandidateRecord,
  getAdminResumeDownloadUrl,
  readApiError,
} from "../../../../lib/api";

const statuses: ApplicantStatus[] = [
  "applied",
  "screened",
  "shortlisted",
  "in_interview",
  "offer",
  "rejected",
];

function readString(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

function readStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => readString(item)).filter((item): item is string => item !== null);
}

function readRecordArray(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null);
}

export default function CandidateProfilePage() {
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const [token, setToken] = useState<string>("");
  const [candidate, setCandidate] = useState<CandidateRecord | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isDownloadingResume, setIsDownloadingResume] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const parseResult = useMemo<Record<string, unknown> | null>(() => {
    if (!candidate?.parse_result) return null;
    if (typeof candidate.parse_result !== "object") return null;
    return candidate.parse_result as Record<string, unknown>;
  }, [candidate]);

  const parsedSkills = readStringArray(parseResult?.skills);
  const parsedEducation = readRecordArray(parseResult?.education);
  const parsedWorkExperience = readRecordArray(parseResult?.work_experience);
  const parsedPriorOffices = readStringArray(parseResult?.old_offices);
  const parsedKeyResponsibilities = readStringArray(parseResult?.key_achievements);
  const parsedYears = typeof parseResult?.total_years_experience === "number"
    ? parseResult.total_years_experience
    : null;

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
          <strong>Type:</strong> {candidate.resume.content_type}
        </p>
        <p>
          <strong>Size:</strong> {(candidate.resume.size_bytes / (1024 * 1024)).toFixed(2)} MB
        </p>
        <p>
          <strong>S3 Path:</strong> <code>{candidate.resume.storage_path}</code>
        </p>
        <div className="row">
          <button type="button" onClick={onDownloadResume} disabled={isDownloadingResume}>
            {isDownloadingResume ? "Preparing download..." : "Download Resume"}
          </button>
        </div>
      </section>

      <section className="panel stack">
        <h2>AI Screening</h2>
        <p>
          <strong>Evaluation Status:</strong> {candidate.evaluation_status || "-"}
        </p>
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
        <h2>Parsed Candidate Profile</h2>
        <p>
          <strong>Parse Status:</strong> {candidate.parse_status}
        </p>
        <p>
          <strong>Total Years of Experience:</strong> {parsedYears ?? "-"}
        </p>

        <div className="stack">
          <h3>Skills</h3>
          {parsedSkills.length === 0 ? (
            <p className="muted">No parsed skills available.</p>
          ) : (
            <div className="row">
              {parsedSkills.map((skill) => (
                <span key={skill} className="badge">
                  {skill}
                </span>
              ))}
            </div>
          )}
        </div>

        <div className="stack">
          <h3>Education</h3>
          {parsedEducation.length === 0 ? (
            <p className="muted">No parsed education available.</p>
          ) : (
            <ul>
              {parsedEducation.map((item, index) => {
                const degree = readString(item.degree) || "Degree not detected";
                const institution = readString(item.institution) || "Institution not detected";
                const yearRange = readString(item.year_range) || "Year range not detected";
                return (
                  <li key={`${degree}-${institution}-${index}`}>
                    {degree} - {institution} ({yearRange})
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <div className="stack">
          <h3>Work Experience</h3>
          {parsedWorkExperience.length === 0 ? (
            <p className="muted">No parsed work experience available.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Company</th>
                    <th>Role</th>
                    <th>Duration</th>
                    <th>Key Responsibilities</th>
                  </tr>
                </thead>
                <tbody>
                  {parsedWorkExperience.map((item, index) => {
                    const company = readString(item.company) || "-";
                    const position = readString(item.position) || "-";
                    const startDate = readString(item.start_date);
                    const endDate = readString(item.end_date);
                    const duration = startDate && endDate ? `${startDate} to ${endDate}` : "-";
                    const responsibilities = readStringArray(item.job_description);

                    return (
                      <tr key={`${company}-${position}-${index}`}>
                        <td>{company}</td>
                        <td>{position}</td>
                        <td>{duration}</td>
                        <td>
                          {responsibilities.length === 0 ? (
                            "-"
                          ) : (
                            <ul>
                              {responsibilities.map((line) => (
                                <li key={line}>{line}</li>
                              ))}
                            </ul>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="stack">
          <h3>Prior Job Roles / Offices</h3>
          {parsedPriorOffices.length === 0 ? (
            <p className="muted">No prior company list available.</p>
          ) : (
            <ul>
              {parsedPriorOffices.map((office) => (
                <li key={office}>{office}</li>
              ))}
            </ul>
          )}
        </div>

        <div className="stack">
          <h3>Key Responsibilities / Achievements</h3>
          {parsedKeyResponsibilities.length === 0 ? (
            <p className="muted">No parsed responsibility summary available.</p>
          ) : (
            <ul>
              {parsedKeyResponsibilities.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          )}
        </div>
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
