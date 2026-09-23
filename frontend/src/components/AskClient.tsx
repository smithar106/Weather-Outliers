"use client";

import { useState } from "react";

import { Callout } from "@/components/ui";

const SUGGESTIONS = [
  "What was the most unusual event on the latest day?",
  "Which cities were unusually cold?",
  "Compare the rarest heat and cold events",
  "Which city sits furthest from its seasonal norm?",
];

interface ChatResponse {
  question: string;
  answer: string;
  sql: string | null;
  explanation: string | null;
  columns: string[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
  refused: boolean;
}

type Status = "idle" | "loading" | "done" | "error";

export function AskClient() {
  const [question, setQuestion] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const [result, setResult] = useState<ChatResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showData, setShowData] = useState(false);
  const [showSql, setShowSql] = useState(false);

  async function ask(query: string) {
    const trimmed = query.trim();
    if (!trimmed || status === "loading") return;
    setQuestion(trimmed);
    setStatus("loading");
    setError(null);
    setResult(null);
    setShowData(false);
    setShowSql(false);
    try {
      const response = await fetch("/api/ask", {
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
      setResult(data as ChatResponse);
      setStatus("done");
    } catch {
      setError("Could not reach the analysis service.");
      setStatus("error");
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-5 py-12 sm:px-8 sm:py-16">
      <p className="text-[0.6875rem] font-semibold uppercase tracking-[0.12em] text-paper-faint">
        Ask the data
      </p>
      <h1 className="mt-2 font-display text-3xl tracking-tight text-balance text-paper sm:text-4xl">
        What do you want to understand?
      </h1>
      <p className="mt-3 text-[0.9375rem] leading-relaxed text-paper-muted">
        Ask a question in plain language. It is answered with a read-only query over the
        application&apos;s data, and the query is shown with the answer.
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
            placeholder="e.g. Which city was most unusual on the latest day?"
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
          <p className="text-sm text-paper-muted">
            Analyzing the data and running a read-only query…
          </p>
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
          <p className="text-[0.6875rem] font-semibold uppercase tracking-[0.12em] text-paper-faint">
            Answer
          </p>
          <p className="mt-2 font-display text-xl leading-snug text-balance text-paper sm:text-2xl">
            {result.answer}
          </p>
          {result.explanation && (
            <p className="mt-2 text-sm leading-relaxed text-paper-muted">{result.explanation}</p>
          )}

          {result.columns.length > 0 && (
            <div className="mt-8">
              <button
                type="button"
                onClick={() => setShowData((value) => !value)}
                className="text-sm text-accent-bright transition-colors hover:text-accent"
                aria-expanded={showData}
              >
                {showData ? "Hide data" : "View data"}
              </button>
              {showData && (
                <div className="mt-3 overflow-x-auto rounded-lg border border-ink-700">
                  <table className="w-full border-collapse text-sm">
                    <thead>
                      <tr className="border-b border-ink-700">
                        {result.columns.map((column) => (
                          <th
                            key={column}
                            scope="col"
                            className="whitespace-nowrap px-4 py-2.5 text-left text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint"
                          >
                            {column}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {result.rows.map((row, index) => (
                        <tr key={index} className="border-b border-ink-800/70 last:border-0">
                          {result.columns.map((column, columnIndex) => (
                            <td key={column} className="tnum px-4 py-2.5 text-paper-dim">
                              {row[columnIndex] === null ? "—" : String(row[columnIndex])}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <p className="mt-2 text-xs text-paper-faint">
                {result.row_count} {result.row_count === 1 ? "row" : "rows"}
                {result.truncated ? " · truncated to the row limit" : ""}
              </p>
            </div>
          )}

          {result.sql && (
            <div className="mt-6">
              <button
                type="button"
                onClick={() => setShowSql((value) => !value)}
                className="text-sm text-accent-bright transition-colors hover:text-accent"
                aria-expanded={showSql}
              >
                {showSql ? "Hide SQL" : "View SQL"}
              </button>
              {showSql && (
                <pre className="mt-3 overflow-x-auto rounded-lg border border-ink-700 bg-ink-900 px-4 py-3 text-[0.8125rem] leading-relaxed text-paper-dim">
                  <code>{result.sql}</code>
                </pre>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
