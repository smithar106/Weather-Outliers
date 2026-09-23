/**
 * Presentation helpers.
 *
 * Several of the project's honesty rules are really formatting rules, so they
 * live here rather than being restated in every component:
 *
 * * A missing number renders as an em dash, never as `0`.
 * * A tail probability that is capped by sample size is prefixed "at least as
 *   rare as", never stated as exact.
 * * A z-score is only ever rendered through {@link formatZScore}, which refuses
 *   to print one the backend flagged as not meaningful.
 * * Nothing here produces the word "record".
 */

import type {
  CategoryId,
  DataQuality,
  DataTier,
  Direction,
  EventCalculation,
  MetricId,
  ObservationType,
} from "@/lib/types";

/** What an absent measurement looks like. Never `0`, never blank. */
export const NO_VALUE = "—";

// ---------------------------------------------------------------------------
// Numbers
// ---------------------------------------------------------------------------

export function formatNumber(
  value: number | null | undefined,
  decimals = 1,
  options: { signed?: boolean } = {}
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return NO_VALUE;
  const text = value.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  if (options.signed && value > 0) return `+${text}`;
  return text;
}

/** A measurement with its unit, e.g. `45.6 °C`. Degrees get no space break. */
export function formatMeasurement(
  value: number | null | undefined,
  unit: string,
  decimals = 1
): string {
  const text = formatNumber(value, decimals);
  if (text === NO_VALUE) return NO_VALUE;
  return `${text} ${unit}`;
}

/** A signed departure from baseline, e.g. `+4.4 °C`. */
export function formatDeviation(
  value: number | null | undefined,
  unit: string,
  decimals = 1
): string {
  const text = formatNumber(value, decimals, { signed: true });
  if (text === NO_VALUE) return NO_VALUE;
  return `${text} ${unit}`;
}

/**
 * A percentile in ordinal form, e.g. `99.8th`.
 *
 * Deliberately not rounded to a whole number: the difference between the 99.8th
 * and the 99.9th percentile is a factor of two in rarity.
 */
export function formatPercentile(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return NO_VALUE;
  return `${formatNumber(value, 1)}th`;
}

/**
 * A tail probability as a percentage, with enough precision to stay meaningful
 * in the far tail.
 */
export function formatProbability(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return NO_VALUE;
  if (value === 0) return "<0.001%";
  const pct = value * 100;
  const decimals = pct >= 1 ? 1 : pct >= 0.1 ? 2 : 3;
  return `${formatNumber(pct, decimals)}%`;
}

/**
 * `about 1 in 455` — the reciprocal of the tail probability, in plain words.
 *
 * Deliberately unitless. Every caller already names the population it is counting
 * ("of comparable days"), and repeating the noun here produced the tautology
 * "1.6% of comparable days — about 1 in 62 comparable days".
 */
export function formatOddsPhrase(tailProbability: number | null | undefined): string | null {
  if (
    tailProbability === null ||
    tailProbability === undefined ||
    !Number.isFinite(tailProbability) ||
    tailProbability <= 0
  ) {
    return null;
  }
  const oneIn = Math.round(1 / tailProbability);
  return `about 1 in ${oneIn.toLocaleString("en-US")}`;
}

/**
 * A return period in years.
 *
 * Phrased as "roughly once every N years for this city and season", because the
 * figure is an empirical estimate from a 30-year sample, not a design standard.
 */
export function formatReturnPeriod(years: number | null | undefined): string {
  if (years === null || years === undefined || !Number.isFinite(years)) return NO_VALUE;
  if (years >= 100) return `${formatNumber(years, 0)} years`;
  if (years >= 10) return `${formatNumber(years, 0)} years`;
  return `${formatNumber(years, 1)} years`;
}

/**
 * The rarity statement for an event, honouring the bounded flag.
 *
 * When `tail_probability_is_bounded` is true the probability is capped by the
 * sample size (1/(n+1)) rather than measured, so the only defensible phrasing is
 * a lower bound.
 */
export function describeRarity(calculation: EventCalculation): string {
  const probability = formatProbability(calculation.tail_probability);
  if (probability === NO_VALUE) return "Rarity could not be estimated from the reference sample.";
  if (calculation.tail_probability_is_bounded) {
    return `At least as rare as ${probability} of comparable days — the reference sample is too short to measure further into the tail.`;
  }
  const odds = formatOddsPhrase(calculation.tail_probability);
  return odds ? `${probability} of comparable days — ${odds}.` : `${probability} of comparable days.`;
}

/**
 * How much of the reference sample this event was more extreme than, as a
 * percentage: "more unusual than 99.3% of comparable days".
 *
 * Derived from the percentile rather than from `tail_probability`, and the choice
 * matters at the very end of the tail. The two agree everywhere they can —
 * `percentile` is the empirical CDF `F(x)` in percent, and the backend's
 * one-sided tail is `1 − F` above and `F` below, so `100 · (1 − tail)` is the
 * same number — except that the tail probability is *floored* at `1/(n+1)` so it
 * can never claim more precision than the sample supports. Reading rarity off
 * that floor would top out at `n/(n+1)`, which is 99.8% for a 450-day window, and
 * would report a value that beat every single sampled day as though one day had
 * beaten it. The percentile has no floor, so it says 100% — which is the exact,
 * checkable claim: every one of the N sampled days was less extreme.
 *
 * That is also why the scope has to travel with the number. 100% here means 100%
 * of the days in this city's 1991–2020 seasonal window, not 100% of all days ever
 * recorded. Callers must print the sample size; {@link RarityTable} does it in the
 * column footnote and in each row's tooltip.
 */
export function rarityPercent(
  calculation: EventCalculation,
  direction: Direction
): number | null {
  const percentile = calculation.percentile;
  if (percentile === null || percentile === undefined || !Number.isFinite(percentile)) {
    return null;
  }
  const rarity = direction === "below" ? 100 - percentile : percentile;
  return Math.min(100, Math.max(0, rarity));
}

/**
 * {@link rarityPercent} as a string, with one guard: 100% is reserved for events
 * that actually cleared the whole sample.
 *
 * The empirical CDF returns `F = 1.0` for a value that merely *ties* the sample
 * maximum, because `F` counts days at or below `x`. "More unusual than 100% of
 * comparable days" would then be printed for a day that one historical day
 * matched. So unless `beyond_baseline_sample` says the value strictly cleared
 * every sampled day, the display is capped at the most a tie can truthfully
 * claim: `n − 1` of `n` days were strictly less extreme, i.e. `100 · (1 − 1/n)`.
 * For a 450-day window that is 99.8%, and the cap is derived from the sample
 * rather than picked to look plausible. It is a display cap only; the figure
 * behind it is untouched.
 *
 * One decimal place, matching {@link formatPercentile}. A 450-day window resolves
 * steps of 100/450 ≈ 0.22 points, so a second decimal would be invented
 * precision.
 */
export function formatRarityPercent(
  calculation: EventCalculation,
  direction: Direction,
  sampleSize: number
): string {
  const rarity = rarityPercent(calculation, direction);
  if (rarity === null) return NO_VALUE;
  if (calculation.beyond_baseline_sample) return "100%";
  const cap = sampleSize > 1 ? 100 * (1 - 1 / sampleSize) : 0;
  return `${formatNumber(Math.min(rarity, cap), 1)}%`;
}

/**
 * A z-score, or an explanation of why there isn't one.
 *
 * Returns `null` when the backend flagged the distribution as unsuitable. A
 * caller that renders `null` as a number would be presenting a z-score as
 * universally comparable, which it is not.
 */
export function formatZScore(calculation: EventCalculation): string | null {
  if (!calculation.z_valid || calculation.z_score === null) return null;
  return `${formatNumber(calculation.z_score, 2, { signed: true })} σ`;
}

// ---------------------------------------------------------------------------
// Dates
// ---------------------------------------------------------------------------

/**
 * A local calendar date, formatted without ever constructing a local `Date`.
 *
 * `new Date("2025-07-15")` is parsed as UTC midnight and then rendered in the
 * viewer's zone, which silently shifts the date backwards for anyone west of
 * Greenwich. The analysed date is a city-local calendar date with no time
 * component, so it is formatted from its parts instead.
 */
export function formatLocalDate(
  isoDate: string,
  style: "long" | "medium" | "short" = "long"
): string {
  const [year, month, day] = isoDate.split("-").map(Number);
  if (!year || !month || !day) return isoDate;
  const asUtc = new Date(Date.UTC(year, month - 1, day));
  const options: Intl.DateTimeFormatOptions =
    style === "long"
      ? { weekday: "long", year: "numeric", month: "long", day: "numeric", timeZone: "UTC" }
      : style === "medium"
        ? { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" }
        : { month: "short", day: "numeric", timeZone: "UTC" };
  return asUtc.toLocaleDateString("en-US", options);
}

/** A publication instant, in UTC, labelled as UTC. */
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return NO_VALUE;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return NO_VALUE;
  return `${at.toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "UTC",
  })} UTC`;
}

export function formatHoursAgo(hours: number | null | undefined): string {
  if (hours === null || hours === undefined || !Number.isFinite(hours)) return NO_VALUE;
  if (hours < 1) return "less than an hour ago";
  if (hours < 48) return `${Math.round(hours)} hours ago`;
  return `${Math.round(hours / 24)} days ago`;
}

export function formatDurationMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return NO_VALUE;
  if (ms < 1000) return `${formatNumber(ms, 0)} ms`;
  if (ms < 60_000) return `${formatNumber(ms / 1000, 1)} s`;
  return `${formatNumber(ms / 60_000, 1)} min`;
}

// ---------------------------------------------------------------------------
// Categories and provenance labels
// ---------------------------------------------------------------------------

export interface CategoryStyle {
  label: string;
  /** Hex, because MapLibre needs a colour string it can pass to WebGL. */
  color: string;
  /** Tailwind classes for badges and accents. */
  badge: string;
  text: string;
}

export const CATEGORY_STYLES: Record<CategoryId, CategoryStyle> = {
  heat: {
    label: "Heat",
    color: "#ea580c",
    badge: "bg-[#ea580c]/10 text-[#c2410c] ring-[#ea580c]/30",
    text: "text-[#c2410c]",
  },
  cold: {
    label: "Cold",
    color: "#0284c7",
    badge: "bg-[#0284c7]/10 text-[#0369a1] ring-[#0284c7]/30",
    text: "text-[#0369a1]",
  },
  temperature: {
    label: "Temperature",
    color: "#7c3aed",
    badge: "bg-[#7c3aed]/10 text-[#6d28d9] ring-[#7c3aed]/30",
    text: "text-[#6d28d9]",
  },
  precipitation: {
    label: "Precipitation",
    color: "#0d9488",
    badge: "bg-[#0d9488]/10 text-[#0f766e] ring-[#0d9488]/30",
    text: "text-[#0f766e]",
  },
  wind: {
    label: "Wind",
    color: "#ca8a04",
    badge: "bg-[#ca8a04]/10 text-[#a16207] ring-[#ca8a04]/30",
    text: "text-[#a16207]",
  },
};

export function categoryStyle(category: CategoryId | string): CategoryStyle {
  return CATEGORY_STYLES[category as CategoryId] ?? CATEGORY_STYLES.temperature;
}

export const METRIC_SHORT_LABELS: Record<MetricId, string> = {
  temp_max: "Daily high",
  temp_min: "Daily low",
  temp_mean: "Daily mean",
  precipitation: "Precipitation",
  wind_gust: "Peak gust",
};

export function metricShortLabel(metric: MetricId | string): string {
  return METRIC_SHORT_LABELS[metric as MetricId] ?? String(metric).replace(/_/g, " ");
}

export function directionWord(direction: string, category: CategoryId | string): string {
  if (category === "precipitation" || category === "wind") return "above normal";
  return direction === "above" ? "above normal" : "below normal";
}

/**
 * Plain-English provenance. This is the label that keeps the project's central
 * promise: a model estimate is never described as a station observation.
 */
export const OBSERVATION_TYPE_LABELS: Record<ObservationType, string> = {
  reanalysis: "Reanalysis estimate",
  model_analysis: "Model analysis estimate",
  station_observation: "Station observation",
};

export function observationTypeLabel(type: ObservationType | string): string {
  return OBSERVATION_TYPE_LABELS[type as ObservationType] ?? String(type).replace(/_/g, " ");
}

export const OBSERVATION_TYPE_NOTES: Record<ObservationType, string> = {
  reanalysis:
    "A gridded model reconstruction of past weather, constrained by observations. It is not a reading from a weather station in the city.",
  model_analysis:
    "A near-real-time gridded model estimate. It is not a reading from a weather station in the city, and it may be revised.",
  station_observation: "A direct measurement reported by a weather station.",
};

export function observationTypeNote(type: ObservationType | string): string {
  return (
    OBSERVATION_TYPE_NOTES[type as ObservationType] ??
    "Provenance for this value is not described by the source dataset."
  );
}

export const DATA_TIER_LABELS: Record<DataTier, string> = {
  final: "Final",
  provisional: "Provisional",
};

export const DATA_TIER_NOTES: Record<DataTier, string> = {
  final: "Drawn from the settled reanalysis archive.",
  provisional:
    "Drawn from near-real-time model analysis, which can be revised when the archive catches up.",
};

export function dataTierLabel(tier: DataTier | string): string {
  return DATA_TIER_LABELS[tier as DataTier] ?? String(tier);
}

export function dataTierNote(tier: DataTier | string): string {
  return DATA_TIER_NOTES[tier as DataTier] ?? "";
}

export const DATA_QUALITY_LABELS: Record<DataQuality, string> = {
  ok: "Complete",
  incomplete: "Incomplete",
  missing: "Missing",
};

export function dataQualityLabel(quality: DataQuality | string): string {
  return DATA_QUALITY_LABELS[quality as DataQuality] ?? String(quality);
}

const COUNTRY_NAMES: Record<string, string> = {
  US: "United States",
  CA: "Canada",
  MX: "Mexico",
};

export function countryName(code: string): string {
  return COUNTRY_NAMES[code] ?? code;
}

/** `Phoenix, Arizona` / `Calgary, Alberta` — the label used in headings. */
export function cityLabel(city: { name: string; admin: string }): string {
  return city.admin ? `${city.name}, ${city.admin}` : city.name;
}

export function formatUsd(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return NO_VALUE;
  if (value === 0) return "$0.00";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(2)}`;
}

export function formatPercent(fraction: number | null | undefined, decimals = 1): string {
  if (fraction === null || fraction === undefined || !Number.isFinite(fraction)) return NO_VALUE;
  return `${formatNumber(fraction * 100, decimals)}%`;
}
