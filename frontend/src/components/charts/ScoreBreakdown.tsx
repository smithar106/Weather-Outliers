/**
 * How one event's anomaly score was assembled.
 *
 * `score = surprisal + margin_bonus`, where surprisal is `-log10(p_tail)` and the
 * margin bonus is `0.5 * log10(1 + margin / IQR)` for readings that fall outside
 * the reference sample entirely. Both terms are stored columns, so this component
 * does no arithmetic of its own beyond laying them out proportionally — there is
 * nothing here that could disagree with the database.
 *
 * The bar is a visual aid, not the claim. Every number is also printed.
 */

import { Badge } from "@/components/ui";
import {
  describeRarity,
  formatMeasurement,
  formatNumber,
  formatPercentile,
  formatProbability,
  formatReturnPeriod,
  formatZScore,
} from "@/lib/format";
import type { EventBaseline, EventCalculation } from "@/lib/types";

/**
 * The top of the plotted scale, in score units.
 *
 * 4 means a tail probability of 1e-4: about one day in 10,000 comparable days, or
 * roughly a 1-in-27-year event in a seasonal window of this width. Scores above it
 * are possible and are labelled as running off the scale rather than being clipped
 * silently — but a fixed ceiling is what makes two events visually comparable, which
 * a self-normalising bar can never be.
 */
const SCALE_MAX = 4;

/** Gridlines labelled in odds, since a score is a probability in disguise. */
const SCALE_TICKS = [
  { score: 1, label: "1 in 10" },
  { score: 2, label: "1 in 100" },
  { score: 3, label: "1 in 1k" },
  { score: 4, label: "1 in 10k" },
];

export function ScoreBreakdown({
  calculation,
  baseline,
  unit,
  color,
}: {
  calculation: EventCalculation;
  baseline: EventBaseline;
  unit: string;
  color: string;
}) {
  const zScore = formatZScore(calculation);

  // Plotted against a fixed scale rather than against the score's own total.
  // Normalising by the total made every bar exactly full, which carried no
  // information and read as "this event maxed out the scale". Because surprisal is
  // −log10(p), a linear axis in score units is a logarithmic axis in probability,
  // and the gridlines below are labelled with the odds they correspond to.
  const surprisalShare = Math.max(0, Math.min(1, calculation.surprisal / SCALE_MAX));
  const marginShare = Math.max(
    0,
    Math.min(1 - surprisalShare, calculation.margin_bonus / SCALE_MAX)
  );
  const offScale = calculation.anomaly_score > SCALE_MAX;

  return (
    <div>
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
        <p className="eyebrow">Anomaly score</p>
        <p className="tnum text-2xl text-paper">{formatNumber(calculation.anomaly_score, 3)}</p>
      </div>

      <div className="mt-3">
        <div
          className="relative h-3 w-full overflow-hidden rounded-full bg-ink-800"
          role="img"
          aria-label={`Score of ${formatNumber(calculation.anomaly_score, 3)} on a scale to ${SCALE_MAX}, composed of ${formatNumber(
            calculation.surprisal,
            3
          )} from rarity and ${formatNumber(calculation.margin_bonus, 3)} from the margin beyond the reference sample.`}
        >
          <div className="flex h-full">
            <span style={{ width: `${surprisalShare * 100}%`, backgroundColor: color }} />
            <span style={{ width: `${marginShare * 100}%`, backgroundColor: color, opacity: 0.45 }} />
          </div>
          {/* The tick at SCALE_MAX is the bar's own right edge; a line there would
              sit under the rounded corner and read as a rendering artefact. */}
          {SCALE_TICKS.filter((tick) => tick.score < SCALE_MAX).map((tick) => (
            <span
              key={tick.score}
              aria-hidden="true"
              className="absolute top-0 h-full w-px bg-ink-950/70"
              style={{ left: `${(tick.score / SCALE_MAX) * 100}%` }}
            />
          ))}
        </div>
        <div className="relative mt-1 h-4" aria-hidden="true">
          {SCALE_TICKS.map((tick) => (
            <span
              key={tick.score}
              // Centred on its tick, except at the ends, where centring would put
              // half the label outside the container and clip it.
              className={`tnum absolute text-[0.625rem] text-paper-faint ${
                tick.score === SCALE_MAX ? "-translate-x-full" : "-translate-x-1/2"
              }`}
              style={{ left: `${(tick.score / SCALE_MAX) * 100}%` }}
            >
              {tick.label}
            </span>
          ))}
        </div>
        {offScale && (
          <p className="mt-1 text-xs text-paper-muted">
            The score runs past the end of this scale — rarer than {SCALE_TICKS.at(-1)?.label}.
          </p>
        )}
      </div>

      <dl className="mt-4 space-y-3">
        <Term
          term="Surprisal"
          value={formatNumber(calculation.surprisal, 3)}
          swatch={color}
          note={
            <>
              <code>−log₁₀(p_tail)</code> — how improbable a day at least this extreme is in this
              city&apos;s seasonal window. This is the ranking, and it is comparable across metrics
              because it is a probability rather than a magnitude.
            </>
          }
        />
        <Term
          term="Margin bonus"
          value={formatNumber(calculation.margin_bonus, 3)}
          swatch={color}
          swatchOpacity={0.45}
          note={
            <>
              <code>0.5 · log₁₀(1 + margin / IQR)</code> — a bounded credit for readings beyond every
              value in the reference sample, where the tail probability alone is capped by sample
              size.{" "}
              {calculation.beyond_baseline_sample
                ? "This reading falls outside the reference sample."
                : "This reading falls inside the reference sample, so the term is zero."}
            </>
          }
        />
      </dl>

      <div className="mt-6 grid gap-x-8 gap-y-5 border-t border-ink-800 pt-5 sm:grid-cols-2">
        <Figure
          label="Empirical percentile"
          value={formatPercentile(calculation.percentile)}
          note={`Rank of this reading among ${formatNumber(baseline.n, 0)} comparable days.`}
        />
        <Figure
          label="Tail probability"
          value={formatProbability(calculation.tail_probability)}
          note={describeRarity(calculation)}
          flag={
            calculation.tail_probability_is_bounded ? (
              <Badge className="bg-warning/10 text-warning ring-warning/30">Lower bound</Badge>
            ) : undefined
          }
        />
        <Figure
          label="Return period"
          value={formatReturnPeriod(calculation.return_period_years)}
          note="Roughly how often a day this extreme occurs in this city and season, estimated from the 30-year reference sample. Not a design standard."
        />
        <Figure
          label="Standardised anomaly"
          value={zScore ?? "Withheld"}
          note={
            zScore
              ? "Reported because this metric's seasonal distribution is close enough to symmetric for a z-score to summarise it. Even so, the ranking uses the empirical tail probability, not this."
              : "Not reported: this metric's seasonal distribution is skewed or zero-heavy, so a z-score would not be comparable with other metrics and could not be read as a probability."
          }
        />
        <Figure
          label="Departure from reference"
          value={formatMeasurement(calculation.deviation, unit)}
          note="Absolute difference from the seasonal reference value, in the metric's own units. Shown for interpretability; never used for ranking."
        />
        <Figure
          label="Robust deviation"
          value={
            calculation.robust_deviation === null
              ? "—"
              : `${formatNumber(calculation.robust_deviation, 2)} × IQR`
          }
          note="Distance from the seasonal median in interquartile ranges. Used only as a tie-break, because it degrades gracefully on skewed distributions."
        />
      </div>
    </div>
  );
}

function Term({
  term,
  value,
  note,
  swatch,
  swatchOpacity = 1,
}: {
  term: string;
  value: string;
  note: React.ReactNode;
  swatch: string;
  swatchOpacity?: number;
}) {
  return (
    <div className="flex gap-3">
      <span
        aria-hidden="true"
        className="mt-1.5 inline-block size-2.5 shrink-0 rounded-sm"
        style={{ backgroundColor: swatch, opacity: swatchOpacity }}
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-4">
          <dt className="text-sm font-medium text-paper">{term}</dt>
          <dd className="tnum text-sm text-paper-dim">{value}</dd>
        </div>
        <p className="mt-1 text-xs leading-relaxed text-paper-faint">{note}</p>
      </div>
    </div>
  );
}

function Figure({
  label,
  value,
  note,
  flag,
}: {
  label: string;
  value: string;
  note: string;
  flag?: React.ReactNode;
}) {
  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
          {label}
        </p>
        {flag}
      </div>
      <p className="tnum mt-1.5 text-lg text-paper">{value}</p>
      <p className="mt-1.5 text-xs leading-relaxed text-paper-faint">{note}</p>
    </div>
  );
}
