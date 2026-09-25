import type { ReactNode } from "react";

import { Badge, Callout, Card, Container, Section, SectionHeading } from "@/components/ui";
import { getMonitorEvals, getMonitorRuns, getMonitorTraces } from "@/lib/api";
import {
  formatDurationMs,
  formatTimestamp,
  formatUsd,
  NO_VALUE,
} from "@/lib/format";
import type { MonitorEval, MonitorRun, MonitorTrace, MonitorTraces } from "@/lib/types";

export const dynamic = "force-dynamic";

export const metadata = {
  title: "Monitor",
  description:
    "Pipeline run history, evaluation reports, and MLflow traces for the Weather Outliers application.",
  alternates: { canonical: "/monitor" },
  openGraph: { title: "Monitor", url: "/monitor" },
};

export default async function MonitorPage() {
  const [runs, evals, traces] = await Promise.all([
    getMonitorRuns().catch(() => null),
    getMonitorEvals().catch(() => null),
    getMonitorTraces().catch(() => null),
  ]);

  return (
    <Container className="py-12 sm:py-16">
      <SectionHeading
        as="h1"
        eyebrow="Operations"
        title="Monitor"
        description="How the pipeline is performing: run history, evaluation outcomes, and the MLflow traces each run produced."
      />

      <Section id="runs" className="mt-12">
        <SectionHeading
          eyebrow="Pipeline"
          title="Recent runs"
          description="One row per scheduled execution, newest first."
        />
        <RunsTable runs={runs?.runs ?? null} />
      </Section>

      <Section id="evals" className="mt-14">
        <SectionHeading
          eyebrow="Evaluation"
          title="Evaluation reports"
          description="Each row is one run of the evaluation harness, persisted by the delivery worker."
        />
        <EvalsTable evals={evals?.reports ?? null} />
      </Section>

      <Section id="traces" className="mt-14">
        <SectionHeading
          eyebrow="Telemetry"
          title="Traces"
          description="MLflow traces from the scheduled workers. A trace is one pipeline execution, broken into spans."
        />
        <TracesTable traces={traces} />
      </Section>
    </Container>
  );
}

// ---------------------------------------------------------------------------
// Runs
// ---------------------------------------------------------------------------

function RunsTable({ runs }: { runs: MonitorRun[] | null }) {
  if (runs === null) {
    return (
      <Callout tone="danger" className="mt-6">
        Pipeline run history could not be loaded from the API.
      </Callout>
    );
  }
  if (runs.length === 0) {
    return <Callout className="mt-6">No pipeline runs recorded yet.</Callout>;
  }

  return (
    <Card className="mt-6 overflow-x-auto">
      <table className="w-full min-w-[760px] border-collapse text-sm">
        <thead>
          <tr className="border-b border-ink-700 text-left">
            <Th>Started</Th>
            <Th>Kind</Th>
            <Th>Date</Th>
            <Th>Status</Th>
            <Th className="text-right">Events</Th>
            <Th className="text-right">Cities</Th>
            <Th className="text-right">LLM calls</Th>
            <Th className="text-right">Est. cost</Th>
            <Th>Outcome</Th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.run_id} className="border-b border-ink-800/70 last:border-0">
              <Td mono>{formatTimestamp(run.started_at)}</Td>
              <Td>{run.kind}</Td>
              <Td mono>{run.analysis_date ?? NO_VALUE}</Td>
              <Td>
                <StatusBadge status={run.status} />
              </Td>
              <Td mono className="text-right">
                {run.events_published}
              </Td>
              <Td mono className="text-right">
                {run.cities_with_data}/{run.cities_total}
              </Td>
              <Td mono className="text-right">
                {run.llm_calls}
              </Td>
              <Td mono className="text-right">
                {run.llm_calls > 0 ? formatUsd(run.llm_estimated_usd) : NO_VALUE}
              </Td>
              <Td>
                {run.error ? (
                  <span title={run.error} className="text-negative">
                    {run.error_type ?? "error"}
                  </span>
                ) : run.published ? (
                  <span className="text-paper-dim">published</span>
                ) : (
                  <span className="text-paper-faint">not published</span>
                )}
              </Td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Evals
// ---------------------------------------------------------------------------

function EvalsTable({ evals }: { evals: MonitorEval[] | null }) {
  if (evals === null) {
    return (
      <Callout tone="danger" className="mt-6">
        Evaluation reports could not be loaded from the API.
      </Callout>
    );
  }
  if (evals.length === 0) {
    return (
      <Callout className="mt-6">
        No evaluation reports persisted yet. The delivery worker writes them on its schedule.
      </Callout>
    );
  }

  return (
    <Card className="mt-6 overflow-x-auto">
      <table className="w-full min-w-[720px] border-collapse text-sm">
        <thead>
          <tr className="border-b border-ink-700 text-left">
            <Th>Generated</Th>
            <Th>Status</Th>
            <Th className="text-right">Suites</Th>
            <Th className="text-right">Cases</Th>
            <Th className="text-right">Duration</Th>
            <Th>Commit</Th>
          </tr>
        </thead>
        <tbody>
          {evals.map((report) => (
            <tr key={report.id} className="border-b border-ink-800/70 last:border-0">
              <Td mono>{formatTimestamp(report.generated_at)}</Td>
              <Td>
                <StatusBadge status={report.status} />
              </Td>
              <Td mono className="text-right">
                {report.suites_passed}/{report.suites_total}
              </Td>
              <Td mono className="text-right">
                {report.cases_passed}/{report.cases_total}
              </Td>
              <Td mono className="text-right">
                {formatDurationMs(report.duration_ms)}
              </Td>
              <Td mono>
                {report.git_commit ?? NO_VALUE}
                {report.git_dirty ? " (dirty)" : ""}
              </Td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Traces
// ---------------------------------------------------------------------------

function TracesTable({ traces }: { traces: MonitorTraces | null }) {
  if (traces === null) {
    return (
      <Callout tone="danger" className="mt-6">
        Trace data could not be loaded from the API.
      </Callout>
    );
  }
  if (!traces.available) {
    return (
      <Callout className="mt-6">
        {traces.note ?? "The tracking store is not configured."}
      </Callout>
    );
  }
  if (traces.count === 0) {
    return (
      <Callout className="mt-6">
        {traces.note ?? "No traces recorded yet in this experiment."}
      </Callout>
    );
  }

  return (
    <Card className="mt-6 overflow-x-auto">
      <table className="w-full min-w-[820px] border-collapse text-sm">
        <thead>
          <tr className="border-b border-ink-700 text-left">
            <Th>Time (UTC)</Th>
            <Th>Status</Th>
            <Th className="text-right">Duration</Th>
            <Th className="text-right">Spans</Th>
            <Th>Root span</Th>
            <Th>Model</Th>
            <Th>Prompt</Th>
            <Th>Run</Th>
          </tr>
        </thead>
        <tbody>
          {traces.traces.map((trace) => (
            <TraceRow key={trace.trace_id} trace={trace} />
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function TraceRow({ trace }: { trace: MonitorTrace }) {
  const label = `${trace.root_span ?? "trace"} · ${trace.trace_id.slice(0, 12)}…`;
  return (
    <tr className="border-b border-ink-800/70 last:border-0">
      <Td mono>{formatEpochMs(trace.timestamp_ms)}</Td>
      <Td>
        <StatusBadge status={trace.status} />
      </Td>
      <Td mono className="text-right">
        {formatDurationMs(trace.duration_ms)}
      </Td>
      <Td mono className="text-right">
        {trace.span_count}
      </Td>
      <Td>
        <a
          href={`/monitor/traces/${trace.trace_id}`}
          title={trace.trace_id}
          className="link-underline text-paper"
        >
          {label}
        </a>
      </Td>
      <Td>{trace.model ?? NO_VALUE}</Td>
      <Td mono>{trace.prompt_version ?? NO_VALUE}</Td>
      <Td mono>{trace.run_id ? trace.run_id.slice(0, 20) : NO_VALUE}</Td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Shared bits
// ---------------------------------------------------------------------------

function Th({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <th
      scope="col"
      className={`whitespace-nowrap px-4 py-2.5 text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint ${className}`}
    >
      {children}
    </th>
  );
}

function Td({
  children,
  className = "",
  mono = false,
}: {
  children: ReactNode;
  className?: string;
  mono?: boolean;
}) {
  return <td className={`px-4 py-2.5 align-top text-paper-dim ${mono ? "tnum" : ""} ${className}`}>{children}</td>;
}

function StatusBadge({ status }: { status: string }) {
  const tone = STATUS_TONES[status] ?? "text-paper-dim ring-ink-700 bg-ink-800";
  return <Badge className={tone}>{status}</Badge>;
}

const STATUS_TONES: Record<string, string> = {
  succeeded: "text-positive ring-positive/40 bg-positive/10",
  passed: "text-positive ring-positive/40 bg-positive/10",
  OK: "text-positive ring-positive/40 bg-positive/10",
  failed: "text-negative ring-negative/40 bg-negative/10",
  error: "text-negative ring-negative/40 bg-negative/10",
  ERROR: "text-negative ring-negative/40 bg-negative/10",
  running: "text-warning ring-warning/40 bg-warning/10",
};

function formatEpochMs(ms: number | null): string {
  if (ms === null) return NO_VALUE;
  const date = new Date(ms);
  if (Number.isNaN(date.getTime())) return NO_VALUE;
  return `${date.toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "UTC",
  })}`;
}
