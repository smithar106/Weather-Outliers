import { Badge, Container, Section, SectionHeading } from "@/components/ui";
import { getMethodology } from "@/lib/api";
import type { Methodology } from "@/lib/types";

export const dynamic = "force-dynamic";
export const fetchCache = "default-cache";

export const metadata = {
  title: "Methodology",
  description:
    "How the daily statistical outlier board is computed, in four paragraphs.",
  alternates: { canonical: "/methodology" },
  openGraph: { title: "Methodology", url: "/methodology" },
};

export default async function MethodologyPage() {
  const methodology: Methodology | null = await getMethodology().catch(() => null);
  const period = methodology?.reference_period ?? "1991–2020";

  return (
    <Container className="py-12 sm:py-16">
      <Section>
        <SectionHeading
          as="h1"
          eyebrow="Methodology"
          title="How an unusual day is identified"
        />

        {methodology && (
          <div className="mt-5 flex flex-wrap gap-2">
            <Badge>{period} reference period</Badge>
            <Badge>Methodology {methodology.methodology_version}</Badge>
          </div>
        )}

        <div className="prose-desk mt-8 max-w-3xl space-y-5 text-[0.9375rem]">
          <p>
            Every day, each of the 50 North American cities is compared against its own
            thirty-year ({period}) seasonal history to find the readings that were
            statistically unusual — the outliers, not simply the extreme values.
          </p>
          <p>
            Events are ranked by an anomaly score built from the tail probability — how
            improbable a reading that extreme is for that city at that time of year — plus a
            margin term for readings beyond the entire reference sample. The result is
            &ldquo;how improbable&rdquo;, not &ldquo;how large&rdquo;: a cold snap in Mérida can outrank a
            hotter day in Phoenix.
          </p>
          <p>
            The values are gridded reanalysis estimates (ERA5, via Open-Meteo) for the grid
            cell nearest each city — model estimates, not readings from a weather station
            inside the city. Each figure is labelled with its source dataset and its tier
            (final or provisional).
          </p>
          <p>
            These are statistical outliers, not weather records. No authoritative records
            archive is consulted, and nothing here is verified as a city, state, or national
            record. The full calculation, data sources, and limitations remain in the API and
            the repository.
          </p>
        </div>
      </Section>
    </Container>
  );
}
