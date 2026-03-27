"use client";

import Link from "next/link";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import {
  API_BASE,
  ApplicantStatus,
  CandidateRecord,
  JobOpening,
  getAdminResumeDownloadUrl,
  readApiError,
} from "../../../lib/api";

interface CandidateListResponse {
  items: CandidateRecord[];
  total: number;
  offset: number;
  limit: number;
}

interface JobOpeningListResponse {
  items: JobOpening[];
  total: number;
  offset: number;
  limit: number;
}

const dashboardStatuses: ApplicantStatus[] = [
  "applied",
  "screened",
  "shortlisted",
  "in_interview",
  "offer",
  "offer_letter_created",
  "offer_letter_sent",
  "offer_letter_sign",
  "accepted",
  "rejected",
];

export default function AdminDashboardPage() {
  const router = useRouter();
  const [token, setToken] = useState<string>("");
  const [openings, setOpenings] = useState<JobOpening[]>([]);
  const [candidates, setCandidates] = useState<CandidateRecord[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [downloadingCandidateId, setDownloadingCandidateId] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const [roleFilter, setRoleFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [fromFilter, setFromFilter] = useState("");
  const [toFilter, setToFilter] = useState("");

  useEffect(() => {
    const stored = localStorage.getItem("hireme_admin_token");
    if (!stored) {
      router.replace("/admin");
      return;
    }
    setToken(stored);
  }, [router]);

  const roleOptions = useMemo(
    () => [...new Set(openings.map((item) => item.role_title))].sort(),
    [openings],
  );
  const totalPositions = roleOptions.length;
  const openJobs = openings.filter((opening) => opening.status === "open").length;
  const totalApplicants = candidates.length;

  const withAuth = (init: RequestInit = {}): RequestInit => ({
    ...init,
    headers: {
      Accept: "application/json",
      ...(init.headers || {}),
      Authorization: `Bearer ${token}`,
    },
  });

  const loadData = async () => {
    if (!token) return;

    setIsLoading(true);
    setError("");
    try {
      const openingResp = await fetch(`${API_BASE}/api/v1/job-openings`, { cache: "no-store" });
      const openingJson = (await openingResp.json()) as JobOpeningListResponse;
      if (!openingResp.ok) {
        throw new Error("Failed to load job openings");
      }
      setOpenings(openingJson.items || []);

      const params = new URLSearchParams();
      if (roleFilter) params.set("role_selection", roleFilter);
      if (statusFilter) params.set("applicant_status", statusFilter);
      if (fromFilter) {
        params.set("submitted_from", new Date(`${fromFilter}T00:00:00`).toISOString());
      }
      if (toFilter) {
        params.set("submitted_to", new Date(`${toFilter}T23:59:59.999`).toISOString());
      }

      const candidateResp = await fetch(
        `${API_BASE}/api/v1/admin/candidates${params.toString() ? `?${params}` : ""}`,
        withAuth(),
      );
      const candidateJson = (await candidateResp.json()) as CandidateListResponse;
      if (!candidateResp.ok) {
        throw new Error(readApiError(candidateJson as unknown as { detail?: unknown }, "Failed to load candidates"));
      }
      setCandidates(candidateJson.items || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load dashboard");
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    loadData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const onCreateJob = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!token) return;

    const formData = new FormData(event.currentTarget);
    const responsibilities = String(formData.get("responsibilities") || "")
      .split("\n")
      .map((item) => item.trim())
      .filter(Boolean);
    const requirements = String(formData.get("requirements") || "")
      .split("\n")
      .map((item) => item.trim())
      .filter(Boolean);

    const payload = {
      role_title: String(formData.get("role_title") || "").trim(),
      manager_email: String(formData.get("manager_email") || "").trim(),
      team: String(formData.get("team") || "").trim(),
      location: String(formData.get("location") || "").trim(),
      experience_level: String(formData.get("experience_level") || "").trim().toLowerCase(),
      experience_range: String(formData.get("experience_range") || "").trim().toLowerCase(),
      application_open_at: new Date(String(formData.get("application_open_at") || "")).toISOString(),
      application_close_at: new Date(String(formData.get("application_close_at") || "")).toISOString(),
      responsibilities,
      requirements,
    };

    setMessage("");
    setError("");
    try {
      const response = await fetch(
        `${API_BASE}/api/v1/job-openings`,
        withAuth({
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        }),
      );
      const json = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(readApiError(json, "Failed to create job opening"));
      }
      setMessage("Job opening created.");
      event.currentTarget.reset();
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create job opening");
    }
  };

  const onDeleteJob = async (jobOpeningId: string) => {
    if (!token) return;
    setError("");
    setMessage("");
    try {
      const response = await fetch(
        `${API_BASE}/api/v1/job-openings/${jobOpeningId}`,
        withAuth({ method: "DELETE" }),
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(readApiError(payload, "Failed to delete opening"));
      }
      setMessage("Job opening removed.");
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete opening");
    }
  };

  const onTogglePause = async (opening: JobOpening) => {
    if (!token) return;
    setError("");
    setMessage("");
    try {
      const response = await fetch(
        `${API_BASE}/api/v1/job-openings/${opening.id}/pause`,
        withAuth({
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ paused: !opening.paused }),
        }),
      );
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(readApiError(payload, "Failed to update pause status"));
      }
      setMessage(`Job opening ${opening.paused ? "resumed" : "paused"}.`);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update pause state");
    }
  };

  const onLogout = () => {
    localStorage.removeItem("hireme_admin_token");
    router.push("/admin");
  };

  const onDownloadResume = async (candidateId: string) => {
    if (!token) return;
    setError("");
    setDownloadingCandidateId(candidateId);
    try {
      const payload = await getAdminResumeDownloadUrl(candidateId, token);
      window.open(payload.download_url, "_blank", "noopener,noreferrer");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch resume download link");
    } finally {
      setDownloadingCandidateId("");
    }
  };

  return (
    <main className="stack">
      <div className="page-head">
        <h1>Admin Dashboard</h1>
        <button className="secondary" onClick={onLogout} type="button">
          Logout
        </button>
      </div>

      {message ? <p className="ok">{message}</p> : null}
      {error ? <p className="error">{error}</p> : null}

      <section className="summary-grid">
        <article className="panel summary-card">
          <p className="muted">Job Positions</p>
          <h2>{totalPositions}</h2>
        </article>
        <article className="panel summary-card">
          <p className="muted">Open Jobs</p>
          <h2>{openJobs}</h2>
        </article>
        <article className="panel summary-card">
          <p className="muted">Applicants</p>
          <h2>{totalApplicants}</h2>
        </article>
      </section>

      <section className="panel stack">
        <h2>Create Job Opening</h2>
        <form className="stack" onSubmit={onCreateJob}>
          <div className="form-grid">
            <label>
              Role Title
              <input name="role_title" required />
            </label>
            <label>
              Team
              <input name="team" required />
            </label>
            <label>
              Manager Email
              <input name="manager_email" type="email" required />
            </label>
            <label>
              Location (remote/onsite or city)
              <input name="location" required />
            </label>
            <label>
              Experience Level
              <select name="experience_level" required>
                <option value="intern">intern</option>
                <option value="junior">junior</option>
                <option value="mid">mid</option>
                <option value="senior">senior</option>
                <option value="staff">staff</option>
                <option value="principal">principal</option>
              </select>
            </label>
            <label>
              Experience Range
              <input name="experience_range" required placeholder="2-4 years" />
            </label>
            <label>
              Application Open At
              <input name="application_open_at" type="datetime-local" required />
            </label>
            <label>
              Application Close At
              <input name="application_close_at" type="datetime-local" required />
            </label>
            <label className="full">
              Responsibilities (one per line)
              <textarea name="responsibilities" required />
            </label>
            <label className="full">
              Requirements (one per line)
              <textarea name="requirements" required />
            </label>
          </div>
          <button type="submit">Create Job</button>
        </form>
      </section>

      <section className="panel stack">
        <h2>Job Openings</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Role</th>
                <th>Manager</th>
                <th>Status</th>
                <th>Window</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {openings.map((opening) => (
                <tr key={opening.id}>
                  <td>{opening.role_title}</td>
                  <td>{opening.manager_email || "-"}</td>
                  <td>{opening.status}</td>
                  <td>
                    {new Date(opening.application_open_at).toLocaleDateString()} -{" "}
                    {new Date(opening.application_close_at).toLocaleDateString()}
                  </td>
                  <td>
                    <div className="row">
                      <button type="button" className="secondary" onClick={() => onTogglePause(opening)}>
                        {opening.paused ? "Resume" : "Pause"}
                      </button>
                      <button type="button" className="danger" onClick={() => onDeleteJob(opening.id)}>
                        Remove
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel stack">
        <h2>Submitted Applications</h2>
        <div className="form-grid">
          <label>
            Filter by role
            <select value={roleFilter} onChange={(event) => setRoleFilter(event.target.value)}>
              <option value="">All</option>
              {roleOptions.map((role) => (
                <option key={role} value={role}>
                  {role}
                </option>
              ))}
            </select>
          </label>
          <label>
            Filter by status
            <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
              <option value="">All</option>
              {dashboardStatuses.map((status) => (
                <option key={status} value={status}>
                  {status}
                </option>
              ))}
            </select>
          </label>
          <label>
            Submitted from
            <input type="date" value={fromFilter} onChange={(event) => setFromFilter(event.target.value)} />
          </label>
          <label>
            Submitted to
            <input type="date" value={toFilter} onChange={(event) => setToFilter(event.target.value)} />
          </label>
        </div>
        <div className="row">
          <button type="button" onClick={loadData}>
            Apply Filters
          </button>
        </div>

        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Position</th>
                <th>Status</th>
                <th>Evaluation</th>
                <th>Email</th>
                <th>Submission Date</th>
                <th>AI Score</th>
                <th>Resume</th>
              </tr>
            </thead>
            <tbody>
              {!isLoading && candidates.length === 0 ? (
                <tr>
                  <td colSpan={9} className="muted">
                    No candidates found.
                  </td>
                </tr>
              ) : null}
              {candidates.map((candidate) => (
                <tr key={candidate.id}>
                  <td>
                    <Link href={`/admin/candidates/${candidate.id}`}>{candidate.full_name}</Link>
                  </td>
                  <td>{candidate.role_selection}</td>
                  <td>{candidate.applicant_status}</td>
                  <td>{candidate.evaluation_status || "-"}</td>
                  <td>{candidate.email}</td>
                  <td>{new Date(candidate.created_at).toLocaleString()}</td>
                  <td>{candidate.ai_score ?? "-"}</td>
                  <td>
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => onDownloadResume(candidate.id)}
                      disabled={downloadingCandidateId === candidate.id}
                    >
                      {downloadingCandidateId === candidate.id ? "Loading..." : "Download"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </main>
  );
}
