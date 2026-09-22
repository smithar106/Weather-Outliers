/**
 * One archived board.
 *
 * Deliberately the same components as the home page, because it is the same thing:
 * a published run with its provenance. The only difference is that the date came
 * from the URL, and the page says so when the backend served a different date than
 * the one asked for.
 */

import Link from "next/link";
import { notFound } from "next/navigation";

import { AnomalyCard } from "@/components/AnomalyCard";
import { ProvenanceStrip } from "@/components/ProvenanceStrip";
import {
  Badge,
  Callout,
  Container,
  EmptyState,
  LoadFailure,
  Section,
  SectionHeading,
} from "@/components/ui";
import { ApiError, getRankingsForDate, isNotFound } from "@/lib/api";
import { formatLocalDate, formatTimestamp } from "@/lib/format";
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

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

interface PageProps {
  params: Promise<{ date: string }>;
}

export async function generateMetadata({ params }: PageProps) {
  const { date } = await params;
  if (!ISO_DATE.test(date)) return { title: "Archive" };
  return {
    title: formatLocalDate(date, "medium"),
    description: `The statistical outlier board published for ${formatLocalDate(date, "long")}.`,
  };
}

export default async function ArchivedBoardPage({ params }: PageProps) {
  const { date } = await params;

  // Rejected here rather than forwarded: the API validates the path too, but a
  // malformed date should not become a backend request at all.
  if (!ISO_DATE.test(date)) notFound();

  let rankings: Rankings;
  try {
    rankings = await getRankingsForDate(date);
  } catch (error) {
    if (isNotFound(error)) notFound();
    return (
      <LoadFailure
        what={`The board for ${formatLocalDate(date, "medium")}`}
        detail={error instanceof ApiError ? `${error.status || "network"} · ${error.message}` : undefined}
      >
        <Link href="/archive" className="link-underline text-sm text-accent-bright">
          Back to the archive →
        </Link>
      </LoadFailure>
    );
  }

  const servedADifferentDate = rankings.analysis_date !== date;

  return (
    <Container className="py-12 sm:py-16">
      <Section>
        <p className="text-sm text-paper-faint">
          <Link href="/archive" className="link-underline">
            Archive
          </Link>{" "}
          <span aria-hidden="true">/</span> {rankings.analysis_date}
        </p>

        <SectionHeading
          as="h1"
          eyebrow="Archived board"
          title={formatLocalDate(rankings.analysis_date, "long")}
          description={
            <>
              Top {rankings.count} statistical outliers as published for this date. Published{" "}
              {formatTimestamp(rankings.published_at)}.
            </>
          }
        />

        <div className="mt-5 flex flex-wrap gap-2">
          <Badge>Methodology {rankings.methodology_version}</Badge>
          {rankings.run.data_tier && <Badge>{rankings.run.data_tier} data</Badge>}
          <Badge>{rankings.run.cities_with_data} cities with data</Badge>
        </div>

        {servedADifferentDate && (
          <Callout tone="warning" className="mt-6">
            No board was published for {formatLocalDate(date, "medium")}. The nearest available run
            is shown instead, for {formatLocalDate(rankings.analysis_date, "medium")}.
          </Callout>
        )}
      </Section>

      <Section className="mt-10">
        {rankings.events.length === 0 ? (
          <EmptyState title="This run published no ranked events.">
            The pipeline completed but nothing met the minimum sample-size and data quality rules.
          </EmptyState>
        ) : (
          <ol className="space-y-5">
            {rankings.events.map((ranked) => (
              <li key={ranked.event.id}>
                <AnomalyCard ranked={ranked} />
              </li>
            ))}
          </ol>
        )}
      </Section>

      <Section className="mt-16">
        <h2 className="font-display text-2xl tracking-tight text-paper">Run provenance</h2>
        <div className="mt-5">
          <ProvenanceStrip run={rankings.run} dataSources={rankings.data_sources} />
        </div>
      </Section>
    </Container>
  );
}
