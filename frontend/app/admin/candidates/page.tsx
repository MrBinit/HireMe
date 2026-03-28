"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import {
  approveOfferLetter,
  API_BASE,
  CandidateRecord,
  FirefliesDemoRequest,
  getAdminOfferLetterDownloadUrl,
  getAdminResumeDownloadUrl,
  ManagerSelectionDetails,
  ReferenceListResponse,
  ReferenceRecord,
  readApiError,
  retrySlackInvite,
  setFirefliesDemoState,
  submitManagerDecision,
  syncOfferLetterSignatureStatus,
  updateApplicantStatus,
} from "../../../lib/api";
import { evidenceRefHref, normalizeSeverity, parseResearchSummary } from "../../../lib/researchSummary";

function severityBadgeClass(value: string): string {
  const normalized = normalizeSeverity(value);
  return `badge severity-badge severity-${normalized}`;
}

function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "-";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function formatIssueType(value: string): string {
  return String(value || "unknown")
    .trim()
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ");
}

function tryPrettyJson(value: string): string | null {
  const raw = String(value || "").trim();
  if (!raw) return null;
  if (!(raw.startsWith("{") || raw.startsWith("["))) return null;
  try {
    const parsed = JSON.parse(raw);
    return JSON.stringify(parsed, null, 2);
  } catch {
    return null;
  }
}

function summarizeText(value: string, maxChars: number): string {
  const text = String(value || "").trim();
  if (!text) return "No details provided.";
  if (text.length <= maxChars) return text;
  return `${text.slice(0, maxChars).trimEnd()}...`;
}

export default function CandidateProfilePage() {
  const router = useRouter();
  const [candidateRouteId, setCandidateRouteId] = useState("");
  const [token, setToken] = useState<string>("");
  const [candidate, setCandidate] = useState<CandidateRecord | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isDownloadingResume, setIsDownloadingResume] = useState(false);
  const [isDownloadingOfferLetter, setIsDownloadingOfferLetter] = useState(false);
  const [isApprovingOfferLetter, setIsApprovingOfferLetter] = useState(false);
  const [isSyncingSignature, setIsSyncingSignature] = useState(false);
  const [isRetryingSlackInvite, setIsRetryingSlackInvite] = useState(false);
  const [isApplyingFirefliesDemo, setIsApplyingFirefliesDemo] = useState(false);
  const [isUpdatingApplicantStatus, setIsUpdatingApplicantStatus] = useState(false);
  const [isSubmittingDecision, setIsSubmittingDecision] = useState(false);
  const [statusDecisionNote, setStatusDecisionNote] = useState("");
  const [decision, setDecision] = useState<"select" | "reject">("select");
  const [decisionNote, setDecisionNote] = useState("");
  const [selectionDetails, setSelectionDetails] = useState<ManagerSelectionDetails>({
    confirmed_job_title: "",
    start_date: "",
    base_salary: "",
    compensation_structure: "",
    equity_or_bonus: "",
    reporting_manager: "",
    custom_terms: "",
  });
  const [references, setReferences] = useState<ReferenceRecord[]>([]);
  const [isLoadingReferences, setIsLoadingReferences] = useState(false);
  const [error, setError] = useState("");
  const candidateId = candidate?.id || "";
  const hasBrief = Boolean(candidate?.candidate_brief);
  const parsedResearch = useMemo(
    () => parseResearchSummary(candidate?.online_research_summary, candidate?.research_summary),
    [candidate?.online_research_summary, candidate?.research_summary],
  );
  const isRejected = candidate?.applicant_status === "rejected";
  const evalFailed = candidate?.evaluation_status === "failed";
  const interviewScheduleStatus = String(candidate?.interview_schedule_status || "").toLowerCase();
  const canSubmitManagerDecision = interviewScheduleStatus === "interview_done";
  const transcriptStatus = String(candidate?.interview_transcript_status || "").toLowerCase();
  const isInterviewLifecycle = interviewScheduleStatus.startsWith("interview");
  const isTranscriptTerminal = ["completed", "not_found", "failed"].includes(transcriptStatus);
  const needsBriefRefresh = !hasBrief && !isRejected && !evalFailed;
  const needsTranscriptRefresh = isInterviewLifecycle && !isTranscriptTerminal;
  const canManualScreeningDecision =
    candidate?.applicant_status === "screened" || candidate?.applicant_status === "applied";
  const fireflies = (() => {
    if (!candidate?.interview_schedule_options) return null;
    const source = candidate.interview_schedule_options as Record<string, unknown>;
    const payload = source.fireflies;
    if (!payload || typeof payload !== "object") return null;
    return payload as Record<string, unknown>;
  })();
  const firefliesTranscript =
    fireflies && typeof fireflies.transcript === "object" && fireflies.transcript
      ? (fireflies.transcript as Record<string, unknown>)
      : null;
  const confirmedMeetingLink = (() => {
    if (!candidate?.interview_schedule_options) return null;
    const source = candidate.interview_schedule_options as Record<string, unknown>;
    const value = source.confirmed_meeting_link;
    return typeof value === "string" && value.trim().length > 0 ? value : null;
  })();
  const firefliesActionItems =
    firefliesTranscript && Array.isArray(firefliesTranscript.action_items)
      ? firefliesTranscript.action_items.filter((item): item is string => typeof item === "string")
      : [];
  const issueFlags = parsedResearch?.issueFlags || [];
  const issueCounts = issueFlags.reduce(
    (acc, item) => {
      const sev = normalizeSeverity(item.severity);
      if (sev === "high") acc.high += 1;
      else if (sev === "medium") acc.medium += 1;
      else if (sev === "low") acc.low += 1;
      else acc.unknown += 1;
      return acc;
    },
    { high: 0, medium: 0, low: 0, unknown: 0 },
  );

  useEffect(() => {
    const stored = localStorage.getItem("hireme_admin_token");
    if (!stored) {
      router.replace("/admin");
      return;
    }
    const routeId = new URLSearchParams(window.location.search).get("id") || "";
    setCandidateRouteId(routeId.trim());
    setToken(stored);
  }, [router]);

  const loadCandidate = async () => {
    if (!token) return;
    if (!candidateRouteId) {
      setError("Missing candidate id in URL.");
      setIsLoading(false);
      return;
    }
    setIsLoading(true);
    setError("");
    try {
      const response = await fetch(`${API_BASE}/api/v1/admin/candidates/${candidateRouteId}`, {
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${token}`,
        },
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(readApiError(payload, "Failed to load candidate profile"));
      }
      const loaded = payload as CandidateRecord;
      setCandidate(loaded);
      setDecision(loaded.manager_decision || "select");
      setDecisionNote(loaded.manager_decision_note || "");
      if (loaded.manager_selection_details) {
        setSelectionDetails({
          confirmed_job_title: loaded.manager_selection_details.confirmed_job_title || "",
          start_date: loaded.manager_selection_details.start_date || "",
          base_salary: loaded.manager_selection_details.base_salary || "",
          compensation_structure: loaded.manager_selection_details.compensation_structure || "",
          equity_or_bonus: loaded.manager_selection_details.equity_or_bonus || "",
          reporting_manager: loaded.manager_selection_details.reporting_manager || "",
          custom_terms: loaded.manager_selection_details.custom_terms || "",
        });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load candidate profile");
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    loadCandidate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, candidateRouteId]);

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
    if (!needsBriefRefresh && !needsTranscriptRefresh) return;

    let stopped = false;
    const poll = async () => {
      try {
        const response = await fetch(`${API_BASE}/api/v1/admin/candidates/${candidateRouteId}`, {
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
  }, [token, candidateRouteId, candidateId, needsBriefRefresh, needsTranscriptRefresh]);

  useEffect(() => {
    if (!token || !candidateId || !candidate?.docusign_envelope_id) return;
    if (candidate.offer_letter_status !== "sent_for_signature") return;

    let stopped = false;
    let inFlight = false;
    const syncOnce = async () => {
      if (stopped || inFlight) return;
      inFlight = true;
      try {
        const updated = await syncOfferLetterSignatureStatus(candidateId, token);
        if (!stopped) {
          setCandidate(updated);
        }
      } catch {
        // Best-effort polling; keep UI responsive even if one sync fails.
      } finally {
        inFlight = false;
      }
    };

    const intervalId = window.setInterval(() => {
      void syncOnce();
    }, 30000);
    void syncOnce();
    return () => {
      stopped = true;
      window.clearInterval(intervalId);
    };
  }, [token, candidateId, candidate?.docusign_envelope_id, candidate?.offer_letter_status]);

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

  const onDownloadOfferLetter = async () => {
    if (!token || !candidate) return;
    setIsDownloadingOfferLetter(true);
    setError("");
    try {
      const payload = await getAdminOfferLetterDownloadUrl(candidate.id, token);
      window.open(payload.download_url, "_blank", "noopener,noreferrer");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch offer letter download link");
    } finally {
      setIsDownloadingOfferLetter(false);
    }
  };

  const onApproveOfferLetter = async () => {
    if (!token || !candidate) return;
    setIsApprovingOfferLetter(true);
    setError("");
    try {
      const updated = await approveOfferLetter(candidate.id, token);
      setCandidate(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to approve/send offer letter");
    } finally {
      setIsApprovingOfferLetter(false);
    }
  };

  const onSyncOfferSignature = async () => {
    if (!token || !candidate || !candidate.docusign_envelope_id) return;
    setIsSyncingSignature(true);
    setError("");
    try {
      const updated = await syncOfferLetterSignatureStatus(candidate.id, token);
      setCandidate(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to sync DocuSign signature status");
    } finally {
      setIsSyncingSignature(false);
    }
  };

  const onRetrySlackInvite = async () => {
    if (!token || !candidate) return;
    setIsRetryingSlackInvite(true);
    setError("");
    try {
      const updated = await retrySlackInvite(candidate.id, token);
      setCandidate(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to retry Slack invite");
    } finally {
      setIsRetryingSlackInvite(false);
    }
  };

  const onSetFirefliesDemo = async (mockCompleted: boolean) => {
    if (!token || !candidate) return;
    setIsApplyingFirefliesDemo(true);
    setError("");
    try {
      const payload: FirefliesDemoRequest = { mock_completed: mockCompleted };
      const updated = await setFirefliesDemoState(candidate.id, token, payload);
      setCandidate(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to apply Fireflies demo state");
    } finally {
      setIsApplyingFirefliesDemo(false);
    }
  };

  const onSelectionDetailsChange = (key: keyof ManagerSelectionDetails, value: string) => {
    setSelectionDetails((prev) => ({ ...prev, [key]: value }));
  };

  const onUpdateApplicantStatus = async (nextStatus: "shortlisted" | "rejected") => {
    if (!token || !candidate) return;
    setIsUpdatingApplicantStatus(true);
    setError("");
    try {
      const updated = await updateApplicantStatus(candidate.id, token, {
        applicant_status: nextStatus,
        note: statusDecisionNote.trim() || null,
      });
      setCandidate(updated);
      if (nextStatus === "shortlisted" || nextStatus === "rejected") {
        setStatusDecisionNote("");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update candidate status");
    } finally {
      setIsUpdatingApplicantStatus(false);
    }
  };

  const onSubmitDecision = async () => {
    if (!token || !candidate) return;
    if (!canSubmitManagerDecision) {
      setError("Manager decision is allowed only when interview_schedule_status is interview_done.");
      return;
    }
    setIsSubmittingDecision(true);
    setError("");
    try {
      if (decision === "reject") {
        const updated = await submitManagerDecision(candidate.id, token, {
          decision: "reject",
          note: decisionNote || null,
          selection_details: null,
        });
        setCandidate(updated);
      } else {
        const required = [
          selectionDetails.confirmed_job_title,
          selectionDetails.start_date,
          selectionDetails.base_salary,
          selectionDetails.compensation_structure,
          selectionDetails.reporting_manager,
        ];
        if (required.some((item) => !item || !item.trim())) {
          throw new Error("Please fill all required approved-offer fields before generating letter.");
        }

        const updated = await submitManagerDecision(candidate.id, token, {
          decision: "select",
          note: decisionNote || null,
          selection_details: {
            confirmed_job_title: selectionDetails.confirmed_job_title.trim(),
            start_date: selectionDetails.start_date,
            base_salary: selectionDetails.base_salary.trim(),
            compensation_structure: selectionDetails.compensation_structure.trim(),
            equity_or_bonus: selectionDetails.equity_or_bonus?.trim() || null,
            reporting_manager: selectionDetails.reporting_manager.trim(),
            custom_terms: selectionDetails.custom_terms?.trim() || null,
          },
        });
        setCandidate(updated);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to submit manager decision");
    } finally {
      setIsSubmittingDecision(false);
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
          <strong>Interview Link:</strong>{" "}
          {confirmedMeetingLink ? (
            <a href={confirmedMeetingLink} target="_blank" rel="noreferrer">
              {confirmedMeetingLink}
            </a>
          ) : (
            "-"
          )}
        </p>
        <p>
          <strong>Transcript Status:</strong> {candidate.interview_transcript_status || "-"}
        </p>
        <p>
          <strong>Interview Email Sent At:</strong>{" "}
          {candidate.interview_schedule_sent_at
            ? new Date(candidate.interview_schedule_sent_at).toLocaleString()
            : "-"}
        </p>
        <p>
          <strong>Transcript Synced At:</strong>{" "}
          {candidate.interview_transcript_synced_at
            ? new Date(candidate.interview_transcript_synced_at).toLocaleString()
            : "-"}
        </p>
      </section>

      {canManualScreeningDecision ? (
        <section className="panel stack">
          <h2>Human Screening Decision</h2>
          <p className="muted">
            Manual review candidate. Choose whether to move ahead or send rejection.
          </p>
          <label>
            Decision Note (optional)
            <textarea
              value={statusDecisionNote}
              onChange={(event) => setStatusDecisionNote(event.target.value)}
              rows={3}
            />
          </label>
          <div className="row">
            <button
              type="button"
              onClick={() => onUpdateApplicantStatus("shortlisted")}
              disabled={isUpdatingApplicantStatus}
            >
              {isUpdatingApplicantStatus ? "Updating..." : "Move Ahead"}
            </button>
            <button
              type="button"
              className="danger"
              onClick={() => onUpdateApplicantStatus("rejected")}
              disabled={isUpdatingApplicantStatus}
            >
              {isUpdatingApplicantStatus ? "Updating..." : "Don't Move Ahead"}
            </button>
          </div>
        </section>
      ) : null}

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
        <div className="resume-card">
          <div className="resume-preview-box" aria-label="Resume file preview">
            <span>{candidate.resume.original_filename || "resume.pdf"}</span>
          </div>
          <div className="stack-tight">
            <p>
              <strong>File:</strong> {candidate.resume.original_filename || "-"}
            </p>
            <p>
              <strong>Type:</strong> {candidate.resume.content_type || "-"}
            </p>
            <p>
              <strong>Size:</strong> {formatFileSize(candidate.resume.size_bytes)}
            </p>
          </div>
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
        <h2>Research Flags and Evidence</h2>
        {parsedResearch?.parseError ? (
          <p className="muted">
            Research summary exists but is in legacy text format. Showing raw summary below.
          </p>
        ) : null}
        {parsedResearch?.legacySummary ? (
          <p>{parsedResearch.legacySummary}</p>
        ) : null}
        <div className="research-summary-grid">
          <div className="research-summary-card">
            <p className="research-summary-label">Confidence</p>
            <p className="research-summary-value">{parsedResearch?.confidence || "-"}</p>
          </div>
          <div className="research-summary-card">
            <p className="research-summary-label">Manual Review Required</p>
            <p className="research-summary-value">
              {typeof parsedResearch?.manualReviewRequired === "boolean"
                ? parsedResearch.manualReviewRequired
                  ? "Yes"
                  : "No"
                : "-"}
            </p>
          </div>
          <div className="research-summary-card">
            <p className="research-summary-label">High Flags</p>
            <p className="research-summary-value">{issueCounts.high}</p>
          </div>
          <div className="research-summary-card">
            <p className="research-summary-label">Medium Flags</p>
            <p className="research-summary-value">{issueCounts.medium}</p>
          </div>
        </div>
        {parsedResearch?.deterministicNotes?.length ? (
          <div>
            <strong>Deterministic Notes:</strong>
            <ul>
              {parsedResearch.deterministicNotes.map((item, idx) => (
                <li key={`${idx}-${item.slice(0, 24)}`}>{item}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {issueFlags.length ? (
          <div id="research-issue-flags">
            <strong>Issue Flags:</strong>
            <div className="research-issues-grid">
              {issueFlags.map((item, idx) => {
                const detailText = String(item.details || "").trim() || "No details provided.";
                const pretty = tryPrettyJson(detailText);
                const preview = summarizeText(pretty || detailText, 220);
                return (
                  <article
                    className="research-issue-card"
                    key={`${idx}-${item.type}-${item.severity}-${item.source}`.slice(0, 80)}
                  >
                    <div className="research-issue-head">
                      <span className={severityBadgeClass(item.severity)}>
                        {normalizeSeverity(item.severity).toUpperCase()}
                      </span>
                      <p className="research-issue-title">{formatIssueType(item.type)}</p>
                      <p className="research-issue-source">{item.source}</p>
                    </div>
                    <p className="research-issue-preview">{preview}</p>
                    {detailText.length > preview.length ? (
                      <details className="research-issue-details">
                        <summary>View full evidence</summary>
                        <pre>{pretty || detailText}</pre>
                      </details>
                    ) : null}
                  </article>
                );
              })}
            </div>
          </div>
        ) : (
          <p className="muted">No issue flags available yet.</p>
        )}
        {parsedResearch?.discrepancies?.length ? (
          <div id="research-discrepancies">
            <strong>Discrepancies:</strong>
            <ul>
              {parsedResearch.discrepancies.map((item, idx) => (
                <li key={`${idx}-${item.slice(0, 30)}`}>{item}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {parsedResearch?.provenance?.length ? (
          <div id="research-flags-evidence">
            <strong>Claim Provenance:</strong>
            <ul>
              {parsedResearch.provenance.map((item, idx) => (
                <li key={`${idx}-${item.claim.slice(0, 24)}`}>
                  <p>{item.claim}</p>
                  <p className="muted">
                    {item.evidenceRefs.map((ref, refIdx) => {
                      const href = evidenceRefHref(ref);
                      const isExternal = href.startsWith("http://") || href.startsWith("https://");
                      return (
                        <span key={`${refIdx}-${ref}`}>
                          <a
                            href={href}
                            target={isExternal ? "_blank" : undefined}
                            rel={isExternal ? "noreferrer" : undefined}
                          >
                            {ref}
                          </a>
                          {refIdx < item.evidenceRefs.length - 1 ? ", " : ""}
                        </span>
                      );
                    })}
                  </p>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {parsedResearch?.sourceLinks?.length ? (
          <div id="research-source-links">
            <strong>Source Links:</strong>
            <ul>
              {parsedResearch.sourceLinks.map((item, idx) => (
                <li key={`${idx}-${item.url}`}>
                  <a href={item.url} target="_blank" rel="noreferrer">
                    {item.label}
                  </a>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </section>

      <section className="panel stack">
        <h2>Manager Decision</h2>
        {!canSubmitManagerDecision ? (
          <p className="muted">
            Manager decision is locked until interview is completed and{" "}
            <code>interview_schedule_status</code> becomes <code>interview_done</code>.
          </p>
        ) : null}
        <div className="row">
          <label>
            <input
              type="radio"
              name="manager_decision"
              checked={decision === "select"}
              onChange={() => setDecision("select")}
              disabled={!canSubmitManagerDecision}
            />{" "}
            Approve
          </label>
          <label>
            <input
              type="radio"
              name="manager_decision"
              checked={decision === "reject"}
              onChange={() => setDecision("reject")}
              disabled={!canSubmitManagerDecision}
            />{" "}
            Reject
          </label>
        </div>

        {decision === "select" ? (
          <div className="form-grid">
            <label>
              Confirmed Job Title
                <input
                  value={selectionDetails.confirmed_job_title}
                  onChange={(event) =>
                    onSelectionDetailsChange("confirmed_job_title", event.target.value)
                  }
                  disabled={!canSubmitManagerDecision}
                  required
                />
              </label>
            <label>
              Start Date
                <input
                  type="date"
                  value={selectionDetails.start_date}
                  onChange={(event) => onSelectionDetailsChange("start_date", event.target.value)}
                  disabled={!canSubmitManagerDecision}
                  required
                />
              </label>
            <label>
              Base Salary
                <input
                  value={selectionDetails.base_salary}
                  onChange={(event) => onSelectionDetailsChange("base_salary", event.target.value)}
                  disabled={!canSubmitManagerDecision}
                  required
                />
              </label>
            <label>
              Compensation Structure
                <input
                  value={selectionDetails.compensation_structure}
                  onChange={(event) =>
                    onSelectionDetailsChange("compensation_structure", event.target.value)
                  }
                  disabled={!canSubmitManagerDecision}
                  required
                />
              </label>
            <label>
              Equity / Bonus (optional)
                <input
                  value={selectionDetails.equity_or_bonus || ""}
                  onChange={(event) => onSelectionDetailsChange("equity_or_bonus", event.target.value)}
                  disabled={!canSubmitManagerDecision}
                />
              </label>
            <label>
              Reporting Manager
                <input
                  value={selectionDetails.reporting_manager}
                  onChange={(event) =>
                    onSelectionDetailsChange("reporting_manager", event.target.value)
                  }
                  disabled={!canSubmitManagerDecision}
                  required
                />
              </label>
            <label className="full">
              Custom Terms / Conditions (optional)
                <textarea
                  value={selectionDetails.custom_terms || ""}
                  onChange={(event) => onSelectionDetailsChange("custom_terms", event.target.value)}
                  disabled={!canSubmitManagerDecision}
                  rows={4}
                />
              </label>
          </div>
        ) : null}

        <label>
          Decision Note (optional)
          <textarea
            value={decisionNote}
            onChange={(event) => setDecisionNote(event.target.value)}
            disabled={!canSubmitManagerDecision}
            rows={3}
          />
        </label>

        <div className="row">
          <button
            type="button"
            onClick={onSubmitDecision}
            disabled={isSubmittingDecision || !canSubmitManagerDecision}
          >
            {isSubmittingDecision
              ? "Submitting..."
              : decision === "select"
                ? "Generate Offer Letter"
                : "Reject Candidate"}
          </button>
        </div>
      </section>

      <section className="panel stack">
        <h2>Offer Letter</h2>
        <p>
          <strong>Offer Letter Status:</strong> {candidate.offer_letter_status || "-"}
        </p>
        <p>
          <strong>Generated At:</strong>{" "}
          {candidate.offer_letter_generated_at
            ? new Date(candidate.offer_letter_generated_at).toLocaleString()
            : "-"}
        </p>
        <p>
          <strong>Sent At:</strong>{" "}
          {candidate.offer_letter_sent_at
            ? new Date(candidate.offer_letter_sent_at).toLocaleString()
            : "-"}
        </p>
        <p>
          <strong>Signed At:</strong>{" "}
          {candidate.offer_letter_signed_at
            ? new Date(candidate.offer_letter_signed_at).toLocaleString()
            : "-"}
        </p>
        <p>
          <strong>DocuSign Envelope ID:</strong> {candidate.docusign_envelope_id || "-"}
        </p>
        <p>
          <strong>Slack Invite Status:</strong> {candidate.slack_invite_status || "-"}
        </p>
        <p>
          <strong>Slack User ID:</strong> {candidate.slack_user_id || "-"}
        </p>
        <p>
          <strong>Slack Joined At:</strong>{" "}
          {candidate.slack_joined_at ? new Date(candidate.slack_joined_at).toLocaleString() : "-"}
        </p>
        <p>
          <strong>Slack Onboarding Status:</strong> {candidate.slack_onboarding_status || "-"}
        </p>
        <div className="row">
          <button
            type="button"
            onClick={onDownloadOfferLetter}
            disabled={isDownloadingOfferLetter || !candidate.offer_letter_storage_path}
          >
            {isDownloadingOfferLetter ? "Preparing offer letter..." : "Download Offer Letter"}
          </button>
          <button
            type="button"
            onClick={onApproveOfferLetter}
            disabled={isApprovingOfferLetter || candidate.offer_letter_status !== "created"}
          >
            {isApprovingOfferLetter
              ? "Sending for e-signature..."
              : "Send for eSignature"}
          </button>
          <button
            type="button"
            onClick={onSyncOfferSignature}
            disabled={
              isSyncingSignature ||
              !candidate.docusign_envelope_id ||
              candidate.offer_letter_status === "signed"
            }
          >
            {isSyncingSignature ? "Syncing..." : "Refresh Signature Status"}
          </button>
          <button
            type="button"
            onClick={onRetrySlackInvite}
            disabled={
              isRetryingSlackInvite ||
              candidate.offer_letter_status !== "signed" ||
              candidate.slack_invite_status === "invited" ||
              candidate.slack_invite_status === "already_in_workspace"
            }
          >
            {isRetryingSlackInvite ? "Retrying..." : "Retry Slack Invite"}
          </button>
        </div>
      </section>

      <section className="panel stack">
        <h2>Interview Transcript (Fireflies)</h2>
        <p className="muted">
          Fireflies transcript usually becomes available after interview completion.
        </p>
        <div className="row">
          <button
            type="button"
            className="secondary"
            disabled={isApplyingFirefliesDemo}
            onClick={() => onSetFirefliesDemo(true)}
          >
            {isApplyingFirefliesDemo ? "Applying..." : "Mock TRUE (Interview Done)"}
          </button>
          <button
            type="button"
            className="secondary"
            disabled={isApplyingFirefliesDemo}
            onClick={() => onSetFirefliesDemo(false)}
          >
            {isApplyingFirefliesDemo ? "Applying..." : "Mock FALSE (Transcript Pending)"}
          </button>
        </div>
        {!fireflies ? (
          <p className="muted">Transcript sync is not available for this interview yet.</p>
        ) : (
          <>
            <p>
              <strong>Sync Status:</strong> {String(fireflies.status || "-")}
            </p>
            <p>
              <strong>Transcript Link:</strong>{" "}
              {candidate.interview_transcript_url ? (
                <a href={candidate.interview_transcript_url} target="_blank" rel="noreferrer">
                  {candidate.interview_transcript_url}
                </a>
              ) : firefliesTranscript && typeof firefliesTranscript.url === "string" ? (
                <a href={firefliesTranscript.url} target="_blank" rel="noreferrer">
                  {firefliesTranscript.url}
                </a>
              ) : (
                "-"
              )}
            </p>
            <p>
              <strong>Summary:</strong>{" "}
              {candidate.interview_transcript_summary ||
                (firefliesTranscript && typeof firefliesTranscript.summary === "string"
                  ? firefliesTranscript.summary
                  : "-")}
            </p>
            {firefliesActionItems.length > 0 ? (
              <div>
                <strong>Action Items:</strong>
                <ul>
                  {firefliesActionItems.map((item, idx) => (
                    <li key={`${idx}-${item.slice(0, 16)}`}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </>
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
