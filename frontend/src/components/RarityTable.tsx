import Link from "next/link";

import { Card, Dot } from "@/components/ui";
import {
  categoryStyle,
  formatNumber,
  formatRarityPercent,
  metricShortLabel,
  rarityPercent,
} from "@/lib/format";
import type { RankedEvent } from "@/lib/types";

/**
 * The board as a scannable index: how unusual each event was, in one percentage.
 *
 * It answers a question the cards answer only indirectly. A card gives a
 * percentile, a tail probability and an anomaly score, which is the honest full
 * picture and also three numbers to hold in your head. "More unusual than 99.3%
 * of comparable days" is one number, in the direction a reader already thinks:
 * bigger means rarer, for a cold event as much as a hot one.
 *
 * Two things this is careful about.
 *
 * **The rows are not sorted by this column.** The board's order is the anomaly
 * score, which is surprisal plus a margin term, so a row can sit above another
 * with an equal or slightly lower rarity percentage. Sorting the table by rarity
 * would silently present a second, different ranking; instead the column stays in
 * board order and the sample-size footnote says what the number is measured
 * against. See docs/methodology.md for why the score is not the percentile.
 *
 * **The scope travels with the number.** Every percentage is a share of *that
 * city's* 1991–2020 seasonal window, not of all weather everywhere, and the two
 * are easy to conflate when a figure reads 100%. The count is in the footnote and
 * in each row's tooltip, and `formatRarityPercent` will not print 100% unless the
 * value strictly cleared every day in the sample.
 */
export function RarityTable({ events }: { events: RankedEvent[] }) {
  if (events.length === 0) return null;

  // Every city's baseline is the same ±7-day window over the same 30 years, so
  // the counts are equal in practice — but they are read from the data rather
  // than assumed, and the footnote degrades to a range if they ever differ.
  const sampleSizes = [...new Set(events.map((ranked) => ranked.event.baseline.n))].sort(
    (a, b) => a - b
  );
  const sampleNote =
    sampleSizes.length === 1
      ? `${formatNumber(sampleSizes[0], 0)} comparable days`
      : `${formatNumber(sampleSizes[0], 0)}–${formatNumber(
          sampleSizes[sampleSizes.length - 1],
          0
        )} comparable days`;

  return (
    <Card className="p-4 sm:p-5">
      <p className="eyebrow">Percent rarity</p>
      <p className="mt-2 text-xs leading-relaxed text-paper-muted">
        How much of each city&rsquo;s own seasonal history the day beat. Higher is rarer.
      </p>

      <table className="mt-4 w-full border-collapse text-sm">
        <caption className="sr-only">
          Each ranked event and the share of its city&rsquo;s 1991&ndash;2020 seasonal sample that
          was less extreme, in board order.
        </caption>
        <thead>
          <tr className="border-b border-ink-700 text-left">
            <th scope="col" className="pb-2 pr-2 text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint">
              City
            </th>
            <th
              scope="col"
              className="whitespace-nowrap pb-2 pl-2 text-right text-[0.6875rem] font-semibold uppercase tracking-[0.1em] text-paper-faint"
            >
              Rarer than
            </th>
          </tr>
        </thead>
        <tbody>
          {events.map((ranked) => (
            <RarityRow key={ranked.event.id} ranked={ranked} />
          ))}
        </tbody>
      </table>

      <p className="mt-4 border-t border-ink-800 pt-3 text-[0.6875rem] leading-relaxed text-paper-faint">
        Out of {sampleNote} in that city&rsquo;s own 1991&ndash;2020 window. Rows follow the board,
        which is ordered by anomaly score.
      </p>
    </Card>
  );
}

function RarityRow({ ranked }: { ranked: RankedEvent }) {
  const { rank, event } = ranked;
  const style = categoryStyle(event.category);
  const rarity = rarityPercent(event.calculation, event.direction);
  const formatted = formatRarityPercent(event.calculation, event.direction, event.baseline.n);

  const tooltip =
    rarity === null
      ? "Rarity could not be estimated from this city's reference sample."
      : `${metricShortLabel(event.metric)}: more unusual than ${formatted} of the ` +
        `${formatNumber(event.baseline.n, 0)} comparable days in ${event.city.name}'s ` +
        `1991–2020 seasonal window.`;

  return (
    <tr className="border-b border-ink-800/70 last:border-0">
      <th scope="row" className="py-2 pr-2 text-left font-normal">
        <Link
          href={`/city/${event.city.id}`}
          title={tooltip}
          className="flex items-center gap-2 text-paper-dim transition-colors hover:text-accent-bright"
        >
          <span className="tnum w-5 shrink-0 text-xs text-paper-faint">{rank}</span>
          <Dot color={style.color} />
          <span className="truncate">{event.city.name}</span>
        </Link>
      </th>
      <td className="tnum py-2 pl-2 text-right text-paper" title={tooltip}>
        {formatted}
      </td>
    </tr>
  );
}
