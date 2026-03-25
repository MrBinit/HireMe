"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";

import { API_BASE, readApiError } from "../../lib/api";

export default function AdminLoginPage() {
  const router = useRouter();
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState("");

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const formData = new FormData(event.currentTarget);
    const username = String(formData.get("username") || "");
    const password = String(formData.get("password") || "");

    setIsSubmitting(true);
    setError("");
    try {
      const response = await fetch(`${API_BASE}/api/v1/admin/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(readApiError(payload, "Admin login failed"));
      }

      const token = payload?.access_token;
      if (!token || typeof token !== "string") {
        throw new Error("Invalid login response from backend");
      }
      localStorage.setItem("hireme_admin_token", token);
      router.push("/admin/dashboard");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Admin login failed");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <main className="stack">
      <h1>Admin Login</h1>
      <section className="panel stack">
        <form className="stack" onSubmit={onSubmit}>
          <label>
            Username
            <input name="username" required minLength={3} />
          </label>
          <label>
            Password
            <input name="password" type="password" required minLength={8} />
          </label>
          <button type="submit" disabled={isSubmitting}>
            {isSubmitting ? "Signing in..." : "Sign In"}
          </button>
          {error ? <p className="error">{error}</p> : null}
        </form>
      </section>
    </main>
  );
}
