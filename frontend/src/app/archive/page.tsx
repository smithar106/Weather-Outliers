/**
 * The archive index.
 *
 * Every published run is kept, including runs computed under an earlier
 * methodology version, and the version is shown per row. Retracting or silently
 * recomputing history would make the reproducibility claim meaningless — if a past
 * board was produced by different code, the honest thing is to say which code.
 */

import Link from "next/link";

import {
  Badge,
  Card,
  Container,
  EmptyState,
  LoadFailure,
  Section,
  SectionHeading,
} from "@/components/ui";
import { ApiError, getArchive } from "@/lib/api";
import { formatLocalDate, formatNumber, formatTimestamp, metricShortLabel } from "@/lib/format";
import type { Archive } from "@/lib/types";

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
  title: "Archive",
  description: "Every published daily ranking, with the methodology version that produced it.",
};

const PAGE_SIZE = 40;

export default async function ArchivePage({
  searchParams,
}: {
  searchParams: Promise<{ offset?: string }>;
}) {
  const { offset: offsetParam } = await searchParams;
  // Parsed defensively: the offset reaches the API as a query parameter, and the
  // API's own validation rejects a bad one, but there is no reason to send it.
  const parsed = Number.parseInt(offsetParam ?? "0", 10);
  const offset = Number.isFinite(parsed) && parsed > 0 ? parsed : 0;

  let archive: Archive;
  try {
    archive = await getArchive({ limit: PAGE_SIZE, offset });
  } catch (error) {
    return (
      <LoadFailure
        what="The archive"
        detail={error instanceof ApiError ? `${error.status || "network"} · ${error.message}` : undefined}
      />
    );
  }

  const shownFrom = archive.total === 0 ? 0 : archive.offset + 1;
  const shownTo = archive.offset + archive.count;
  const hasPrevious = archive.offset > 0;
  const hasNext = shownTo < archive.total;

  return (
    <Container className="py-12 sm:py-16">
      <Section>
        <SectionHeading
          as="h1"
          eyebrow="Archive"
          title="Every board that has been published"
          description="Runs are preserved rather than recomputed. A row's methodology version is the version that produced it, which is not necessarily the version running today."
        />

        {archive.total === 0 ? (
          <div className="mt-8">
            <EmptyState title="No runs have been published yet.">
              The archive fills one row per successful pipeline run.
            </EmptyState>
          </div>
        ) : (
          <>
            <p className="mt-6 text-sm text-paper-faint">
              Showing {formatNumber(shownFrom, 0)}–{formatNumber(shownTo, 0)} of{" "}
              {formatNumber(archive.total, 0)} published{" "}
              {archive.total === 1 ? "run" : "runs"}.
            </p>

            <ul className="mt-5 space-y-3">
              {archive.entries.map((entry) => (
                <li key={entry.analysis_date}>
                  <Link
                    href={`/archive/${entry.analysis_date}`}
                    className="surface block p-5 transition-colors hover:border-accent-dim/60"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
                      <div className="min-w-0">
                        <p className="font-display text-lg leading-tight text-paper">
                          {formatLocalDate(entry.analysis_date, "long")}
                        </p>
                        <p className="mt-1 text-sm text-paper-muted">
                          {entry.top_city ? (
                            <>
                              Led by {entry.top_city}
                              {entry.top_metric ? ` · ${metricShortLabel(entry.top_metric)}` : ""}
                              {entry.top_score === null
                                ? ""
                                : ` · score ${formatNumber(entry.top_score, 2)}`}
                            </>
                          ) : (
                            "No ranked events in this run"
                          )}
                        </p>
                      </div>
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge>
                          {formatNumber(entry.event_count, 0)}{" "}
                          {entry.event_count === 1 ? "event" : "events"}
                        </Badge>
                        <Badge>Methodology {entry.methodology_version}</Badge>
                      </div>
                    </div>
                    <p className="mt-3 text-xs text-paper-faint">
                      Published {formatTimestamp(entry.published_at)}
                    </p>
                  </Link>
                </li>
              ))}
            </ul>

            {(hasPrevious || hasNext) && (
              <nav className="mt-8 flex items-center justify-between gap-4" aria-label="Pagination">
                {hasPrevious ? (
                  <Link
                    href={
                      archive.offset - PAGE_SIZE <= 0
                        ? "/archive"
                        : `/archive?offset=${archive.offset - PAGE_SIZE}`
                    }
                    className="rounded-lg border border-ink-700 px-4 py-2 text-sm text-paper-dim transition-colors hover:border-accent-dim hover:text-accent-bright"
                  >
                    ← Newer
                  </Link>
                ) : (
                  <span />
                )}
                {hasNext && (
                  <Link
                    href={`/archive?offset=${archive.offset + PAGE_SIZE}`}
                    className="rounded-lg border border-ink-700 px-4 py-2 text-sm text-paper-dim transition-colors hover:border-accent-dim hover:text-accent-bright"
                  >
                    Older →
                  </Link>
                )}
              </nav>
            )}
          </>
        )}

        <Card className="mt-12 p-6">
          <p className="eyebrow">A note on comparing dates</p>
          <p className="mt-3 text-sm leading-relaxed text-paper-muted">
            Scores are comparable between cities on the same day and between days for the same city,
            because both are probabilities against a fixed reference period. They are not comparable
            across methodology versions: if the seasonal window or the scoring formula changed, a
            score of 3.4 in one version does not mean what a 3.4 means in another. The version on
            each row is there so that comparison can be made deliberately rather than by accident.
          </p>
        </Card>
      </Section>
    </Container>
  );
}
