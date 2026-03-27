"use client";

import { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { processInterviewActionByToken, readApiError } from "../../../lib/api";

type ActionState =
  | { kind: "loading"; message: string }
  | { kind: "success"; message: string; meetingLink?: string | null }
  | { kind: "error"; message: string };

type UiMessage = {
  title: string;
  body: string;
  hint?: string;
};

function normalizeActionMessage(state: ActionState): UiMessage {
  if (state.kind === "loading") {
    return {
      title: "Processing request",
      body: "We are applying your interview action now.",
    };
  }
  if (state.kind === "success") {
    return {
      title: "Action completed",
      body: state.message,
      hint: state.meetingLink
        ? "The latest meeting link is available below."
        : "You can check email/calendar for updated interview details.",
    };
  }

  const raw = state.message.toLowerCase();
  if (raw.includes("outdated")) {
    return {
      title: "Action link is outdated",
      body: "This action was already processed or replaced by a newer option set.",
      hint: "Please use the latest email link.",
    };
  }
  if (raw.includes("expired")) {
    return {
      title: "Action link expired",
      body: "This action token is no longer valid.",
      hint: "Use the newest scheduling email to continue.",
    };
  }
  if (raw.includes("missing action token")) {
    return {
      title: "Invalid action link",
      body: "This page was opened without a valid action token.",
      hint: "Open the action link directly from your email.",
    };
  }
  return {
    title: "Unable to process action",
    body: state.message,
    hint: "Please retry from your latest email link.",
  };
}

function InterviewActionContent() {
  const searchParams = useSearchParams();
  const token = searchParams.get("token") || "";
  const [state, setState] = useState<ActionState>({
    kind: "loading",
    message: "Processing interview action...",
  });

  useEffect(() => {
    let isMounted = true;

    const run = async () => {
      if (!token) {
        if (isMounted) {
          setState({
            kind: "error",
            message: "Missing action token. Please use the email CTA link.",
          });
        }
        return;
      }
      try {
        const response = await processInterviewActionByToken(token);
        if (!isMounted) {
          return;
        }
        setState({
          kind: "success",
          message: response.message,
          meetingLink: response.confirmed_meeting_link || response.confirmed_event_link || null,
        });
      } catch (error) {
        if (!isMounted) {
          return;
        }
        const message =
          error instanceof Error
            ? error.message
            : readApiError(null, "Interview action failed.");
        setState({ kind: "error", message });
      }
    };

    run();
    return () => {
      isMounted = false;
    };
  }, [token]);

  const ui = normalizeActionMessage(state);

  return (
    <main className="stack">
      <section className="panel stack">
        <div className="stack-tight">
          <p className="status-label">Interview Action</p>
          <h1>{ui.title}</h1>
        </div>
        <div
          className={`status-card ${
            state.kind === "success"
              ? "status-card-success"
              : state.kind === "error"
                ? "status-card-error"
                : "status-card-loading"
          }`}
        >
          <p>{ui.body}</p>
          {ui.hint ? <p className="muted">{ui.hint}</p> : null}
        </div>
        {state.kind === "success" ? (
          <>
            {state.meetingLink ? (
              <div className="row">
                <a className="cta-button" href={state.meetingLink} target="_blank" rel="noreferrer">
                  Open Meeting Link
                </a>
              </div>
            ) : null}
          </>
        ) : null}
        <div className="row">
          <Link href="/" className="badge">
            Back To HireMe
          </Link>
        </div>
      </section>
    </main>
  );
}

export default function InterviewActionPage() {
  return (
    <Suspense
      fallback={
        <main className="stack">
          <section className="panel stack">
            <div className="stack-tight">
              <p className="status-label">Interview Action</p>
              <h1>Processing request</h1>
            </div>
            <div className="status-card status-card-loading">
              <p>We are applying your interview action now.</p>
            </div>
          </section>
        </main>
      }
    >
      <InterviewActionContent />
    </Suspense>
  );
}
