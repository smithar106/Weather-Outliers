import Link from "next/link";

import { AnomalyCard } from "@/components/AnomalyCard";
import { ProvenanceStrip } from "@/components/ProvenanceStrip";
import { RarityTable } from "@/components/RarityTable";
import { Badge, Callout, Card, Container, EmptyState, LoadFailure, Section } from "@/components/ui";
import { ApiError, getLatestRankings } from "@/lib/api";
import {
  categoryStyle,
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

  return (
    <>
      <Hero rankings={rankings} />

      <Container className="pb-16">
        <Section id="ranking" className="mt-14">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div>
              <p className="eyebrow">The board</p>
              <h2 className="mt-1.5 font-display text-2xl tracking-tight text-paper">
                Top {rankings.count} statistical outliers
              </h2>
            </div>
            <p className="max-w-md text-sm leading-relaxed text-paper-muted">
              {rankings.one_event_per_city
                ? "One event per city, so a single unusual day cannot occupy the whole board. Every city's other events remain in the dataset and on its detail page."
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
            /*
             * The rarity table is a sibling of the cards rather than a column inside
             * each one, because it is a different reading: ten percentages together
             * are comparable at a glance, and the same ten spread across ten cards
             * are not. On narrow screens it stacks above the cards, which is the
             * right order — an index, then the detail.
             *
             * `lg:items-start` keeps `sticky` working: a stretched grid item is as
             * tall as the row, and a sticky box that fills its container has nothing
             * to slide against.
             */
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
                href="/map"
                className="rounded-lg border border-ink-700 px-4 py-2 text-sm text-paper-dim transition-colors hover:border-accent-dim hover:text-accent-bright"
              >
                See it on the map
              </Link>
              <Link
                href="/evaluation"
                className="rounded-lg border border-ink-700 px-4 py-2 text-sm text-paper-dim transition-colors hover:border-accent-dim hover:text-accent-bright"
              >
                Evaluation results
              </Link>
            </div>
          </Card>
        </Section>
      </Container>
    </>
  );
}

function Hero({ rankings }: { rankings: Rankings }) {
  const topEvent = rankings.events[0]?.event;
  const categories = new Set(rankings.events.map((ranked) => ranked.event.category));

  return (
    <Container className="pt-14 sm:pt-20">
      <div className="max-w-3xl">
        <div className="flex flex-wrap items-center gap-2">
          <Badge className="bg-accent/10 text-accent-bright ring-accent-dim/40">
            Statistical outliers, not records
          </Badge>
          <Badge>{formatNumber(rankings.run.cities_total, 0)} cities</Badge>
          <Badge>1991–2020 baseline</Badge>
        </div>

        <h1 className="mt-6 font-display text-4xl leading-[1.08] tracking-tight text-balance text-paper sm:text-[3.25rem]">
          Yesterday was anything but normal.
        </h1>

        <p className="mt-5 text-lg leading-relaxed text-paper-dim">
          Every day this site compares {formatNumber(rankings.run.cities_total, 0)} North American
          cities against their own thirty-year seasonal history and publishes the{" "}
          {rankings.count} most improbable readings — ranked by how unlikely they were, not by how
          large they were, so a cold snap in Mérida can outrank a hotter day in Phoenix.
        </p>

        {!rankings.is_latest_available && rankings.requested_date && (
          <Callout tone="warning" className="mt-6">
            No analysis was published for {formatLocalDate(rankings.requested_date, "medium")}. The
            most recent successful run is shown instead, for{" "}
            {formatLocalDate(rankings.analysis_date, "medium")}.
          </Callout>
        )}
      </div>

      <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <HeroStat
          label="Analysis date"
          value={formatLocalDate(rankings.analysis_date, "medium")}
          note="The last completed local calendar day for these cities"
        />
        <HeroStat
          label="Published"
          value={formatTimestamp(rankings.published_at)}
          note="Computed once by the scheduled pipeline, then served from storage"
        />
        <HeroStat
          label="Most improbable"
          value={topEvent ? topEvent.city.name : NO_VALUE}
          note={
            topEvent
              ? `${categoryStyle(topEvent.category).label} · score ${formatNumber(
                  topEvent.calculation.anomaly_score,
                  2
                )}`
              : "No ranked events in this run"
          }
          accent={topEvent ? categoryStyle(topEvent.category).color : undefined}
        />
        <HeroStat
          label="Categories on the board"
          value={String(categories.size)}
          note={[...categories].map((category) => categoryStyle(category).label).join(" · ") || NO_VALUE}
        />
      </div>
    </Container>
  );
}

function HeroStat({
  label,
  value,
  note,
  accent,
}: {
  label: string;
  value: string;
  note: string;
  accent?: string;
}) {
  return (
    <Card className="p-4">
      <p className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
        {label}
      </p>
      <p
        className="tnum mt-2 text-[1.0625rem] leading-tight text-paper"
        style={accent ? { color: accent } : undefined}
      >
        {value}
      </p>
      <p className="mt-2 text-xs leading-snug text-paper-faint">{note}</p>
    </Card>
  );
}
