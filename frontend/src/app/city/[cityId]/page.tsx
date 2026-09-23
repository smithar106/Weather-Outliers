/**
 * One city, in as much detail as the stored data honestly supports.
 *
 * Two requests: the city detail (its registry entry, latest observation, stored
 * seasonal baselines and any scored events) and its observation history for the
 * trend chart. Both are cached page-level fetches, so a visitor never reaches the
 * weather provider.
 *
 * The page is careful about a distinction that is easy to blur: a city with no
 * scored events is a city that had an unremarkable day, not a city with missing
 * data. Those render differently.
 */

import Link from "next/link";
import { notFound } from "next/navigation";

import { DistributionChart } from "@/components/charts/DistributionChart";
import { ScoreBreakdown } from "@/components/charts/ScoreBreakdown";
import { TrendChart } from "@/components/charts/TrendChart";
import {
  Badge,
  Callout,
  Card,
  Container,
  DefRow,
  Dot,
  EmptyState,
  LoadFailure,
  Metric,
  Section,
  SectionHeading,
} from "@/components/ui";
import {
  ApiError,
  getCity,
  getCityHistory,
  isNotFound,
} from "@/lib/api";
import {
  categoryStyle,
  cityLabel,
  countryName,
  dataQualityLabel,
  dataTierLabel,
  dataTierNote,
  directionWord,
  formatDeviation,
  formatLocalDate,
  formatMeasurement,
  formatNumber,
  formatPercent,
  formatTimestamp,
  metricShortLabel,
  NO_VALUE,
  observationTypeLabel,
  observationTypeNote,
} from "@/lib/format";
import type { AnomalyEvent, Baseline, CityDetail, CityHistory, MetricId } from "@/lib/types";

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

interface PageProps {
  params: Promise<{ cityId: string }>;
}

export async function generateMetadata({ params }: PageProps) {
  const { cityId } = await params;
  try {
    const detail = await getCity(cityId);
    return {
      title: cityLabel(detail.city),
      description: `Seasonal baselines, stored observations and anomaly scoring for ${cityLabel(
        detail.city
      )}.`,
    };
  } catch {
    // A metadata failure must not take the page down; the page itself handles it.
    return { title: "City" };
  }
}

export default async function CityPage({ params }: PageProps) {
  const { cityId } = await params;

  let detail: CityDetail;
  try {
    detail = await getCity(cityId);
  } catch (error) {
    if (isNotFound(error)) notFound();
    return (
      <LoadFailure
        what="This city"
        detail={error instanceof ApiError ? `${error.status || "network"} · ${error.message}` : undefined}
      />
    );
  }

  // History is supplementary: a failure here costs one chart, not the page.
  let history: CityHistory | null = null;
  try {
    history = await getCityHistory(cityId, { days: 120 });
  } catch {
    history = null;
  }

  const { city } = detail;
  const primaryMetric: MetricId = detail.events[0]?.metric ?? "temp_max";
  const baselinesByMetric = new Map<MetricId, Baseline>(
    detail.baselines.map((baseline) => [baseline.metric, baseline])
  );

  return (
    <Container className="py-12 sm:py-16">
      <Section>
        <p className="text-sm text-paper-faint">
          <Link href="/" className="link-underline">
            Today
          </Link>{" "}
          <span aria-hidden="true">/</span> {city.name}
        </p>

        <SectionHeading
          as="h1"
          eyebrow={`${countryName(city.country)} · ${city.region}`}
          title={cityLabel(city)}
          description={
            detail.analysis_date
              ? `Showing the analysis for ${formatLocalDate(detail.analysis_date, "long")}.`
              : "No analysis has been published for this city yet."
          }
        />

        {detail.events.length > 0 ? (
          <div className="mt-10">
            <Metric
              label="Most unusual reading"
              value={formatDeviation(detail.events[0].calculation.deviation, detail.events[0].unit)}
              note={`${metricShortLabel(detail.events[0].metric)} — ${directionWord(
                detail.events[0].direction,
                detail.events[0].category
              )}`}
              accent={categoryStyle(detail.events[0].category).color}
            />
          </div>
        ) : (
          <p className="mt-8 text-paper-muted">
            Nothing crossed the scoring threshold for this city on the latest analysis.
          </p>
        )}

        <dl className="mt-10 grid grid-cols-2 gap-x-8 gap-y-6 border-t border-ink-800 pt-8 sm:grid-cols-4">
          <div>
            <dt className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
              Coordinates
            </dt>
            <dd className="tnum mt-1.5 text-base text-paper">
              {formatNumber(city.latitude, 3)}, {formatNumber(city.longitude, 3)}
            </dd>
            <p className="mt-1 text-xs text-paper-faint">City centre, from the registry</p>
          </div>
          <div>
            <dt className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
              Time zone
            </dt>
            <dd className="mt-1.5 text-base text-paper">{city.timezone}</dd>
            <p className="mt-1 text-xs text-paper-faint">IANA identifier</p>
          </div>
          <div>
            <dt className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
              Population
            </dt>
            <dd className="tnum mt-1.5 text-base text-paper">
              {city.population === null ? NO_VALUE : formatNumber(city.population, 0)}
            </dd>
            <p className="mt-1 text-xs text-paper-faint">
              {city.population_source ?? "Not sourced for this city"}
            </p>
          </div>
          <div>
            <dt className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
              Scored events
            </dt>
            <dd className="tnum mt-1.5 text-base text-paper">{formatNumber(detail.events.length, 0)}</dd>
            <p className="mt-1 text-xs text-paper-faint">
              {detail.events.length === 0
                ? "Nothing crossed the scoring threshold"
                : "Every metric that scored, not only the one on the board"}
            </p>
          </div>
        </dl>
      </Section>

      {/* ---------------- Latest observation ---------------- */}
      <Section className="mt-14">
        <h2 className="font-display text-2xl tracking-tight text-paper">Latest stored day</h2>
        {detail.latest_observation ? (
          <ObservationCard observation={detail.latest_observation} />
        ) : (
          <div className="mt-5">
            <EmptyState title="No observations stored for this city yet.">
              Observations are written one completed local day at a time by the scheduled pipeline.
            </EmptyState>
          </div>
        )}
      </Section>

      {/* ---------------- Events ---------------- */}
      <Section className="mt-14">
        <h2 className="font-display text-2xl tracking-tight text-paper">
          Anomaly scoring{detail.analysis_date ? `, ${formatLocalDate(detail.analysis_date, "medium")}` : ""}
        </h2>

        {detail.events.length === 0 ? (
          <div className="mt-5">
            <EmptyState title="Nothing unusual to report for this city.">
              An ordinary day produces no scored events. That is the expected outcome for most cities
              on most days — it is not a gap in the data.
            </EmptyState>
          </div>
        ) : (
          <div className="mt-5 space-y-5">
            {detail.events.map((event) => (
              <EventPanel
                key={event.id}
                event={event}
                baseline={baselinesByMetric.get(event.metric) ?? null}
              />
            ))}
          </div>
        )}
      </Section>

      {/* ---------------- Trend ---------------- */}
      <Section className="mt-14">
        <h2 className="font-display text-2xl tracking-tight text-paper">
          Stored daily observations
        </h2>
        <Card className="mt-5 p-6">
          {history && history.points.length > 0 ? (
            <>
              <p className="text-sm leading-relaxed text-paper-muted">
                {metricShortLabel(primaryMetric)} across every day this deployment has stored for{" "}
                {city.name}, from {formatLocalDate(history.start_date, "medium")} to{" "}
                {formatLocalDate(history.end_date, "medium")}.
              </p>
              <div className="mt-5">
                <TrendChart points={history.points} metric={primaryMetric} />
              </div>
              {history.source_datasets.length > 0 && (
                <p className="mt-4 text-xs leading-relaxed text-paper-faint">
                  Source {history.source_datasets.length === 1 ? "dataset" : "datasets"}:{" "}
                  {history.source_datasets.join(", ")}. The series is short by design — the baseline
                  build stores aggregated seasonal statistics rather than three decades of daily rows,
                  so this chart grows one day per completed pipeline run rather than starting full.
                </p>
              )}
            </>
          ) : (
            <p className="text-sm text-paper-muted">
              No stored observation history is available for this city.
            </p>
          )}
        </Card>
      </Section>

      {/* ---------------- Baselines ---------------- */}
      <Section className="mt-14">
        <h2 className="font-display text-2xl tracking-tight text-paper">
          Seasonal reference distributions
        </h2>
        <p className="prose-desk mt-3 max-w-3xl text-[0.9375rem]">
          What a normal day looks like here at this point in the year, computed from the reference
          period and cached. These are the distributions every score above is measured against.
        </p>

        {detail.baselines.length === 0 ? (
          <div className="mt-5">
            <EmptyState title="No cached baselines for this city yet.">
              Run the baseline build before the daily pipeline; see the methodology page.
            </EmptyState>
          </div>
        ) : (
          <div className="mt-5 space-y-4">
            {detail.baselines.map((baseline) => (
              <BaselineCard key={baseline.metric} baseline={baseline} />
            ))}
          </div>
        )}
      </Section>

      {/* ---------------- Limitations ---------------- */}
      {detail.limitations.length > 0 && (
        <Section className="mt-14">
          <h2 className="font-display text-2xl tracking-tight text-paper">
            What this page does not tell you
          </h2>
          <Card className="mt-5 p-6">
            <ul className="space-y-3.5">
              {detail.limitations.map((limitation) => (
                <li key={limitation} className="flex gap-3 text-sm leading-relaxed text-paper-dim">
                  <span
                    aria-hidden="true"
                    className="mt-2 size-1.5 shrink-0 rounded-full bg-ink-600"
                  />
                  {limitation}
                </li>
              ))}
            </ul>
            <Link
              href="/methodology"
              className="link-underline mt-5 inline-block text-sm text-accent-bright"
            >
              Full methodology and limitations →
            </Link>
          </Card>
        </Section>
      )}
    </Container>
  );
}

// ---------------------------------------------------------------------------

function ObservationCard({ observation }: { observation: CityDetail["latest_observation"] }) {
  if (!observation) return null;
  const readings: Array<{ label: string; value: number | null; unit: string }> = [
    { label: "Daily high", value: observation.temp_max_c, unit: "°C" },
    { label: "Daily low", value: observation.temp_min_c, unit: "°C" },
    { label: "Daily mean", value: observation.temp_mean_c, unit: "°C" },
    { label: "Precipitation", value: observation.precipitation_mm, unit: "mm" },
    { label: "Peak gust", value: observation.wind_gust_max_kmh, unit: "km/h" },
  ];

  return (
    <Card className="mt-5 p-6">
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2">
        <p className="font-display text-lg text-paper">
          {formatLocalDate(observation.local_date, "long")}
        </p>
        <div className="flex flex-wrap gap-2">
          <Badge title={dataTierNote(observation.data_tier)}>
            {dataTierLabel(observation.data_tier)}
          </Badge>
          <Badge title={observationTypeNote(observation.observation_type)}>
            {observationTypeLabel(observation.observation_type)}
          </Badge>
          <Badge>{dataQualityLabel(observation.data_quality)}</Badge>
        </div>
      </div>

      <dl className="mt-6 grid grid-cols-2 gap-x-6 gap-y-5 sm:grid-cols-3 lg:grid-cols-5">
        {readings.map((reading) => (
          <div key={reading.label}>
            <dt className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
              {reading.label}
            </dt>
            <dd className="tnum mt-1.5 text-xl text-paper">
              {formatMeasurement(reading.value, reading.unit)}
            </dd>
          </div>
        ))}
      </dl>

      {observation.missing_fields && observation.missing_fields.length > 0 && (
        <Callout tone="warning" className="mt-5">
          The provider returned no value for{" "}
          {observation.missing_fields.map((field) => field.replace(/_/g, " ")).join(", ")}. Those
          metrics were not scored for this day rather than being filled in.
        </Callout>
      )}

      <dl className="mt-6 border-t border-ink-800 pt-4">
        <DefRow term="Provider" mono>
          {observation.source_provider}
        </DefRow>
        <DefRow term="Dataset" mono>
          {observation.source_dataset}
        </DefRow>
        <DefRow term="Grid cell sampled" mono>
          {observation.grid_latitude === null || observation.grid_longitude === null
            ? NO_VALUE
            : `${formatNumber(observation.grid_latitude, 3)}, ${formatNumber(
                observation.grid_longitude,
                3
              )}`}
        </DefRow>
        <DefRow term="Grid elevation" mono>
          {observation.grid_elevation_m === null
            ? NO_VALUE
            : `${formatNumber(observation.grid_elevation_m, 0)} m`}
        </DefRow>
        <DefRow term="Retrieved" mono>
          {formatTimestamp(observation.retrieved_at)}
        </DefRow>
      </dl>

      <p className="mt-4 text-xs leading-relaxed text-paper-faint">
        {observationTypeNote(observation.observation_type)}{" "}
        {dataTierNote(observation.data_tier)}
      </p>
    </Card>
  );
}

function EventPanel({ event, baseline }: { event: AnomalyEvent; baseline: Baseline | null }) {
  const style = categoryStyle(event.category);

  return (
    <Card className="p-6 sm:p-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <Badge className={style.badge}>
            <Dot color={style.color} />
            {style.label}
          </Badge>
          <h3 className="mt-3 font-display text-xl tracking-tight text-paper">
            {event.metric_label} was {formatDeviation(event.calculation.deviation, event.unit)}{" "}
            {directionWord(event.direction, event.category)}
          </h3>
          <p className="tnum mt-1.5 text-sm text-paper-muted">
            {formatMeasurement(event.observed_value, event.unit)} observed ·{" "}
            {formatMeasurement(
              baseline?.metric === "precipitation" || baseline?.metric === "wind_gust"
                ? event.baseline.median
                : event.baseline.mean,
              event.unit
            )}{" "}
            typical
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Badge title={dataTierNote(event.data_tier)}>{dataTierLabel(event.data_tier)}</Badge>
          <Badge title={observationTypeNote(event.observation_type)}>
            {observationTypeLabel(event.observation_type)}
          </Badge>
        </div>
      </div>

      {baseline?.histogram && (
        <div className="mt-7">
          <p className="eyebrow">Where this day fell in the seasonal sample</p>
          <div className="mt-3">
            <DistributionChart
              histogram={baseline.histogram}
              unit={event.unit}
              observedValue={event.observed_value}
              color={style.color}
              sampleSize={baseline.n_samples}
              label={event.metric_label}
            />
          </div>
        </div>
      )}

      <div className="mt-8 border-t border-ink-800 pt-7">
        <ScoreBreakdown
          calculation={event.calculation}
          baseline={event.baseline}
          unit={event.unit}
          color={style.color}
        />
      </div>

      {event.explanation && (
        <div className="mt-8 border-t border-ink-800 pt-6">
          <p className="eyebrow">Explanation</p>
          <p className="mt-3 font-display text-lg leading-snug text-paper">
            {event.explanation.headline}
          </p>
          <div className="prose-desk mt-3 space-y-3 text-sm">
            <p>{event.explanation.statistical_explanation}</p>
            <p>{event.explanation.historical_context}</p>
            <p className="text-paper-faint">{event.explanation.caveats}</p>
          </div>
          <p className="mt-4 text-xs text-paper-faint">
            {event.explanation.generator === "llm"
              ? `Written by ${event.explanation.llm_provider ?? "a language model"}${
                  event.explanation.model ? ` (${event.explanation.model})` : ""
                } from ${event.explanation.tool_call_count} tool call${
                  event.explanation.tool_call_count === 1 ? "" : "s"
                } against the figures above.`
              : "Written by the deterministic template, not a language model."}
            {event.explanation.fallback_reason
              ? ` Fallback reason: ${event.explanation.fallback_reason}.`
              : ""}
          </p>
        </div>
      )}

      <div className="mt-6 flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-ink-800 pt-4 text-xs text-paper-faint">
        <span>Statistical outlier · not checked against any official records archive</span>
        <code className="tnum ml-auto">{event.id}</code>
      </div>
    </Card>
  );
}

function BaselineCard({ baseline }: { baseline: Baseline }) {
  const style = categoryStyle(
    baseline.metric === "precipitation"
      ? "precipitation"
      : baseline.metric === "wind_gust"
        ? "wind"
        : "temperature"
  );
  const unit =
    baseline.metric === "precipitation" ? "mm" : baseline.metric === "wind_gust" ? "km/h" : "°C";

  return (
    <Card className="p-6">
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2">
        <h3 className="font-display text-lg text-paper">{metricShortLabel(baseline.metric)}</h3>
        <div className="flex flex-wrap gap-2">
          <Badge>{baseline.reference_period}</Badge>
          <Badge>±{baseline.seasonal_window_days} days</Badge>
          {!baseline.sufficient && (
            <Badge className="bg-warning/10 text-warning ring-warning/30">
              Below minimum sample
            </Badge>
          )}
        </div>
      </div>

      <dl className="mt-5 grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
        <Figure label="Mean" value={formatMeasurement(baseline.mean, unit)} />
        <Figure label="Median" value={formatMeasurement(baseline.median, unit)} />
        <Figure
          label="Interquartile range"
          value={
            baseline.p25 === null || baseline.p75 === null
              ? NO_VALUE
              : `${formatNumber(baseline.p25, 1)} – ${formatNumber(baseline.p75, 1)} ${unit}`
          }
        />
        <Figure
          label="Sample"
          value={`${formatNumber(baseline.n_samples, 0)} days`}
          note={`${formatNumber(baseline.n_years, 0)} years`}
        />
        <Figure label="Sample minimum" value={formatMeasurement(baseline.min_value, unit)} />
        <Figure label="Sample maximum" value={formatMeasurement(baseline.max_value, unit)} />
        {baseline.zero_fraction !== null && (
          <Figure
            label="Dry days"
            value={formatPercent(baseline.zero_fraction)}
            note={
              baseline.nonzero_n === null
                ? undefined
                : `${formatNumber(baseline.nonzero_n, 0)} wet days in sample`
            }
          />
        )}
        <Figure
          label="Standard deviation"
          value={baseline.std === null ? "Not published" : formatMeasurement(baseline.std, unit)}
          note={
            baseline.std === null
              ? "Withheld: this distribution is too skewed for it to be meaningful"
              : undefined
          }
        />
      </dl>

      {baseline.histogram && !baseline.histogram.degenerate && (
        <div className="mt-6">
          <DistributionChart
            histogram={baseline.histogram}
            unit={unit}
            color={style.color}
            sampleSize={baseline.n_samples}
            label={metricShortLabel(baseline.metric)}
          />
        </div>
      )}

      <p className="mt-5 text-xs leading-relaxed text-paper-faint">
        Computed by this project from {baseline.source_dataset} under methodology{" "}
        {baseline.methodology_version}. Not official climatological normals.
      </p>
    </Card>
  );
}

function Figure({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div>
      {/*
       * The label steps down below `sm`. "Interquartile range" is the longest of
       * these, and at 11px with 0.1em tracking its first word alone measures
       * ~109px — wider than the 104px column this two-column grid leaves on a
       * 320px screen. It is a single word, so no wrap can rescue it; the tracking
       * and size have to give instead.
       */}
      <dt className="text-[0.625rem] font-semibold uppercase tracking-[0.07em] text-paper-faint sm:text-[0.6875rem] sm:tracking-[0.1em]">
        {label}
      </dt>
      <dd className="tnum mt-1.5 text-base text-paper-dim">{value}</dd>
      {note && <p className="mt-1 text-xs leading-snug text-paper-faint">{note}</p>}
    </div>
  );
}
