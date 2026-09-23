"""Command-line entry point for ``wo``.

    python -m wo summary
    python -m wo traces [--limit N]
    python -m wo trace <trace_id>

The exit code follows the same contract as the pipeline CLI: ``0`` means the
command completed and printed its report (including the case where the report
says "nothing recorded"); ``1`` means it could not do its primary job — the
tracking store was unreachable, or a specific trace did not exist.
"""

from __future__ import annotations

import argparse
import sys

from wo import __version__
from wo.commands import run_summary, run_trace, run_traces
from wo.config import Config, from_env, with_overrides
from wo.stores.app_db import AppDbStore
from wo.stores.mlflow_api import MlflowApiStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m wo",
        description="Read-only observability for Weather Outliers (MLflow + PostgreSQL).",
    )
    parser.add_argument("--version", action="version", version=f"wo {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    summary = sub.add_parser("summary", help="Pipeline, baseline and telemetry status.")
    _add_common(summary)
    summary.add_argument("--runs", type=int, default=5, help="Recent runs to show (default: 5).")

    traces = sub.add_parser("traces", help="List traces in the experiment.")
    _add_common(traces)
    traces.add_argument("--limit", type=int, default=20, help="Maximum traces (default: 20).")

    trace = sub.add_parser("trace", help="Render one trace's span tree.")
    _add_common(trace)
    trace.add_argument("trace_id", help="The MLflow trace id (e.g. tr-…).")

    return parser


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tracking-uri", default=None, help="Override MLFLOW_TRACKING_URI.")
    parser.add_argument("--experiment", default=None, help="Override MLFLOW_EXPERIMENT.")
    parser.add_argument("--database-url", default=None, help="Override DATABASE_URL.")


def _config(args: argparse.Namespace) -> Config:
    return with_overrides(
        from_env(),
        database_url=args.database_url,
        mlflow_tracking_uri=args.tracking_uri,
        mlflow_experiment=args.experiment,
    )


def _stores(config: Config) -> tuple[AppDbStore, MlflowApiStore]:
    app_store = AppDbStore(database_url=config.database_url)
    trace_store = MlflowApiStore(
        config.mlflow_tracking_uri,
        username=config.mlflow_username,
        password=config.mlflow_password,
    )
    return app_store, trace_store


_COMMANDS = {
    "summary": run_summary,
    "traces": run_traces,
    "trace": run_trace,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _config(args)
    app_store, trace_store = _stores(config)

    handler = _COMMANDS[args.command]
    try:
        return handler(args, config, app_store, trace_store)
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
