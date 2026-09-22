import Link from "next/link";

import { Card, DefRow } from "@/components/ui";
import {
  formatLocalDate,
  formatNumber,
  formatPercent,
  formatTimestamp,
  formatUsd,
  NO_VALUE,
} from "@/lib/format";
import type { DataSource, RunProvenance } from "@/lib/types";

/**
 * Where the board came from and what it cost.
 *
 * Shown in full rather than tucked behind a link because the site's claim is
 * reproducibility: analysis date, publication time, the registry and methodology
 * versions in force, how complete the input was, and how many model calls the run
 * made. All of it is read from the stored `pipeline_runs` row, so it describes the
 * run that produced the numbers above it, not the current configuration.
 */
export function ProvenanceStrip({
  run,
  dataSources,
}: {
  run: RunProvenance;
  dataSources: DataSource[];
}) {
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Card className="p-5 sm:p-6">
        <p className="eyebrow">This analysis run</p>
        <dl className="mt-4">
          <DefRow term="Analysis date">{formatLocalDate(run.analysis_date, "medium")}</DefRow>
          <DefRow term="Published" mono>
            {formatTimestamp(run.published_at)}
          </DefRow>
          <DefRow term="Cities analysed" mono>
            {formatNumber(run.cities_with_data, 0)} of {formatNumber(run.cities_total, 0)}
            {run.completeness !== null && (
              <span className="text-paper-faint"> ({formatPercent(run.completeness)})</span>
            )}
          </DefRow>
          <DefRow term="Events scored" mono>
            {formatNumber(run.events_total, 0)} computed, {formatNumber(run.events_published, 0)}{" "}
            published
          </DefRow>
          <DefRow term="Methodology version" mono>
            {run.methodology_version}
          </DefRow>
          <DefRow term="City registry version" mono>
            {run.registry_version ?? NO_VALUE}
          </DefRow>
          <DefRow term="AI calls in this run" mono>
            {formatNumber(run.llm_calls, 0)}
            {run.llm_calls > 0 && (
              <span className="text-paper-faint"> · est. {formatUsd(run.llm_estimated_usd)}</span>
            )}
          </DefRow>
          <DefRow term="Run id" mono>
            <span className="break-all">{run.run_id}</span>
          </DefRow>
        </dl>
        {run.llm_budget_exhausted && (
          <p className="mt-4 rounded-lg border border-warning/35 bg-warning/[0.06] px-3 py-2 text-xs leading-relaxed text-paper-dim">
            The configured monthly AI budget was exhausted during this run. Remaining explanations
            were generated from the deterministic templates, which use the same stored calculations.
          </p>
        )}
      </Card>

      <Card className="p-5 sm:p-6">
        <p className="eyebrow">Data sources</p>
        <ul className="mt-4 space-y-4">
          {dataSources.map((source) => (
            <li key={`${source.name}-${source.dataset}`} className="border-b border-ink-800 pb-4 last:border-0 last:pb-0">
              <a
                href={source.url}
                target="_blank"
                rel="noreferrer noopener"
                className="link-underline text-sm font-medium text-paper"
              >
                {source.name}
              </a>
              <dl className="mt-2 space-y-1 text-xs text-paper-muted">
                <div className="flex gap-2">
                  <dt className="shrink-0 text-paper-faint">Dataset</dt>
                  <dd className="tnum">{source.dataset}</dd>
                </div>
                <div className="flex gap-2">
                  <dt className="shrink-0 text-paper-faint">Values are</dt>
                  <dd>{source.observation_type.replace(/_/g, " ")}</dd>
                </div>
                <div className="flex gap-2">
                  <dt className="shrink-0 text-paper-faint">Licence</dt>
                  <dd>{source.licence}</dd>
                </div>
              </dl>
              <p className="mt-2 text-xs leading-relaxed text-paper-faint">{source.attribution}</p>
              {source.note && (
                <p className="mt-1.5 text-xs leading-relaxed text-paper-faint">{source.note}</p>
              )}
            </li>
          ))}
        </ul>
        <Link href="/methodology" className="link-underline mt-5 inline-block text-sm text-accent-bright">
          How the ranking is calculated →
        </Link>
      </Card>
    </div>
  );
}
