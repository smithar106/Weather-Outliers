import Link from "next/link";

import { Badge, Callout, Card, Container, SectionHeading } from "@/components/ui";
import { getMonitorTrace } from "@/lib/api";
import { formatDurationMs } from "@/lib/format";
import type { MonitorSpan } from "@/lib/types";

export const dynamic = "force-dynamic";

interface PageProps {
  params: Promise<{ traceId: string }>;
}

export async function generateMetadata({ params }: PageProps) {
  const { traceId } = await params;
  const canonical = `/monitor/traces/${encodeURIComponent(traceId)}`;
  return {
    title: `Trace ${traceId.slice(0, 12)}…`,
    alternates: { canonical },
    openGraph: { title: `Trace ${traceId.slice(0, 12)}…`, url: canonical },
  };
}

export default async function TraceDetailPage({ params }: PageProps) {
  const { traceId } = await params;
  const trace = await getMonitorTrace(traceId).catch(() => null);

  if (trace === null) {
    return (
      <Container className="py-20">
        <Callout tone="danger">
          Trace <code className="tnum">{traceId}</code> could not be loaded — it does not exist, or
          the tracking store is unreachable.
        </Callout>
        <div className="mt-6">
          <Link href="/monitor" className="link-underline text-sm text-accent-bright">
            ← Back to monitor
          </Link>
        </div>
      </Container>
    );
  }

  const root = trace.spans.find((span) => span.parent_span_id === null);

  return (
    <Container className="py-12 sm:py-16">
      <SectionHeading
        as="h1"
        eyebrow="Trace"
        title={root?.name ?? "Pipeline run"}
        actions={
          <Link href="/monitor" className="link-underline text-sm text-accent-bright">
            ← All traces
          </Link>
        }
      />

      <p className="tnum mt-4 text-sm text-paper-muted">
        <span className="text-paper-faint">id</span> {trace.trace_id} ·{" "}
        <span className="text-paper-faint">status</span> {trace.status} ·{" "}
        <span className="text-paper-faint">duration</span> {formatDurationMs(trace.duration_ms)}
      </p>

      <Card className="mt-6">
        <SpanTree spans={trace.spans} />
      </Card>
    </Container>
  );
}

function SpanTree({ spans }: { spans: MonitorSpan[] }) {
  const children = new Map<string | null, MonitorSpan[]>();
  for (const span of spans) {
    const group = children.get(span.parent_span_id);
    if (group) group.push(span);
    else children.set(span.parent_span_id, [span]);
  }
  for (const group of children.values()) {
    group.sort((a, b) => a.name.localeCompare(b.name));
  }

  const ordered: { span: MonitorSpan; depth: number }[] = [];
  const seen = new Set<string>();
  const walk = (parentId: string | null, depth: number) => {
    for (const span of children.get(parentId) ?? []) {
      if (seen.has(span.span_id)) continue;
      seen.add(span.span_id);
      ordered.push({ span, depth });
      walk(span.span_id, depth + 1);
    }
  };
  walk(null, 0);
  // A span whose parent was never persisted still gets a row rather than being dropped.
  for (const span of spans) {
    if (!seen.has(span.span_id)) ordered.push({ span, depth: 0 });
  }

  return (
    <div className="divide-y divide-ink-800/70 p-2 sm:p-3">
      {ordered.map(({ span, depth }) => (
        <SpanRow key={span.span_id} span={span} depth={depth} />
      ))}
    </div>
  );
}

const SPAN_TONES: Record<string, string> = {
  OK: "text-positive ring-positive/40 bg-positive/10",
  ERROR: "text-negative ring-negative/40 bg-negative/10",
};

function SpanRow({ span, depth }: { span: MonitorSpan; depth: number }) {
  const interesting = Object.entries(span.attributes).filter(
    ([key, value]) =>
      value !== null &&
      value !== "" &&
      !["run_id", "status", "latency_ms", "environment", "command"].includes(key)
  );

  return (
    <div className="py-3">
      <div className="flex min-w-0 items-baseline gap-3" style={{ paddingLeft: `${depth * 1.25}rem` }}>
        <span className="tnum w-16 shrink-0 text-right text-xs text-paper-faint">
          {formatDurationMs(span.latency_ms)}
        </span>
        <span className="truncate text-sm text-paper">{span.name}</span>
        {span.span_type && (
          <span className="tnum text-[0.6875rem] text-paper-faint">{span.span_type}</span>
        )}
        <Badge className={SPAN_TONES[span.status] ?? "text-paper-dim ring-ink-700 bg-ink-800"}>
          {span.status}
        </Badge>
      </div>

      {interesting.length > 0 && (
        <dl className="mt-1.5 flex flex-wrap gap-x-5 gap-y-1 pl-[6.25rem] text-[0.75rem] text-paper-faint">
          {interesting.map(([key, value]) => (
            <div key={key} className="flex items-baseline gap-1.5">
              <dt>{key}</dt>
              <dd className="tnum text-paper-dim">{String(value)}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}
