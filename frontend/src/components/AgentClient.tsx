"use client";

import { useState } from "react";

import { Callout } from "@/components/ui";

const SUGGESTIONS = [
  "How is the agent doing overall?",
  "What was the slowest stage in the recent runs?",
  "Which events fell back to the deterministic template?",
  "How many LLM calls and tokens did the recent runs use?",
  "Were there any errors or failures?",
];

interface AgentChatResponse {
  question: string;
  answer: string;
  trace_count: number;
  available: boolean;
  note: string | null;
}

type Status = "idle" | "loading" | "done" | "error";

export function AgentClient() {
  const [question, setQuestion] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const [result, setResult] = useState<AgentChatResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function ask(query: string) {
    const trimmed = query.trim();
    if (!trimmed || status === "loading") return;
    setQuestion(trimmed);
    setStatus("loading");
    setError(null);
    setResult(null);
    try {
      const response = await fetch("/api/agent", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: trimmed }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Something went wrong. Try again.");
        setStatus("error");
        return;
      }
      setResult(data as AgentChatResponse);
      setStatus("done");
    } catch {
      setError("Could not reach the analysis service.");
      setStatus("error");
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-5 py-12 sm:px-8 sm:py-16">
      <p className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
        How&rsquo;s the agent doing?
      </p>
      <h1 className="mt-2 font-display text-3xl tracking-tight text-balance text-paper sm:text-4xl">
        Ask about the pipeline, not just the data
      </h1>
      <p className="mt-3 text-[0.9375rem] leading-relaxed text-paper-muted">
        Questions here are answered from the MLflow traces each scheduled run produces — latency,
        failures, token usage, and which events fell back to the template.
      </p>

      <form
        className="mt-8"
        onSubmit={(event) => {
          event.preventDefault();
          void ask(question);
        }}
      >
        <div className="flex flex-col gap-2 sm:flex-row">
          <input
            type="text"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="e.g. What was the slowest stage in the recent runs?"
            aria-label="Your question"
            className="min-w-0 flex-1 rounded-lg border border-ink-700 bg-ink-900 px-4 py-3 text-[0.9375rem] text-paper placeholder:text-paper-faint focus:border-accent-dim focus:outline-none"
          />
          <button
            type="submit"
            disabled={!question.trim() || status === "loading"}
            className="rounded-lg bg-accent px-5 py-3 text-sm font-semibold text-ink-950 transition-colors hover:bg-accent-bright disabled:cursor-not-allowed disabled:opacity-50"
          >
            {status === "loading" ? "Analyzing…" : "Ask"}
          </button>
        </div>
      </form>

      {status === "idle" && (
        <div className="mt-8">
          <p className="text-xs text-paper-faint">Or start from one of these:</p>
          <div className="mt-3 flex flex-wrap gap-2">
            {SUGGESTIONS.map((suggestion) => (
              <button
                key={suggestion}
                type="button"
                onClick={() => void ask(suggestion)}
                className="rounded-lg border border-ink-700 px-3.5 py-2 text-sm text-paper-dim transition-colors hover:border-accent-dim hover:text-accent-bright"
              >
                {suggestion}
              </button>
            ))}
          </div>
        </div>
      )}

      {status === "loading" && (
        <div className="mt-10 rounded-lg border border-ink-700/70 bg-ink-850/40 p-6">
          <p className="text-sm text-paper-muted">Reading the recent traces…</p>
          <div className="mt-4 h-1 w-full overflow-hidden rounded-full bg-ink-800">
            <div className="h-full w-1/2 animate-pulse rounded-full bg-accent-dim" />
          </div>
        </div>
      )}

      {status === "error" && (
        <div className="mt-8">
          <Callout tone="danger" title="That did not go through">
            {error}
          </Callout>
        </div>
      )}

      {status === "done" && result && (
        <div className="mt-10">
          <p className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
            Answer
          </p>
          <p className="mt-2 font-display text-xl leading-snug text-balance text-paper sm:text-2xl">
            {result.answer}
          </p>
          {result.available && (
            <p className="mt-3 text-xs text-paper-faint">
              Based on the {result.trace_count} most recent{" "}
              {result.trace_count === 1 ? "trace" : "traces"}.
            </p>
          )}
          {!result.available && result.note && (
            <p className="mt-3 text-xs text-paper-faint">{result.note}</p>
          )}
        </div>
      )}
    </div>
  );
}
