"use client";

import { FormEvent, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { API_BASE, readApiError } from "../../../lib/api";

export default function RefereeDashboardPage() {
  const router = useRouter();
  const [token, setToken] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

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

  const onLogout = () => {
    localStorage.removeItem("hireme_referee_token");
    router.push("/referee");
  };

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!token) return;
    const formData = new FormData(event.currentTarget);

    const payload = {
      applicant_email: String(formData.get("applicant_email") || "").trim(),
      applicant_name: String(formData.get("applicant_name") || "").trim(),
      applicant_position: String(formData.get("applicant_position") || "").trim(),
      referee_name: String(formData.get("referee_name") || "").trim(),
      referee_email: String(formData.get("referee_email") || "").trim(),
      referee_note: String(formData.get("referee_note") || "").trim(),
    };

    setIsSubmitting(true);
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
        throw new Error(readApiError(body, "Failed to submit referral"));
      }

      setMessage("Reference submitted successfully.");
      event.currentTarget.reset();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to submit referral");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <main className="stack">
      <div className="page-head">
        <h1>Referrals</h1>
        <div className="row">
          <Link href="/" className="badge">
            Home
          </Link>
          <button type="button" className="secondary" onClick={onLogout}>
            Logout
          </button>
        </div>
      </div>

      <section className="panel stack">
        <h2>Submit Referral</h2>
        <p className="muted">
          Enter referee details and applicant details. If no applicant exists with that email, you
          will see: &quot;Sorry, no applicant found with this email.&quot;
        </p>
        <form className="stack" onSubmit={onSubmit}>
          <label>
            Applicant Email
            <input name="applicant_email" type="email" required />
          </label>
          <label>
            Applicant Name
            <input name="applicant_name" required minLength={2} />
          </label>
          <label>
            Applicant Position
            <input name="applicant_position" required minLength={2} />
          </label>
          <label>
            Referee Name
            <input name="referee_name" required minLength={2} />
          </label>
          <label>
            Referee Email
            <input name="referee_email" type="email" required />
          </label>
          <label>
            Referee Note
            <textarea name="referee_note" rows={4} maxLength={1000} />
          </label>

          <button type="submit" disabled={isSubmitting}>
            {isSubmitting ? "Submitting..." : "Submit Referral"}
          </button>
        </form>
        {message ? <p className="ok">{message}</p> : null}
        {error ? <p className="error">{error}</p> : null}
      </section>
    </main>
  );
}
