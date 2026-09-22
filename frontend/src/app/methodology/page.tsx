/**
 * The methodology page.
 *
 * Everything on this page is read from `GET /api/methodology`, which the backend
 * assembles from the same constants the pipeline computes with. Nothing is
 * hard-coded here. If the reference period, the seasonal window, the tie-break
 * chain or the score formula ever change, this page changes with them — a page
 * that described the statistics from memory would eventually describe a version
 * of the code that no longer runs.
 */

import Link from "next/link";

import {
  Badge,
  Callout,
  Card,
  Container,
  DefRow,
  LoadFailure,
  Section,
  SectionHeading,
} from "@/components/ui";
import { ApiError, getMethodology } from "@/lib/api";
import { categoryStyle, formatNumber, formatUsd, metricShortLabel } from "@/lib/format";
import type { Methodology } from "@/lib/types";

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
  title: "Methodology",
  description:
    "The exact formulas, baseline period, ranking logic and limitations behind the daily outlier board.",
};

export default async function MethodologyPage() {
  let methodology: Methodology;
  try {
    methodology = await getMethodology();
  } catch (error) {
    return (
      <LoadFailure
        what="The methodology"
        detail={error instanceof ApiError ? `${error.status || "network"} · ${error.message}` : undefined}
      />
    );
  }

  const { ai } = methodology;

  return (
    <Container className="py-12 sm:py-16">
      <Section>
        <SectionHeading
          as="h1"
          eyebrow="Methodology"
          title="How an unusual day is identified"
          description="Every figure below is served by the API from the same constants the pipeline computes with, so this page cannot drift from the code that produced the board."
        />
        <div className="mt-5 flex flex-wrap gap-2">
          <Badge>Methodology {methodology.methodology_version}</Badge>
          {methodology.registry_version && (
            <Badge>City registry {methodology.registry_version}</Badge>
          )}
          <Badge>{methodology.reference_period} reference period</Badge>
        </div>
      </Section>

      {/* ------------------------------------------------------------------ */}
      <Section className="mt-14">
        <h2 className="font-display text-2xl tracking-tight text-paper">
          1. Outliers, not records
        </h2>
        <div className="prose-desk mt-4 max-w-3xl space-y-4 text-[0.9375rem]">
          <p>
            This site answers one question: how improbable was yesterday, for this city, at this time
            of year? It does not answer whether yesterday broke a record. Those are different
            questions with different sources, and conflating them is the most common way a weather
            statistic becomes wrong.
          </p>
          <p>
            No authoritative records archive is consulted anywhere in the pipeline. The API says so
            explicitly: every event carries{" "}
            <code>is_statistical_outlier: true</code> and{" "}
            <code>is_verified_official_record: false</code>. A reading here can be the most unusual
            in thirty years of the reference dataset and still be nowhere near the city&apos;s
            all-time record, because the reference period is thirty years and the record book is
            longer.
          </p>
        </div>
      </Section>

      {/* ------------------------------------------------------------------ */}
      <Section className="mt-14">
        <h2 className="font-display text-2xl tracking-tight text-paper">
          2. The seasonal reference distribution
        </h2>
        <div className="prose-desk mt-4 max-w-3xl space-y-4 text-[0.9375rem]">
          <p>
            A day is only unusual relative to something. The comparison here is each city against
            its own history in the same part of the year — never against another city, and never
            against an annual average, which would make every July day look hot.
          </p>
          <p>
            For each city, metric and calendar day, the pipeline collects every day in the reference
            period that falls within a window of ±{methodology.seasonal_window_days} days of the same
            point in the year — {methodology.seasonal_window_day_count} candidate days in each year of
            the reference period. That pooled sample is the reference distribution. Its shape is
            stored — percentiles, interquartile range, a histogram — and reused, so the site does not
            re-download three decades of weather every morning.
          </p>
        </div>

        <Card className="mt-6 p-6">
          <dl>
            <DefRow term="Reference period" mono>
              {methodology.reference_period}
            </DefRow>
            <DefRow term="Seasonal window" mono>
              ±{methodology.seasonal_window_days} days ({methodology.seasonal_window_day_count} days
              per year)
            </DefRow>
            <DefRow term="Calendar">{methodology.calendar}</DefRow>
            <DefRow term="Leap days">{methodology.leap_day_handling}</DefRow>
            <DefRow term="Minimum sample" mono>
              {formatNumber(methodology.min_samples, 0)} days across at least{" "}
              {formatNumber(methodology.min_years, 0)} years
            </DefRow>
            <DefRow term="Minimum wet days for precipitation" mono>
              {formatNumber(methodology.min_wet_days, 0)}
            </DefRow>
          </dl>
          <p className="mt-4 text-sm leading-relaxed text-paper-muted">
            A city/metric whose reference sample falls short of these minimums is excluded from
            ranking rather than ranked with a wide error bar the interface would have to explain
            away.
          </p>
        </Card>

        <Callout tone="warning" className="mt-4">
          These are not official climatological normals. Normals are published by national
          meteorological services using their own station records, quality control and homogenisation
          procedures. The distributions here are computed by this project from a gridded reanalysis
          archive, and the API labels every one of them{" "}
          <code>not_official_normals: true</code>.
        </Callout>
      </Section>

      {/* ------------------------------------------------------------------ */}
      <Section className="mt-14">
        <h2 className="font-display text-2xl tracking-tight text-paper">
          3. Scoring, and why not degrees
        </h2>
        <div className="prose-desk mt-4 max-w-3xl space-y-4 text-[0.9375rem]">
          <p>{methodology.ranking_basis}</p>
          <p>
            The consequence is worth stating plainly, because it is the reason the board looks the
            way it does: a 41 °C day in Phoenix scores near zero, because Phoenix has hundreds of
            them in the reference sample. A 31 °C day in Anchorage scores very high, because
            Anchorage has none. Ranking by temperature would produce a list of hot cities. Ranking by
            improbability produces a list of unusual days.
          </p>
        </div>

        <Card className="mt-6 p-6">
          <p className="eyebrow">Score formula</p>
          <p className="mt-3 rounded-lg border border-ink-700 bg-ink-900 px-4 py-3 font-mono text-[0.8125rem] leading-relaxed text-paper-dim">
            {methodology.score_formula}
          </p>
          <div className="mt-6 grid gap-5 sm:grid-cols-2">
            <div>
              <p className="text-sm font-semibold text-paper">Surprisal — the ranking term</p>
              <p className="mt-1.5 text-sm leading-relaxed text-paper-muted">
                The negative base-10 logarithm of the empirical one-sided tail probability. A
                one-in-a-hundred day scores 2, a one-in-a-thousand day scores 3. Because it is a
                probability rather than a magnitude, it is comparable between a temperature in
                degrees and a rainfall total in millimetres.
              </p>
            </div>
            <div>
              <p className="text-sm font-semibold text-paper">Margin bonus — the tail-cap fix</p>
              <p className="mt-1.5 text-sm leading-relaxed text-paper-muted">
                An empirical probability cannot go below 1/(n+1), so every reading beyond the entire
                reference sample would otherwise tie. The margin term adds a bounded credit for how
                far beyond the sample the reading fell, measured in interquartile ranges. It is
                capped by the logarithm so a single extraordinary value cannot dominate the board.
              </p>
            </div>
          </div>
        </Card>

        <Card className="mt-4 p-6">
          <p className="eyebrow">Tie-breaking</p>
          <p className="mt-2 text-sm leading-relaxed text-paper-muted">
            Applied in order, so the same inputs always produce the same board. The final key is an
            identifier, which guarantees the chain always terminates.
          </p>
          <ol className="mt-4 space-y-2">
            {methodology.tiebreak_chain.map((step, index) => (
              <li key={step} className="flex gap-3 text-sm text-paper-dim">
                <span className="tnum shrink-0 text-paper-faint">{index + 1}.</span>
                <code className="text-[0.8125rem]">{step}</code>
              </li>
            ))}
          </ol>
        </Card>
      </Section>

      {/* ------------------------------------------------------------------ */}
      <Section className="mt-14">
        <h2 className="font-display text-2xl tracking-tight text-paper">4. Metrics</h2>
        <p className="prose-desk mt-4 max-w-3xl text-[0.9375rem]">
          Each metric is treated according to the shape of its distribution rather than pushed
          through a single formula. A z-score is published only where the seasonal distribution is
          close enough to symmetric for one to mean something; for zero-heavy precipitation and
          one-sided gusts it is withheld, because a number that looks comparable but is not is worse
          than no number.
        </p>

        <div className="mt-6 overflow-x-auto">
          <table className="w-full min-w-[46rem] border-collapse text-sm">
            <thead>
              <tr className="border-b border-ink-700 text-left">
                <Th>Metric</Th>
                <Th>Unit</Th>
                <Th>Tails</Th>
                <Th>Reference</Th>
                <Th>Distribution model</Th>
                <Th>z published</Th>
              </tr>
            </thead>
            <tbody>
              {methodology.metrics.map((metric) => {
                const style = categoryStyle(metric.category);
                return (
                  <tr key={metric.metric} className="border-b border-ink-800 last:border-0">
                    <td className="py-3 pr-4">
                      <span className="block text-paper">{metric.label}</span>
                      <span className={`block text-xs ${style.text}`}>
                        {metricShortLabel(metric.metric)} · {style.label}
                      </span>
                    </td>
                    <td className="tnum py-3 pr-4 text-paper-dim">{metric.unit}</td>
                    <td className="py-3 pr-4 text-paper-dim">
                      {metric.tails === "both" ? "Both" : "Upper only"}
                    </td>
                    <td className="py-3 pr-4 text-paper-dim">{metric.deviation_reference}</td>
                    <td className="py-3 pr-4 text-paper-muted">{metric.distribution_model}</td>
                    <td className="py-3 text-paper-dim">
                      {metric.z_score_published ? (
                        "Yes"
                      ) : (
                        <span className="text-paper-faint">Withheld</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        <Card className="mt-6 p-6">
          <p className="eyebrow">Zero-heavy precipitation</p>
          <p className="mt-2 text-sm leading-relaxed text-paper-muted">
            Most days in most cities are dry, so a precipitation sample is a spike at zero plus a
            skewed tail. Fitting one distribution to that produces nonsense. The tail probability is
            instead factored as{" "}
            <code>P(X ≥ x) = P(X &gt; 0) · P(X ≥ x | X &gt; 0)</code>: the chance of a wet day at
            all, times the chance of a wet day being at least this wet. Both factors come from the
            same reference sample, and the wet-day fraction is stored alongside the distribution so
            it can be checked.
          </p>
        </Card>
      </Section>

      {/* ------------------------------------------------------------------ */}
      <Section className="mt-14">
        <h2 className="font-display text-2xl tracking-tight text-paper">5. The daily board</h2>
        <Card className="mt-5 p-6">
          <dl>
            <DefRow term="Events published" mono>
              Top {formatNumber(methodology.ranking_top_n, 0)}
            </DefRow>
            <DefRow term="One event per city">
              {methodology.one_event_per_city
                ? "Yes — the board shows each city's single most improbable reading"
                : "No"}
            </DefRow>
          </dl>
          <p className="mt-4 text-sm leading-relaxed text-paper-muted">
            {methodology.one_event_per_city
              ? "Without this rule a single deep cold snap could fill the board with one city's daily high, daily low and daily mean, which are three views of the same weather. Every event a city produced is still scored and stored, and appears on that city's detail page and in the API."
              : "All scored events are eligible for the board, including several from the same city."}
          </p>
        </Card>
      </Section>

      {/* ------------------------------------------------------------------ */}
      <Section className="mt-14">
        <h2 className="font-display text-2xl tracking-tight text-paper">
          6. The written explanations
        </h2>
        <div className="prose-desk mt-4 max-w-3xl space-y-4 text-[0.9375rem]">
          <p>{ai.precompute_note}</p>
          <p>{ai.grounding}</p>
        </div>

        <div className="mt-6 grid gap-4 lg:grid-cols-2">
          <Card className="p-6">
            <p className="eyebrow">Configuration of this deployment</p>
            <dl className="mt-3">
              <DefRow term="Explanations">
                {ai.explanations_enabled ? "Enabled" : "Disabled"}
              </DefRow>
              <DefRow term="Language model">
                {ai.llm_enabled ? `${ai.provider}${ai.model ? ` · ${ai.model}` : ""}` : "Not configured"}
              </DefRow>
              <DefRow term="Default generator">
                {ai.generator_default === "llm" ? "Language model" : "Deterministic templates"}
              </DefRow>
              <DefRow term="Precomputed">{ai.precomputed ? "Yes" : "No"}</DefRow>
            </dl>
            {!ai.llm_enabled && (
              <Callout tone="info" className="mt-4">
                No model key is configured, so every explanation on this deployment was written by
                the deterministic templates. The card footers say so individually — the site never
                implies a model wrote text it did not write.
              </Callout>
            )}
          </Card>

          <Card className="p-6">
            <p className="eyebrow">Bounds per event</p>
            <dl className="mt-3">
              <DefRow term="Events investigated" mono>
                {formatNumber(ai.bounds.events_investigated, 0)}
              </DefRow>
              <DefRow term="Max tool calls" mono>
                {formatNumber(ai.bounds.max_tool_calls_per_event, 0)}
              </DefRow>
              <DefRow term="Max iterations" mono>
                {formatNumber(ai.bounds.max_iterations_per_event, 0)}
              </DefRow>
              <DefRow term="Max retries" mono>
                {formatNumber(ai.bounds.max_retries_per_event, 0)}
              </DefRow>
              <DefRow term="Timeout" mono>
                {formatNumber(ai.bounds.timeout_seconds_per_event, 0)} s
              </DefRow>
              <DefRow term="Monthly call ceiling" mono>
                {formatNumber(ai.bounds.monthly_max_llm_calls, 0)}
              </DefRow>
              <DefRow term="Monthly budget" mono>
                {formatUsd(ai.bounds.monthly_usd_budget)}
              </DefRow>
            </dl>
          </Card>
        </div>

        <Card className="mt-4 p-6">
          <p className="eyebrow">Tools the agent may call</p>
          <p className="mt-2 text-sm leading-relaxed text-paper-muted">
            The agent has no free-text access to the database and no web access. It can call these
            functions and nothing else, and every number it prints must have come back from one of
            them.
          </p>
          <ul className="mt-4 flex flex-wrap gap-2">
            {ai.tools.map((tool) => (
              <li key={tool}>
                <code className="rounded-md border border-ink-700 bg-ink-900 px-2.5 py-1 text-xs text-paper-dim">
                  {tool}
                </code>
              </li>
            ))}
          </ul>
          <p className="mt-5 text-sm leading-relaxed text-paper-muted">
            <span className="font-semibold text-paper">Fallback.</span> {ai.fallback}
          </p>
        </Card>
      </Section>

      {/* ------------------------------------------------------------------ */}
      <Section className="mt-14">
        <h2 className="font-display text-2xl tracking-tight text-paper">7. Data sources</h2>
        <div className="mt-5 grid gap-4 lg:grid-cols-2">
          {methodology.data_sources.map((source) => (
            <Card key={`${source.name}-${source.dataset}`} className="p-5">
              <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                <a
                  href={source.url}
                  target="_blank"
                  rel="noreferrer"
                  className="link-underline text-[0.9375rem] text-accent-bright"
                >
                  {source.name}
                </a>
                <Badge>{source.observation_type.replace(/_/g, " ")}</Badge>
              </div>
              <dl className="mt-3">
                <DefRow term="Dataset" mono>
                  {source.dataset}
                </DefRow>
                <DefRow term="Licence">{source.licence}</DefRow>
                <DefRow term="Attribution">{source.attribution}</DefRow>
              </dl>
              {source.note && (
                <p className="mt-3 text-xs leading-relaxed text-paper-faint">{source.note}</p>
              )}
            </Card>
          ))}
        </div>
      </Section>

      {/* ------------------------------------------------------------------ */}
      <Section className="mt-14">
        <h2 className="font-display text-2xl tracking-tight text-paper">8. Limitations</h2>
        <p className="prose-desk mt-4 max-w-3xl text-[0.9375rem]">
          Served by the API rather than written here, so the list a reader sees is the list the
          running code acknowledges.
        </p>
        <Card className="mt-5 p-6">
          <ul className="space-y-3.5">
            {methodology.limitations.map((limitation) => (
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
          <p className="eyebrow">Verification</p>
          <p className="prose-desk mt-3 max-w-3xl text-[0.9375rem]">
            Claims about statistical behaviour are worth exactly as much as the tests behind them.
            The evaluation page publishes the output of the project&apos;s own harness — synthetic
            distribution cases, grounding checks against labelled examples of hallucinated records
            and causes, and a reproducibility check that reruns a pipeline date and compares every
            published field.
          </p>
          <Link
            href="/evaluation"
            className="link-underline mt-4 inline-block text-sm text-accent-bright"
          >
            See the measured results →
          </Link>
        </Card>
      </Section>
    </Container>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return (
    <th className="pb-2.5 pr-4 text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
      {children}
    </th>
  );
}
