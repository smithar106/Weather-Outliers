/**
 * The evaluation dashboard.
 *
 * Everything here is read from `src/data/evaluation-report.json`, which is written
 * by `python -m evals.runner` and copied in by `scripts/sync-evaluation-report.mjs`
 * at build time. Nothing on this page is typed by hand, and nothing is rounded up.
 *
 * Two constraints shaped it:
 *
 *  * No invented figures. If the harness did not measure it, it is not here — which
 *    is why there is no "99.9% uptime" and no production latency number. The timing
 *    figures are labelled as floors, because they were taken against an in-memory
 *    database and a synthetic provider.
 *  * A failed run must be visible. The page renders whatever status the report
 *    carries, and a red header is a legitimate state, not a bug to hide.
 */

import Link from "next/link";

import report from "@/data/evaluation-report.json";
import {
  Badge,
  Callout,
  Card,
  Container,
  DefRow,
  Section,
  SectionHeading,
} from "@/components/ui";
import { formatDurationMs, formatNumber, formatPercent, formatTimestamp } from "@/lib/format";
import type { EvalCase, EvalMetric, EvalReport, EvalSuite, SuiteStatus } from "@/lib/types";

export const metadata = {
  title: "Evaluation",
  description:
    "Measured results from the project's own evaluation harness: statistical suites, agent grounding checks, pipeline reproducibility and API contract verification.",
};

const evaluation = report as EvalReport;

const STATUS_STYLES: Record<SuiteStatus, { label: string; badge: string; dot: string }> = {
  passed: { label: "Passed", badge: "bg-positive/10 text-positive ring-positive/30", dot: "bg-positive" },
  failed: { label: "Failed", badge: "bg-negative/10 text-negative ring-negative/30", dot: "bg-negative" },
  error: { label: "Errored", badge: "bg-negative/10 text-negative ring-negative/30", dot: "bg-negative" },
  skipped: { label: "Skipped", badge: "bg-ink-800 text-paper-muted ring-ink-700", dot: "bg-ink-600" },
};

/** Human wording for the case categories the suites emit. */
const CATEGORY_LABELS: Record<string, string> = {
  test_module: "Test module",
  clean: "Faithful prose (must be accepted)",
  record_claim: "Unsupported record claim",
  causal_claim: "Unsupported causal attribution",
  fabricated_number: "Fabricated measurement",
  source_reference: "Invented source reference",
  missing_evidence: "Missing evidence",
  wrong_city: "Wrong city or date",
  published_explanation: "Published explanation, re-checked",
  determinism: "Determinism",
  idempotency: "Idempotency",
  atomicity: "Atomic publish",
  endpoint: "Endpoint behaviour",
  validation: "Input validation",
  read_only: "Read-only enforcement",
  bounded_query: "Query bounds",
  caching: "Cache headers",
  integrity: "Response matches the database",
};

function categoryLabel(category: string): string {
  return CATEGORY_LABELS[category] ?? category.replace(/_/g, " ");
}

/** A metric value, formatted by its declared unit rather than guessed at. */
function formatMetric(metric: EvalMetric): string {
  const { value, unit } = metric;
  if (value === null) return "—";
  if (typeof value === "string") return value.replace(/_/g, " ");
  if (unit === "fraction") return formatPercent(value, value === 1 || value === 0 ? 0 : 1);
  if (unit === "USD") return `$${value.toFixed(2)}`;
  if (unit === "ms") return formatDurationMs(value);
  if (unit === "s") return `${formatNumber(value, 2)} s`;
  if (unit === null) return formatNumber(value, Number.isInteger(value) ? 0 : 2);
  return `${formatNumber(value, Number.isInteger(value) ? 0 : 2)} ${unit}`;
}

export default function EvaluationPage() {
  const status = STATUS_STYLES[evaluation.status];
  const { totals } = evaluation;

  return (
    <Container className="py-12 sm:py-16">
      <Section>
        <SectionHeading
          as="h1"
          eyebrow="Evaluation"
          title="Measured results, not claimed ones"
          description="The output of this project's own harness, copied into the build verbatim. Every figure below was produced by a run; none of them were typed in by hand, and there are no production performance numbers here because the harness cannot measure production."
        />

        <div className="mt-6 flex flex-wrap items-center gap-2">
          <Badge className={status.badge}>
            <span aria-hidden="true" className={`size-1.5 rounded-full ${status.dot}`} />
            {status.label}
          </Badge>
          <Badge>Methodology {evaluation.methodology_version}</Badge>
          <Badge>
            {evaluation.git_commit
              ? `commit ${evaluation.git_commit.slice(0, 8)}${evaluation.git_dirty ? " (dirty)" : ""}`
              : "commit not recorded"}
          </Badge>
        </div>

        <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Headline
            label="Suites"
            value={`${formatNumber(totals.suites_passed, 0)} / ${formatNumber(totals.suites_total, 0)}`}
            note="Passed of run"
          />
          <Headline
            label="Checks"
            value={`${formatNumber(totals.cases_passed, 0)} / ${formatNumber(totals.cases_total, 0)}`}
            note="Individual assertions with a recorded expectation and observation"
          />
          <Headline
            label="Harness wall-clock"
            value={formatDurationMs(totals.duration_ms)}
            note="Whole harness, including building a synthetic thirty-year world from scratch"
          />
          <Headline
            label="Run at"
            value={formatTimestamp(evaluation.generated_at)}
            note="This page is a build-time artefact; re-run the harness and rebuild to refresh it"
          />
        </div>
      </Section>

      {/* ---------------- Environment ---------------- */}
      <Section className="mt-14">
        <h2 className="font-display text-2xl tracking-tight text-paper">What was under test</h2>
        <Card className="mt-5 p-6">
          <dl>
            {Object.entries(evaluation.environment).map(([key, value]) => (
              <DefRow key={key} term={key.replace(/_/g, " ")} mono>
                {value === null ? "—" : String(value).replace(/_/g, " ")}
              </DefRow>
            ))}
          </dl>
          <p className="mt-4 text-sm leading-relaxed text-paper-muted">
            The harness runs against a synthetic weather provider and a scratch database on purpose.
            A suite that called the real Open-Meteo API would be measuring the network rather than
            the statistics, would not be reproducible, and would put a load on a free service every
            time anyone ran the tests. The trade-off is stated in the limitations below.
          </p>
        </Card>
      </Section>

      {/* ---------------- Suites ---------------- */}
      <Section className="mt-14">
        <h2 className="font-display text-2xl tracking-tight text-paper">Suites</h2>
        <div className="mt-5 space-y-5">
          {evaluation.suites.map((suite) => (
            <SuitePanel key={suite.id} suite={suite} />
          ))}
        </div>
      </Section>

      {/* ---------------- Limitations ---------------- */}
      <Section className="mt-14">
        <h2 className="font-display text-2xl tracking-tight text-paper">
          What these results do not establish
        </h2>
        <Card className="mt-5 p-6">
          <ul className="space-y-3.5">
            {evaluation.limitations.map((limitation) => (
              <li key={limitation} className="flex gap-3 text-sm leading-relaxed text-paper-dim">
                <span aria-hidden="true" className="mt-2 size-1.5 shrink-0 rounded-full bg-ink-600" />
                {limitation}
              </li>
            ))}
          </ul>
        </Card>
      </Section>

      <Section className="mt-14">
        <Card className="p-6 sm:p-8">
          <p className="eyebrow">Reproducing this</p>
          <p className="mt-3 max-w-3xl text-[0.9375rem] leading-relaxed text-paper-muted">
            The harness needs no API keys, no database server and no network access. From the
            repository root:
          </p>
          <pre className="mt-4 overflow-x-auto rounded-lg border border-ink-700 bg-ink-900 px-4 py-3 text-[0.8125rem] leading-relaxed text-paper-dim">
            <code>{"python -m evals.runner\npython -m evals.runner --suite grounding --json"}</code>
          </pre>
          <p className="mt-4 text-sm leading-relaxed text-paper-muted">
            The run writes <code>evals/reports/latest.json</code>, which is what this page renders.
            The build fails if the committed copy has drifted from it, so the page cannot silently
            show an older result than the code it shipped with.
          </p>
          <Link
            href="/methodology"
            className="link-underline mt-5 inline-block text-sm text-accent-bright"
          >
            What the suites are checking against →
          </Link>
        </Card>
      </Section>
    </Container>
  );
}

// ---------------------------------------------------------------------------

function Headline({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <Card className="p-4">
      <p className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
        {label}
      </p>
      <p className="tnum mt-2 text-[1.0625rem] leading-tight text-paper">{value}</p>
      <p className="mt-2 text-xs leading-snug text-paper-faint">{note}</p>
    </Card>
  );
}

function SuitePanel({ suite }: { suite: EvalSuite }) {
  const status = STATUS_STYLES[suite.status];
  const failures = suite.cases.filter((one) => !one.passed);
  const byCategory = new Map<string, EvalCase[]>();
  for (const one of suite.cases) {
    const bucket = byCategory.get(one.category);
    if (bucket) bucket.push(one);
    else byCategory.set(one.category, [one]);
  }

  return (
    <Card className="p-6 sm:p-7">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="max-w-2xl">
          <h3 className="font-display text-xl tracking-tight text-paper">{suite.title}</h3>
          <p className="mt-2 text-sm leading-relaxed text-paper-muted">{suite.description}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge className={status.badge}>
            <span aria-hidden="true" className={`size-1.5 rounded-full ${status.dot}`} />
            {status.label}
          </Badge>
          <Badge>
            {formatNumber(suite.cases_passed, 0)}/{formatNumber(suite.cases_total, 0)} checks
          </Badge>
          <Badge>{formatDurationMs(suite.duration_ms)}</Badge>
        </div>
      </div>

      {suite.error && (
        <Callout tone="danger" className="mt-5" title="The suite did not complete">
          {suite.error}
        </Callout>
      )}

      {suite.metrics.length > 0 && (
        <dl className="mt-6 grid gap-x-6 gap-y-4 border-t border-ink-800 pt-5 sm:grid-cols-2 lg:grid-cols-3">
          {suite.metrics.map((metric) => (
            <div key={metric.label}>
              <dt className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
                {metric.label}
              </dt>
              <dd className="tnum mt-1 text-base text-paper">{formatMetric(metric)}</dd>
              {metric.detail && (
                <p className="mt-1 text-xs leading-snug text-paper-faint">{metric.detail}</p>
              )}
            </div>
          ))}
        </dl>
      )}

      {failures.length > 0 && (
        <div className="mt-6 space-y-3">
          {failures.map((one) => (
            <Callout key={one.id} tone="danger" title={one.title}>
              <p className="tnum">
                Expected {one.expected} · observed {one.observed}
              </p>
              {one.detail && <p className="mt-1 text-paper-faint">{one.detail}</p>}
            </Callout>
          ))}
        </div>
      )}

      {suite.notes.length > 0 && (
        <div className="mt-6 space-y-2 border-t border-ink-800 pt-5">
          {suite.notes.map((note) => (
            <p key={note} className="text-sm leading-relaxed text-paper-muted">
              {note}
            </p>
          ))}
        </div>
      )}

      {suite.cases.length > 0 && (
        <details className="group mt-6 border-t border-ink-800 pt-5">
          <summary className="cursor-pointer text-sm text-accent-bright marker:text-paper-faint">
            All {formatNumber(suite.cases_total, 0)} checks
          </summary>
          <div className="mt-4 space-y-5">
            {[...byCategory.entries()].map(([category, cases]) => (
              <div key={category}>
                <p className="eyebrow">
                  {categoryLabel(category)} · {cases.filter((one) => one.passed).length}/
                  {cases.length}
                </p>
                <ul className="mt-2 divide-y divide-ink-800">
                  {cases.map((one) => (
                    <li key={one.id} className="flex items-start gap-3 py-2.5">
                      <span
                        aria-hidden="true"
                        className={`mt-1.5 size-1.5 shrink-0 rounded-full ${
                          one.passed ? "bg-positive" : "bg-negative"
                        }`}
                      />
                      <span className="min-w-0 flex-1">
                        <span className="block text-sm text-paper-dim">{one.title}</span>
                        <span className="tnum block text-xs text-paper-faint">
                          expected {one.expected} · observed {one.observed}
                        </span>
                      </span>
                      <span className="sr-only">{one.passed ? "passed" : "failed"}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </details>
      )}
    </Card>
  );
}
