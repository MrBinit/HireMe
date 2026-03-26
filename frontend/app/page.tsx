"use client";

import { FormEvent, useEffect, useState } from "react";

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

  useEffect(() => {
    let isActive = true;

    const loadRoles = async () => {
      try {
        const response = await fetch(`${API_BASE}/api/v1/roles`, {
          cache: "no-store",
        });
        const payload = (await response.json().catch(() => [])) as string[];
        if (isActive && Array.isArray(payload)) {
          setRoles(payload);
        }
      } catch {
        if (isActive) {
          setRoles([]);
        }
      } finally {
        if (isActive) {
          setIsLoadingRoles(false);
        }
      }
    };

    loadRoles();

    return () => {
      isActive = false;
    };
  }, []);

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
