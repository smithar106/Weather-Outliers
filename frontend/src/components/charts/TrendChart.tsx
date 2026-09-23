/**
 * Daily weather trend for one city, built from stored observations.
 *
 * The honest bit is the handling of sparseness. A fresh deployment has exactly one
 * observation per city per completed pipeline run, because the baseline build
 * persists aggregated statistics rather than thirty years of daily rows. So the
 * chart states how many days it actually has, draws points rather than only a line
 * when there are few, and refuses to interpolate across a gap: a missing day breaks
 * the path instead of being bridged by a straight line that no measurement supports.
 */

import { formatLocalDate, formatNumber } from "@/lib/format";
import type { CityHistoryPoint, MetricId } from "@/lib/types";

const WIDTH = 720;
const HEIGHT = 240;
const PAD = { top: 16, right: 14, bottom: 34, left: 44 };

type SeriesKey = "temp_max_c" | "temp_min_c" | "precipitation_mm" | "wind_gust_max_kmh";

interface SeriesSpec {
  key: SeriesKey;
  label: string;
  color: string;
  unit: string;
}

const TEMPERATURE_SERIES: SeriesSpec[] = [
  { key: "temp_max_c", label: "Daily high", color: "#ff6b4a", unit: "°C" },
  { key: "temp_min_c", label: "Daily low", color: "#56b4f5", unit: "°C" },
];

const SERIES_FOR_METRIC: Record<MetricId, SeriesSpec[]> = {
  temp_max: TEMPERATURE_SERIES,
  temp_min: TEMPERATURE_SERIES,
  temp_mean: TEMPERATURE_SERIES,
  precipitation: [
    { key: "precipitation_mm", label: "Precipitation", color: "#22c7b8", unit: "mm" },
  ],
  wind_gust: [{ key: "wind_gust_max_kmh", label: "Peak gust", color: "#e8c84a", unit: "km/h" }],
};

export function TrendChart({
  points,
  metric = "temp_max",
}: {
  points: CityHistoryPoint[];
  metric?: MetricId;
}) {
  const series = SERIES_FOR_METRIC[metric] ?? TEMPERATURE_SERIES;
  const unit = series[0].unit;

  // Sorted defensively: the chart's x axis is time, and a reordered response would
  // otherwise draw a path that zigzags backwards.
  const sorted = [...points].sort((a, b) => a.local_date.localeCompare(b.local_date));

  const values = sorted.flatMap((point) =>
    series.map((spec) => point[spec.key]).filter((value): value is number => value !== null)
  );

  if (sorted.length === 0 || values.length === 0) {
    return (
      <p className="rounded-xl border border-ink-700 bg-ink-900/60 px-4 py-6 text-center text-sm text-paper-muted">
        No stored observations for this city and metric yet. Observations accumulate one day per
        completed pipeline run.
      </p>
    );
  }

  const dataMin = Math.min(...values);
  const dataMax = Math.max(...values);
  const headroom = (dataMax - dataMin) * 0.12 || 1;
  // Precipitation and gusts are non-negative, so the axis is anchored at zero
  // rather than floated — a floated zero makes a 0.4 mm day look substantial.
  const yMin = metric === "precipitation" || metric === "wind_gust" ? 0 : dataMin - headroom;
  const yMax = dataMax + headroom;
  const ySpan = yMax - yMin || 1;

  const plotWidth = WIDTH - PAD.left - PAD.right;
  const plotHeight = HEIGHT - PAD.top - PAD.bottom;

  const x = (index: number) =>
    sorted.length === 1
      ? PAD.left + plotWidth / 2
      : PAD.left + (index / (sorted.length - 1)) * plotWidth;
  const y = (value: number) => PAD.top + plotHeight - ((value - yMin) / ySpan) * plotHeight;

  /** Builds one path per unbroken stretch of measurements, never across a gap. */
  function segments(spec: SeriesSpec): string[] {
    const paths: string[] = [];
    let current: string[] = [];
    sorted.forEach((point, index) => {
      const value = point[spec.key];
      if (value === null) {
        if (current.length > 1) paths.push(current.join(" "));
        current = [];
        return;
      }
      current.push(`${current.length === 0 ? "M" : "L"} ${x(index).toFixed(2)} ${y(value).toFixed(2)}`);
    });
    if (current.length > 1) paths.push(current.join(" "));
    return paths;
  }

  const yTicks = [yMin, yMin + ySpan / 2, yMax];
  const showPoints = sorted.length <= 60;

  return (
    <figure>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="h-auto w-full"
        role="img"
        aria-label={`${series.map((s) => s.label).join(" and ")} for ${sorted.length} stored ${
          sorted.length === 1 ? "day" : "days"
        }, from ${sorted[0].local_date} to ${sorted[sorted.length - 1].local_date}.`}
      >
        {yTicks.map((tick, index) => (
          <g key={index}>
            <line
              x1={PAD.left}
              x2={WIDTH - PAD.right}
              y1={y(tick)}
              y2={y(tick)}
              stroke="var(--color-ink-700)"
              strokeWidth="1"
            />
            <text x={PAD.left - 8} y={y(tick) + 3} textAnchor="end" className="fill-paper-faint text-[10px]">
              {formatNumber(tick, ySpan < 5 ? 1 : 0)}
            </text>
          </g>
        ))}

        {series.map((spec) => (
          <g key={spec.key}>
            {segments(spec).map((path, index) => (
              <path
                key={index}
                d={path}
                fill="none"
                stroke={spec.color}
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            ))}
            {showPoints &&
              sorted.map((point, index) => {
                const value = point[spec.key];
                if (value === null) return null;
                return (
                  <circle
                    key={`${spec.key}-${index}`}
                    cx={x(index)}
                    cy={y(value)}
                    r={sorted.length === 1 ? 4 : 2.6}
                    fill={spec.color}
                    stroke="var(--color-ink-900)"
                    strokeWidth="1"
                  />
                );
              })}
          </g>
        ))}

        <text x={PAD.left} y={HEIGHT - 10} className="fill-paper-faint text-[10px]">
          {formatLocalDate(sorted[0].local_date, "short")}
        </text>
        {sorted.length > 1 && (
          <text
            x={WIDTH - PAD.right}
            y={HEIGHT - 10}
            textAnchor="end"
            className="fill-paper-faint text-[10px]"
          >
            {formatLocalDate(sorted[sorted.length - 1].local_date, "short")}
          </text>
        )}
      </svg>

      <figcaption className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-paper-faint">
        {series.map((spec) => (
          <span key={spec.key} className="flex items-center gap-1.5">
            <span
              aria-hidden="true"
              className="inline-block h-0.5 w-3 rounded-full"
              style={{ backgroundColor: spec.color }}
            />
            {spec.label}
          </span>
        ))}
        <span>
          {sorted.length} stored {sorted.length === 1 ? "day" : "days"} · y axis in {unit}
        </span>
      </figcaption>
    </figure>
  );
}
