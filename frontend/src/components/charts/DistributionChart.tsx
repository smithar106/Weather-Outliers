/**
 * The seasonal reference distribution, with the observed day marked on it.
 *
 * Hand-rolled SVG rather than a charting library, for the same reason the backend
 * has no numpy: the shape being drawn is exactly the histogram the backend stored,
 * and a reader can check the rendering against `baseline.histogram` in the API
 * response. It is also a server component with no client JavaScript at all.
 *
 * The y axis is a raw count, not a density, and is labelled as such — the bins are
 * equal width, so counts and density are proportional here, but saying "density"
 * would imply an estimate the backend never made.
 */

import { formatMeasurement, formatNumber } from "@/lib/format";
import type { BaselineHistogram } from "@/lib/types";

const WIDTH = 640;
const HEIGHT = 200;
const PAD = { top: 14, right: 12, bottom: 30, left: 40 };

export function DistributionChart({
  histogram,
  unit,
  observedValue,
  color,
  sampleSize,
  label,
}: {
  histogram: BaselineHistogram;
  unit: string;
  observedValue?: number | null;
  color: string;
  sampleSize: number;
  label: string;
}) {
  if (histogram.degenerate || histogram.counts.length === 0) {
    return (
      <p className="rounded-xl border border-ink-700 bg-ink-900/60 px-4 py-6 text-center text-sm text-paper-muted">
        The seasonal reference sample for this metric has no spread to plot.
      </p>
    );
  }

  const edges = histogram.bin_edges;
  const counts = histogram.counts;
  const domainMin = edges[0];
  const domainMax = edges[edges.length - 1];
  const maxCount = Math.max(...counts, 1);

  const plotWidth = WIDTH - PAD.left - PAD.right;
  const plotHeight = HEIGHT - PAD.top - PAD.bottom;

  // The observed value routinely sits outside the reference range — that is what
  // makes it an outlier — so the x domain is widened to include it rather than
  // clipping the marker to the edge of the plot, which would understate the event.
  const hasObserved = observedValue !== null && observedValue !== undefined;
  const padding = (domainMax - domainMin) * 0.04 || 1;
  const xMin = hasObserved ? Math.min(domainMin, observedValue - padding) : domainMin;
  const xMax = hasObserved ? Math.max(domainMax, observedValue + padding) : domainMax;
  const span = xMax - xMin || 1;

  const x = (value: number) => PAD.left + ((value - xMin) / span) * plotWidth;
  const y = (count: number) => PAD.top + plotHeight - (count / maxCount) * plotHeight;

  const ticks = [xMin, xMin + span / 2, xMax];

  /*
   * Axis labels are HTML over the plot rather than `<text>` inside it, for the
   * reason documented in TrendChart: SVG user units scale with the container, so
   * a 10px label renders around 4px once the card is phone-width. See that file.
   */
  const leftPct = (value: number) => `${(value / WIDTH) * 100}%`;
  const topPct = (value: number) => `${(value / HEIGHT) * 100}%`;

  return (
    <figure>
      <div className="relative">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="h-auto w-full"
        role="img"
        aria-label={`Histogram of ${label} across ${formatNumber(sampleSize, 0)} comparable days in the reference period${
          hasObserved ? `, with the observed value of ${formatMeasurement(observedValue, unit)} marked` : ""
        }.`}
      >
        {/* Horizontal guides at a quarter, half and three quarters of the peak. */}
        {[0.25, 0.5, 0.75, 1].map((fraction) => (
          <line
            key={fraction}
            x1={PAD.left}
            x2={WIDTH - PAD.right}
            y1={y(maxCount * fraction)}
            y2={y(maxCount * fraction)}
            stroke="var(--color-ink-700)"
            strokeWidth="1"
          />
        ))}

        {counts.map((count, index) => {
          const left = x(edges[index]);
          const right = x(edges[index + 1]);
          const barWidth = Math.max(right - left - 1, 1);
          const top = y(count);
          return (
            <rect
              key={index}
              x={left + 0.5}
              y={top}
              width={barWidth}
              height={Math.max(PAD.top + plotHeight - top, 0)}
              rx="1.5"
              fill="var(--color-ink-600)"
            />
          );
        })}

        {/* Baseline axis */}
        <line
          x1={PAD.left}
          x2={WIDTH - PAD.right}
          y1={PAD.top + plotHeight}
          y2={PAD.top + plotHeight}
          stroke="var(--color-ink-600)"
          strokeWidth="1"
        />

        {hasObserved && (
          <g>
            <line
              x1={x(observedValue)}
              x2={x(observedValue)}
              y1={PAD.top - 6}
              y2={PAD.top + plotHeight}
              stroke={color}
              strokeWidth="2"
            />
            <circle cx={x(observedValue)} cy={PAD.top - 6} r="3.5" fill={color} />
          </g>
        )}

      </svg>

      {/* Y axis: 0 and the peak count only. More would be clutter. Anchored by
          `right` for the reason given in TrendChart — the gutter is ~14px wide on
          a phone, too narrow to hold a three-digit count. */}
      {[
        { value: 0, at: PAD.top + plotHeight },
        { value: maxCount, at: PAD.top + 4 },
      ].map(({ value, at }) => (
        <span
          key={value}
          aria-hidden="true"
          className="tnum absolute -translate-y-1/2 whitespace-nowrap text-right text-[11px] leading-none text-paper-faint"
          style={{ top: topPct(at), right: leftPct(WIDTH - (PAD.left - 8)) }}
        >
          {value}
        </span>
      ))}

      {ticks.map((value, index) => {
        const isFirst = index === 0;
        const isLast = index === ticks.length - 1;
        return (
          <span
            key={index}
            aria-hidden="true"
            className={`tnum absolute text-[11px] leading-none text-paper-faint ${
              isFirst ? "" : isLast ? "" : "-translate-x-1/2"
            }`}
            style={
              isLast
                ? { top: topPct(HEIGHT - 16), right: leftPct(PAD.right) }
                : { top: topPct(HEIGHT - 16), left: leftPct(x(value)) }
            }
          >
            {formatNumber(value, Math.abs(span) < 5 ? 1 : 0)}
          </span>
        );
      })}
      </div>

      <figcaption className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-paper-faint">
        <span>
          Count of days per bin across {formatNumber(sampleSize, 0)} comparable days · x axis in{" "}
          {unit}
        </span>
        {hasObserved && (
          <span className="flex items-center gap-1.5">
            <span
              aria-hidden="true"
              className="inline-block h-2.5 w-0.5"
              style={{ backgroundColor: color }}
            />
            observed {formatMeasurement(observedValue, unit)}
          </span>
        )}
      </figcaption>
    </figure>
  );
}
