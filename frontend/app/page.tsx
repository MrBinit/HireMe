"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";

import { API_BASE, readApiError } from "../lib/api";

interface SubmitState {
  type: "idle" | "success" | "error";
  message: string;
}

export default function CandidateApplyPage() {
  const [roles, setRoles] = useState<string[]>([]);
  const [isLoadingRoles, setIsLoadingRoles] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitState, setSubmitState] = useState<SubmitState>({
    type: "idle",
    message: "",
  });

  const loadRoles = useCallback(async () => {
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
      const rolesResponse = await fetch(`${API_BASE}/api/v1/roles`, { cache: "no-store" });
      const rolePayload = await rolesResponse.json().catch(() => null);
      const directRoles = normalizeRoles(rolePayload);

      if (directRoles.length > 0) {
        setRoles(directRoles);
        return;
      }

      const openingsResponse = await fetch(`${API_BASE}/api/v1/job-openings`, { cache: "no-store" });
      const openingsPayload = (await openingsResponse.json().catch(() => null)) as
        | { items?: Array<{ role_title?: string; status?: string }> }
        | null;

      const openingRoles = Array.from(
        new Set(
          (openingsPayload?.items || [])
            .filter((item) => (item.status || "").toLowerCase() === "open")
            .map((item) => (item.role_title || "").trim())
            .filter((item) => item.length > 0),
        ),
      ).sort((a, b) => a.localeCompare(b));

      setRoles(openingRoles);
    } catch {
      setRoles([]);
    } finally {
      setIsLoadingRoles(false);
    }
  }, []);

  useEffect(() => {
    loadRoles();
  }, [loadRoles]);

  useEffect(() => {
    if (roles.length > 0) return;
    const timer = window.setInterval(() => {
      void loadRoles();
    }, 10000);
    return () => window.clearInterval(timer);
  }, [loadRoles, roles.length]);

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
        message: "Application submitted successfully.",
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
              GitHub URL
              <input name="github_url" type="url" required />
            </label>

            <label>
              Twitter URL (optional)
              <input name="twitter_url" type="url" />
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
