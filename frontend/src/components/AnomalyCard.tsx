import Link from "next/link";

import { Badge, Card, Dot } from "@/components/ui";
import {
  categoryStyle,
  cityLabel,
  countryName,
  dataTierLabel,
  describeRarity,
  directionWord,
  formatDeviation,
  formatMeasurement,
  formatNumber,
  formatPercentile,
  formatZScore,
  metricShortLabel,
} from "@/lib/format";
import type { RankedEvent } from "@/lib/types";

/**
 * One ranked event.
 *
 * The card is built so that no figure can appear without the qualifier that makes
 * it honest:
 *
 * * the observed value sits next to the baseline it is being compared against;
 * * the percentile carries the sample size it was measured from;
 * * the rarity sentence comes from {@link describeRarity}, which downgrades to a
 *   lower bound when the tail probability is capped by sample size;
 * * the z-score is omitted entirely, with a reason, for metrics where it would not
 *   be comparable;
 * * the explanation is attributed to a model or to the deterministic templates.
 */
export function AnomalyCard({ ranked }: { ranked: RankedEvent }) {
  const { rank, event } = ranked;
  const style = categoryStyle(event.category);
  const explanation = event.explanation;
  const zScore = formatZScore(event.calculation);
  const baselineReference =
    event.category === "precipitation" || event.category === "wind"
      ? { label: "Seasonal median", value: event.baseline.median }
      : { label: "Seasonal mean", value: event.baseline.mean };

  return (
    <Card as="article" className="overflow-hidden">
      <div className="flex flex-col gap-6 p-5 sm:flex-row sm:p-6">
        <RankMark rank={rank} color={style.color} />

        <div className="min-w-0 flex-1">
          <header className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
            <div className="min-w-0">
              <h3 className="font-display text-xl leading-tight tracking-tight text-paper">
                <Link
                  href={`/city/${event.city.id}`}
                  className="transition-colors hover:text-accent-bright"
                >
                  {cityLabel(event.city)}
                </Link>
              </h3>
              <p className="mt-1 text-sm text-paper-muted">
                {countryName(event.city.country)} · {event.city.region}
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Badge className={style.badge}>
                <Dot color={style.color} />
                {style.label}
              </Badge>
              <Badge title={`Data tier: ${dataTierLabel(event.data_tier)}`}>
                {dataTierLabel(event.data_tier)}
              </Badge>
            </div>
          </header>

          <div className="mt-5 grid gap-x-8 gap-y-4 sm:grid-cols-2 lg:grid-cols-4">
            <figure>
              <figcaption className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
                {metricShortLabel(event.metric)}
              </figcaption>
              <p className={`tnum mt-1 text-[1.75rem] leading-none ${style.text}`}>
                {formatMeasurement(event.observed_value, event.unit)}
              </p>
              <p className="mt-1.5 text-xs text-paper-faint">
                {formatDeviation(event.calculation.deviation, event.unit)}{" "}
                {directionWord(event.direction, event.category)}
              </p>
            </figure>

            <div>
              <p className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
                {baselineReference.label}
              </p>
              <p className="tnum mt-1 text-lg text-paper-dim">
                {formatMeasurement(baselineReference.value, event.unit)}
              </p>
              <p className="mt-1.5 text-xs text-paper-faint">
                1991–2020, ±7 days of this date
              </p>
            </div>

            <div>
              <p className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
                Percentile
              </p>
              <p className="tnum mt-1 text-lg text-paper-dim">
                {formatPercentile(event.calculation.percentile)}
              </p>
              <p className="mt-1.5 text-xs text-paper-faint">
                of {formatNumber(event.baseline.n, 0)} comparable days
              </p>
            </div>

            <div>
              <p className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
                Anomaly score
              </p>
              <p className="tnum mt-1 text-lg text-paper-dim">
                {formatNumber(event.calculation.anomaly_score, 2)}
              </p>
              <p className="mt-1.5 text-xs text-paper-faint">
                {zScore ? `z = ${zScore}` : "z-score withheld for this distribution"}
              </p>
            </div>
          </div>

          <p className="mt-5 border-l-2 border-ink-700 pl-4 text-sm leading-relaxed text-paper-muted">
            {describeRarity(event.calculation)}
          </p>

          {explanation && (
            <div className="mt-6 rounded-xl border border-ink-700/80 bg-ink-900/60 p-4 sm:p-5">
              <p className="font-display text-[1.0625rem] leading-snug text-paper">
                {explanation.headline}
              </p>
              <p className="mt-2.5 text-sm leading-relaxed text-paper-dim">
                {explanation.statistical_explanation}
              </p>
            </div>
          )}

          {/*
           * No provenance strip here. Observation type, source dataset and
           * methodology version are the same three strings on all ten cards, so
           * repeating them ten times told a reader nothing and cost a line of
           * chrome each time. The data-tier badge in the header still marks
           * modelled data as modelled, the caveats paragraph behind "Full
           * breakdown" still says so in words, and the city page carries the
           * dataset and methodology in full.
           */}
          <footer className="mt-5 flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-paper-faint">
            <Link
              href={`/city/${event.city.id}`}
              className="link-underline text-accent-bright"
            >
              Full breakdown →
            </Link>
          </footer>
        </div>
      </div>
    </Card>
  );
}

function RankMark({ rank, color }: { rank: number; color: string }) {
  return (
    <div className="flex shrink-0 items-start gap-3 sm:w-14 sm:flex-col sm:items-center">
      <span
        className="tnum text-3xl font-semibold leading-none"
        style={{ color }}
        aria-label={`Rank ${rank}`}
      >
        {String(rank).padStart(2, "0")}
      </span>
      <span
        aria-hidden="true"
        className="mt-3 hidden w-px flex-1 bg-gradient-to-b from-ink-700 to-transparent sm:block"
      />
    </div>
  );
}
