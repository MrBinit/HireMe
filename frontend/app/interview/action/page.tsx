"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { processInterviewActionByToken, readApiError } from "../../../lib/api";

type ActionState =
  | { kind: "loading"; message: string }
  | { kind: "success"; message: string; meetingLink?: string | null }
  | { kind: "error"; message: string };

export default function InterviewActionPage() {
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

  return (
    <main className="stack">
      <section className="panel stack">
        <h1>Interview Action</h1>
        {state.kind === "loading" ? <p className="muted">{state.message}</p> : null}
        {state.kind === "success" ? (
          <>
            <p className="ok">{state.message}</p>
            {state.meetingLink ? (
              <p>
                Meeting link:{" "}
                <a href={state.meetingLink} target="_blank" rel="noreferrer">
                  {state.meetingLink}
                </a>
              </p>
            ) : null}
          </>
        ) : null}
        {state.kind === "error" ? <p className="error">{state.message}</p> : null}
        <div className="row">
          <Link href="/" className="badge">
            Back To HireMe
          </Link>
        </div>
      </section>
    </main>
  );
}
