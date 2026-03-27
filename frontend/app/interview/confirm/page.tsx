"use client";

import { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { confirmInterviewSlotByToken, readApiError } from "../../../lib/api";

type ConfirmState =
  | { kind: "idle"; message: string }
  | { kind: "loading"; message: string }
  | { kind: "success"; message: string; meetingLink?: string | null }
  | { kind: "error"; message: string };

type UiMessage = {
  title: string;
  body: string;
  hint?: string;
};

function normalizeConfirmMessage(state: ConfirmState): UiMessage {
  if (state.kind === "idle") {
    return {
      title: "Confirm your interview slot",
      body: "Review this link and click confirm to finalize your interview booking.",
    };
  }
  if (state.kind === "loading") {
    return {
      title: "Confirming your interview",
      body: "We are locking your selected slot and finalizing your calendar booking.",
    };
  }
  if (state.kind === "success") {
    return {
      title: "Interview confirmed",
      body: "Your slot is finalized. A calendar invite has been sent.",
      hint: state.meetingLink
        ? "You can join directly from the meeting link below."
        : "You can also join from your calendar event.",
    };
  }

  const raw = state.message.toLowerCase();
  if (raw.includes("being confirmed by another request")) {
    return {
      title: "Confirmation in progress",
      body: "This slot is already being processed. Please wait a few seconds and refresh the page.",
      hint: "If the issue persists, open the latest link from your email.",
    };
  }
  if (raw.includes("expired")) {
    return {
      title: "This link expired",
      body: "The interview hold window has expired.",
      hint: "Please use the newest scheduling email to select another slot.",
    };
  }
  if (raw.includes("not confirmable")) {
    return {
      title: "Slot already finalized",
      body: "This interview slot is already booked or finalized.",
      hint: "Please check your latest scheduling email for the current status.",
    };
  }
  if (raw.includes("failed to fetch") || raw.includes("networkerror")) {
    return {
      title: "Unable to reach confirmation service",
      body: "We could not contact the backend to verify this link right now.",
      hint:
        "If this slot was already booked, use your latest email link to view current status. Otherwise, retry in a moment.",
    };
  }
  if (raw.includes("missing confirmation token")) {
    return {
      title: "Invalid confirmation link",
      body: "This page was opened without a valid confirmation token.",
      hint: "Please open the confirmation link directly from your email.",
    };
  }
  return {
    title: "Unable to confirm interview",
    body: state.message,
    hint: "Please retry from your latest email link.",
  };
}

function InterviewConfirmContent() {
  const searchParams = useSearchParams();
  const token = searchParams.get("token") || "";
  const [state, setState] = useState<ConfirmState>({
    kind: "idle",
    message: "Click confirm to finalize your interview slot.",
  });

  useEffect(() => {
    if (!token) {
      setState({
        kind: "error",
        message: "Missing confirmation token. Please use the link sent in email.",
      });
      return;
    }
    setState({
      kind: "idle",
      message: "Click confirm to finalize your interview slot.",
    });
  }, [token]);

  const handleConfirmClick = async () => {
    if (!token) {
      setState({
        kind: "error",
        message: "Missing confirmation token. Please use the link sent in email.",
      });
      return;
    }
    setState({
      kind: "loading",
      message: "Confirming your selected interview slot...",
    });
    try {
      const response = await confirmInterviewSlotByToken(token);
      setState({
        kind: "success",
        message:
          "Thanks for the confirmation. Your interview slot is finalized. Best of luck for your technical round.",
        meetingLink: response.confirmed_meeting_link || response.confirmed_event_link || null,
      });
    } catch (error) {
      const message =
        error instanceof Error ? error.message : readApiError(null, "Interview confirmation failed.");
      setState({ kind: "error", message });
    }
  };

  const ui = normalizeConfirmMessage(state);

  return (
    <main className="stack">
      <section className="panel stack">
        <div className="stack-tight">
          <p className="status-label">Interview Confirmation</p>
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
          {state.kind !== "success" && token ? (
            <button
              type="button"
              className="cta-button"
              onClick={handleConfirmClick}
              disabled={state.kind === "loading"}
            >
              {state.kind === "loading" ? "Confirming..." : "Confirm This Interview Slot"}
            </button>
          ) : null}
          {state.kind === "error" ? (
            <button
              type="button"
              className="cta-button-secondary"
              onClick={handleConfirmClick}
              disabled={!token}
            >
              Retry Confirmation
            </button>
          ) : null}
          <Link href="/" className="badge">
            Back To HireMe
          </Link>
        </div>
      </section>
    </main>
  );
}

export default function InterviewConfirmPage() {
  return (
    <Suspense
      fallback={
        <main className="stack">
          <section className="panel stack">
            <div className="stack-tight">
              <p className="status-label">Interview Confirmation</p>
              <h1>Confirming your interview</h1>
            </div>
            <div className="status-card status-card-loading">
              <p>We are locking your selected slot and finalizing your calendar booking.</p>
            </div>
          </section>
        </main>
      }
    >
      <InterviewConfirmContent />
    </Suspense>
  );
}
