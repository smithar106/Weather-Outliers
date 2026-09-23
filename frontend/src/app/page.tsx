import Link from "next/link";

import { AnomalyCard } from "@/components/AnomalyCard";
import { ProvenanceStrip } from "@/components/ProvenanceStrip";
import { RarityTable } from "@/components/RarityTable";
import {
  Badge,
  Callout,
  Card,
  Container,
  EmptyState,
  LoadFailure,
  Metric,
  Section,
} from "@/components/ui";
import { ApiError, getHealth, getLatestRankings } from "@/lib/api";
import {
  categoryStyle,
  cityLabel,
  directionWord,
  formatDeviation,
  formatHoursAgo,
  formatLocalDate,
  formatNumber,
  formatTimestamp,
  NO_VALUE,
} from "@/lib/format";
import type { Rankings } from "@/lib/types";

/**
 * Rendered per request, not prerendered at build time.
 *
 * The production build runs in an image builder with no route to the backend — on
 * Railway the API is reachable only over the private network, and only at runtime —
 * so a prerender would bake a "data unavailable" page into the image and serve it
 * until the first revalidation. Per-request rendering costs nothing extra here:
 * `fetchCache` keeps the API response in the Data Cache for the window set in
 * `lib/api.ts`, so a thousand page views inside that window produce one backend
 * query rather than a thousand, and none of them reach a weather provider or a
 * language model either way.
 */
export const dynamic = "force-dynamic";
export const fetchCache = "default-cache";

export const metadata = {
  title: "Yesterday was anything but normal",
  description:
    "The ten most statistically unusual weather events across 50 North American cities, ranked against a 30-year seasonal baseline.",
};

export default async function HomePage() {
  let rankings: Rankings;
  try {
    rankings = await getLatestRankings();
  } catch (error) {
    return (
      <LoadFailure
        what="Today's ranking"
        detail={error instanceof ApiError ? `${error.status || "network"} · ${error.message}` : undefined}
      >
        <Link href="/methodology" className="link-underline text-sm text-accent-bright">
          Read the methodology while the data is unavailable →
        </Link>
      </LoadFailure>
    );
  }

  // Freshness is supplementary; a health failure must not take the page down.
  const health = await getHealth().catch(() => null);

  return (
    <>
      <Hero rankings={rankings} hoursSincePublish={health?.hours_since_publish ?? null} />

      <Container className="pb-16">
        <Insight rankings={rankings} />

        <Section id="ranking" className="mt-16">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div>
              <p className="eyebrow">The board</p>
              <h2 className="mt-1.5 font-display text-2xl tracking-tight text-paper">
                Top {rankings.count} statistical outliers
              </h2>
            </div>
            <p className="max-w-md text-sm leading-relaxed text-paper-muted">
              {rankings.one_event_per_city
                ? "One event per city, so a single unusual day can't occupy the whole board."
                : "All ranked events, including multiple events from the same city."}
            </p>
          </div>

          {rankings.events.length === 0 ? (
            <div className="mt-6">
              <EmptyState title="This run published no ranked events.">
                The pipeline completed but found nothing that met the minimum sample-size and data
                quality rules. That is a legitimate outcome, not an error.
              </EmptyState>
            </div>
          ) : (
            <div className="mt-6 grid gap-6 lg:grid-cols-[16.5rem_minmax(0,1fr)] lg:items-start">
              <div className="lg:sticky lg:top-6">
                <RarityTable events={rankings.events} />
              </div>

              <ol className="space-y-5">
                {rankings.events.map((ranked) => (
                  <li key={ranked.event.id}>
                    <AnomalyCard ranked={ranked} />
                  </li>
                ))}
              </ol>
            </div>
          )}
        </Section>

        <Section id="provenance" className="mt-16">
          <div className="mb-6">
            <p className="eyebrow">Provenance</p>
            <h2 className="mt-1.5 font-display text-2xl tracking-tight text-paper">
              Where these numbers came from
            </h2>
          </div>
          <ProvenanceStrip run={rankings.run} dataSources={rankings.data_sources} />
        </Section>

        <Section className="mt-16">
          <Card className="p-6 sm:p-8">
            <p className="eyebrow">How the ranking works</p>
            <p className="prose-desk mt-3 max-w-3xl text-[0.9375rem]">{rankings.ranking_basis}</p>
            <div className="mt-6 flex flex-wrap gap-3">
              <Link
                href="/methodology"
                className="rounded-lg border border-ink-700 bg-ink-800 px-4 py-2 text-sm text-paper transition-colors hover:border-accent-dim hover:text-accent-bright"
              >
                Full methodology
              </Link>
              <Link
                href="/ask"
                className="rounded-lg border border-ink-700 px-4 py-2 text-sm text-paper-dim transition-colors hover:border-accent-dim hover:text-accent-bright"
              >
                Ask the data
              </Link>
            </div>
          </Card>
        </Section>
      </Container>
    </>
  );
}

function Hero({ rankings, hoursSincePublish }: { rankings: Rankings; hoursSincePublish: number | null }) {
  const topEvent = rankings.events[0]?.event;
  const categories = new Set(rankings.events.map((ranked) => ranked.event.category));
  const topStyle = topEvent ? categoryStyle(topEvent.category) : null;

  return (
    <Container className="pt-12 sm:pt-16">
      <div className="flex flex-wrap items-center gap-2.5 text-sm text-paper-faint">
        <span className="inline-flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className={`size-1.5 rounded-full ${hoursSincePublish !== null ? "bg-positive" : "bg-ink-600"}`}
          />
          {hoursSincePublish !== null ? `Updated ${formatHoursAgo(hoursSincePublish)}` : "Freshness unknown"}
        </span>
        <span aria-hidden="true">·</span>
        <span>for {formatLocalDate(rankings.analysis_date, "long")}</span>
      </div>

      {!rankings.is_latest_available && rankings.requested_date && (
        <Callout tone="warning" className="mt-6">
          No analysis was published for {formatLocalDate(rankings.requested_date, "medium")}. The
          most recent successful run is shown instead, for{" "}
          {formatLocalDate(rankings.analysis_date, "medium")}.
        </Callout>
      )}

      <div className="mt-8 max-w-3xl">
        <div className="flex flex-wrap items-center gap-2">
          <Badge className="bg-accent/10 text-accent-bright ring-accent-dim/40">
            Statistical outliers, not records
          </Badge>
          <Badge>1991–2020 baseline</Badge>
        </div>

        <h1 className="mt-6 font-display text-4xl leading-[1.05] tracking-tight text-balance text-paper sm:text-[3.5rem]">
          Yesterday was anything but normal.
        </h1>

        <p className="mt-6 max-w-2xl text-lg leading-relaxed text-paper-dim">
          {formatNumber(rankings.run.cities_total, 0)} cities measured against their own thirty-year
          history — ranked by how improbable, not how large, each reading was.
        </p>
      </div>

      {topEvent && topStyle && (
        <div className="mt-12">
          <Metric
            label="Most improbable reading"
            value={formatDeviation(topEvent.calculation.deviation, topEvent.unit)}
            note={`${cityLabel(topEvent.city)} — ${directionWord(topEvent.direction, topEvent.category)}`}
            accent={topStyle.color}
          />
        </div>
      )}

      <dl className="mt-12 grid grid-cols-2 gap-x-8 gap-y-6 border-t border-ink-800 pt-8 sm:grid-cols-4">
        <StripItem label="Analysis date" value={formatLocalDate(rankings.analysis_date, "medium")} />
        <StripItem label="Published" value={formatTimestamp(rankings.published_at)} />
        <StripItem
          label="Cities analysed"
          value={`${formatNumber(rankings.run.cities_with_data, 0)} of ${formatNumber(
            rankings.run.cities_total,
            0
          )}`}
        />
        <StripItem
          label="Categories"
          value={String(categories.size)}
          note={[...categories].map((category) => categoryStyle(category).label).join(" · ") || NO_VALUE}
        />
      </dl>
    </Container>
  );
}

function StripItem({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div>
      <dt className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
        {label}
      </dt>
      <dd className="tnum mt-1.5 text-base text-paper">{value}</dd>
      {note && <p className="mt-1 text-xs text-paper-faint">{note}</p>}
    </div>
  );
}

/**
 * The lead insight, surfaced without a prompt.
 *
 * It is the explanation the pipeline already wrote for the most improbable event —
 * a model's sentences when one ran, or the deterministic template's otherwise —
 * presented as the answer to "what happened yesterday?" rather than buried in the
 * tenth card.
 */
function Insight({ rankings }: { rankings: Rankings }) {
  const top = rankings.events[0];
  const event = top?.event;
  if (!event?.explanation) return null;

  const isModel = event.explanation.generator === "llm";
  const style = categoryStyle(event.category);

  return (
    <Section className="mt-16">
      <div className="rounded-lg border border-ink-700/70 bg-ink-850/40 p-6 sm:p-8">
        <div className="flex flex-wrap items-center gap-2.5">
          <p className="eyebrow">The brief</p>
          <Badge className={style.badge}>{style.label}</Badge>
        </div>
        <p className="mt-4 max-w-3xl font-display text-xl leading-snug text-balance text-paper sm:text-2xl">
          {event.explanation.headline}
        </p>
        <p className="mt-3 max-w-3xl text-[0.9375rem] leading-relaxed text-paper-dim">
          {event.explanation.statistical_explanation}
        </p>
        <p className="mt-4 text-xs text-paper-faint">
          {isModel
            ? `Written by ${event.explanation.llm_provider ?? "a language model"}${
                event.explanation.model ? ` (${event.explanation.model})` : ""
              }, grounded in ${event.explanation.tool_call_count} verified tool call${
                event.explanation.tool_call_count === 1 ? "" : "s"
              }.`
            : "Written by the deterministic template, not a language model."}
        </p>
      </div>
    </Section>
  );
}
