"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";

import { API_BASE, readApiError } from "../lib/api";

interface SubmitState {
  type: "idle" | "success" | "error";
  message: string;
}

interface JobOpeningCard {
  id: string;
  role_title: string;
  team: string;
  location: string;
  experience_level: string;
  responsibilities: string[];
  requirements: string[];
  status: string;
  paused: boolean;
}

export default function CandidateApplyPage() {
  const [roles, setRoles] = useState<string[]>([]);
  const [openings, setOpenings] = useState<JobOpeningCard[]>([]);
  const [isLoadingRoles, setIsLoadingRoles] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitState, setSubmitState] = useState<SubmitState>({
    type: "idle",
    message: "",
  });

  const loadOpeningsAndRoles = useCallback(async () => {
    const normalizeRoles = (raw: unknown): string[] => {
      if (!Array.isArray(raw)) return [];
      return Array.from(
        new Set(
          raw
            .filter((item): item is string => typeof item === "string")
            .map((item) => item.trim())
            .filter((item) => item.length > 0),
        ),
      ).sort((a, b) => a.localeCompare(b));
    };

    setIsLoadingRoles(true);
    try {
      const openingsResponse = await fetch(`${API_BASE}/api/v1/job-openings`, { cache: "no-store" });
      const openingsPayload = (await openingsResponse.json().catch(() => null)) as
        | { items?: JobOpeningCard[] }
        | null;
      const openItems = (openingsPayload?.items || [])
        .filter((item) => (item.status || "").toLowerCase() === "open" && !item.paused)
        .filter((item) => item.role_title.trim().length > 0);
      setOpenings(openItems);
      const openingRoles = Array.from(new Set(openItems.map((item) => item.role_title.trim()))).sort(
        (a, b) => a.localeCompare(b),
      );
      if (openingRoles.length > 0) {
        setRoles(openingRoles);
        return;
      }

      const rolesResponse = await fetch(`${API_BASE}/api/v1/roles`, { cache: "no-store" });
      const rolePayload = await rolesResponse.json().catch(() => null);
      setRoles(normalizeRoles(rolePayload));
    } catch {
      setOpenings([]);
      setRoles([]);
    } finally {
      setIsLoadingRoles(false);
    }
  }, []);

  useEffect(() => {
    loadOpeningsAndRoles();
  }, [loadOpeningsAndRoles]);

  useEffect(() => {
    if (roles.length > 0) return;
    const timer = window.setInterval(() => {
      void loadOpeningsAndRoles();
    }, 10000);
    return () => window.clearInterval(timer);
  }, [loadOpeningsAndRoles, roles.length]);

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const formData = new FormData(form);

    setIsSubmitting(true);
    setSubmitState({ type: "idle", message: "" });

    try {
      const response = await fetch(`${API_BASE}/api/v1/applications`, {
        method: "POST",
        body: formData,
      });

      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        const apiMessage = readApiError(payload, "Application submission failed");
        const limitMatch = apiMessage.match(/max allowed is\s+(\d+)\s+MB/i);
        const duplicateMatch = /duplicate application/i.test(apiMessage);
        if (limitMatch) {
          throw new Error(`File size should be less than ${limitMatch[1]} MB.`);
        }
        if (duplicateMatch) {
          throw new Error(
            "You have already submitted an application for this role with this email.",
          );
        }
        throw new Error(apiMessage);
      }

      form.reset();
      setSubmitState({
        type: "success",
        message: "Thank you for your application. We have received it successfully.",
      });
    } catch (error) {
      setSubmitState({
        type: "error",
        message: error instanceof Error ? error.message : "Submission failed",
      });
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <main className="stack">
      <div className="page-head">
        <h1>Candidate Application</h1>
        <span className="badge">Fast submission</span>
      </div>

      <section className="panel stack">
        <h2>Open Roles</h2>
        <p className="muted">
          Each role includes team, location, experience level, responsibilities, and requirements.
        </p>
        <div className="opening-grid">
          {openings.map((opening) => (
            <article key={opening.id} className="opening-card stack-tight">
              <h3>{opening.role_title}</h3>
              <p className="muted">
                {opening.team} · {opening.location} · {opening.experience_level}
              </p>
              <div>
                <strong>Responsibilities</strong>
                <ul>
                  {opening.responsibilities.map((item) => (
                    <li key={`${opening.id}-resp-${item}`}>{item}</li>
                  ))}
                </ul>
              </div>
              <div>
                <strong>Requirements</strong>
                <ul>
                  {opening.requirements.map((item) => (
                    <li key={`${opening.id}-req-${item}`}>{item}</li>
                  ))}
                </ul>
              </div>
            </article>
          ))}
        </div>
        {!isLoadingRoles && openings.length < 3 ? (
          <p className="muted">
            Fewer than 3 open roles are currently active. Admin can publish more openings in dashboard.
          </p>
        ) : null}
      </section>

      <section className="panel stack">
        <h2>Apply To A Role</h2>
        <p className="muted">
          Select an available position from the dropdown and upload your resume (PDF/DOC/DOCX).
          File size limits are enforced by backend config.
        </p>

        <form className="stack" onSubmit={onSubmit}>
          <div className="form-grid">
            <label>
              Full Name
              <input name="full_name" required minLength={2} maxLength={120} />
            </label>

            <label>
              Email
              <input name="email" type="email" required />
            </label>

            <label>
              LinkedIn URL
              <input name="linkedin_url" type="url" required />
            </label>

            <label>
              Portfolio URL (optional)
              <input name="portfolio_url" type="url" />
            </label>

            <label>
              GitHub URL (optional)
              <input name="github_url" type="url" />
            </label>

            <label className="full">
              Position (scroll/select)
              <select name="role_selection" required disabled={isLoadingRoles || roles.length === 0}>
                <option value="">Select a role</option>
                {roles.map((role) => (
                  <option key={role} value={role}>
                    {role}
                  </option>
                ))}
              </select>
              {isLoadingRoles ? <small className="muted">Loading roles...</small> : null}
            </label>

            <label className="full">
              Resume
              <input
                name="resume"
                type="file"
                accept=".pdf,.doc,.docx,application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                required
              />
            </label>
          </div>

          <button type="submit" disabled={isSubmitting}>
            {isSubmitting ? "Submitting..." : "Submit Application"}
          </button>

          {submitState.type === "success" ? <p className="ok">{submitState.message}</p> : null}
          {submitState.type === "error" ? <p className="error">{submitState.message}</p> : null}
        </form>
      </section>

    </main>
  );
}
