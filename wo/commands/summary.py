"""``wo summary`` — one screen of pipeline and telemetry status.

This command is deliberately tolerant of a missing half: if the application
database is unreachable it reports that and still shows the MLflow side, and vice
versa. A source that is reachable but empty (no runs, no baselines, no traces) is
reported as empty, never as an error.
"""

from __future__ import annotations

import argparse

from wo.render import SummaryData, render_summary
from wo.stores.base import AppDbStore, StoreUnavailable, TraceStore


def run(args: argparse.Namespace, config, app_store: AppDbStore, trace_store: TraceStore) -> int:
    mlflow_status, trace_count = _telemetry(trace_store, config.mlflow_experiment)

    coverage, coverage_note = _coverage(app_store)
    runs = _recent_runs(app_store, args.runs)
    mix = _explanation_mix(app_store, runs)
    pricing = _pricing_configured(app_store)

    data = SummaryData(
        experiment=config.mlflow_experiment,
        mlflow_status=mlflow_status,
        trace_count=trace_count,
        coverage=coverage,
        coverage_note=coverage_note,
        runs=tuple(runs),
        generator_mix=mix,
        pricing_configured=pricing,
    )
    print(render_summary(data))
    return 0


def _telemetry(trace_store: TraceStore, experiment: str) -> tuple[str, int | None]:
    try:
        if not trace_store.experiment_exists(experiment):
            return f"experiment {experiment!r} does not exist yet", 0
        return "tracking store reachable", trace_store.count_traces(experiment)
    except StoreUnavailable as exc:
        return f"unavailable — {exc}", None


def _coverage(app_store: AppDbStore):
    try:
        return app_store.coverage(), None
    except StoreUnavailable as exc:
        return None, str(exc)


def _recent_runs(app_store: AppDbStore, limit: int):
    try:
        return app_store.recent_runs(limit)
    except StoreUnavailable:
        return []


def _explanation_mix(app_store: AppDbStore, runs) -> dict[str, int]:
    run_ids = [run.run_id for run in runs]
    if not run_ids:
        return {}
    try:
        return app_store.explanation_mix(run_ids)
    except StoreUnavailable:
        return {}


def _pricing_configured(app_store: AppDbStore) -> bool:
    try:
        return app_store.pricing_configured()
    except StoreUnavailable:
        return False
