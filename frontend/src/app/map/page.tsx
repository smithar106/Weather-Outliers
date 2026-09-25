import Link from "next/link";

import { AnomalyMap } from "@/components/AnomalyMap";
import { Callout, Container, EmptyState, LoadFailure, Section, SectionHeading } from "@/components/ui";
import { ApiError, getLatestRankings } from "@/lib/api";
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

export const metadata = {
  title: "Map",
  description:
    "The ranked cities on a map of North America, coloured by weather category. Tiles from Mapbox and OpenStreetMap.",
  alternates: { canonical: "/map" },
  openGraph: { title: "Map", url: "/map" },
};

export default async function MapPage() {
  // A Mapbox public token (`pk.…`) is read server-side and passed to the client
  // map as a prop, so it is never baked into the bundle at build time. It is a
  // public credential by design and safe to expose to the browser.
  const mapboxToken = process.env.MAPBOX_TOKEN ?? null;

  let rankings: Rankings;
  try {
    rankings = await getLatestRankings();
  } catch (error) {
    return (
      <LoadFailure
        what="The map's ranking data"
        detail={error instanceof ApiError ? `${error.status || "network"} · ${error.message}` : undefined}
      />
    );
  }

  return (
    <Container className="py-12 sm:py-16">
      <Section>
        <SectionHeading
          as="h1"
          eyebrow="Map"
          title="Where yesterday went off script"
          description={
            <>
              Each marker is one ranked city, numbered by rank and coloured by the category of its
              most improbable reading. Analysis date{" "}
              {formatLocalDate(rankings.analysis_date, "medium")}, published{" "}
              {formatTimestamp(rankings.published_at)}.
            </>
          }
        />

        {!rankings.is_latest_available && rankings.requested_date && (
          <Callout tone="warning" className="mt-6">
            The most recent successful run is shown, for{" "}
            {formatLocalDate(rankings.analysis_date, "medium")}.
          </Callout>
        )}

        <div className="mt-8">
          {rankings.events.length === 0 ? (
            <EmptyState title="This run published no ranked events, so there is nothing to place on the map.">
              The pipeline completed but found nothing that met the minimum sample-size and data
              quality rules.
            </EmptyState>
          ) : (
            <AnomalyMap events={rankings.events} accessToken={mapboxToken} />
          )}
        </div>

        <Callout tone="neutral" className="mt-6">
          Markers sit at each city&apos;s own coordinates. The underlying weather values are model
          estimates for the nearest grid cell of a reanalysis dataset, which is a few kilometres wide
          — so a marker shows which city was analysed, not the exact point that was sampled. Each
          city&apos;s{" "}
          <Link href="/methodology" className="link-underline text-accent-bright">
            detail page and the methodology
          </Link>{" "}
          give the grid coordinates actually used.
        </Callout>
      </Section>
    </Container>
  );
}
