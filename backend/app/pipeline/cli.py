"""Command line entry point for the pipeline.

    python -m app.pipeline run [--date YYYY-MM-DD]
    python -m app.pipeline backfill --start YYYY-MM-DD --end YYYY-MM-DD
    python -m app.pipeline finalize [--lookback-days N]
    python -m app.pipeline build-baselines [--city ID ...] [--force]
    python -m app.pipeline seed-cities
    python -m app.pipeline status

Two conventions here are deliberate.

**The exit code is the contract.** ``0`` means a board was published, ``1`` means
the run failed. A scheduler needs that distinction more than it needs the text,
because the text is for whoever reads the logs afterwards. ``run`` is also the
only subcommand a cron schedule should ever invoke without arguments.

**Every subcommand opens its own transaction and closes it.** A cron container
that leaves a connection open holds a slot on a small Postgres plan for as long
as the platform keeps the process alive, so the engine is disposed on the way
out, including after a failure.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime

from app.agent.investigator import prompt_identity
from app.config import get_settings
from app.db import dispose_engine, session_scope
from app.domain import METHODOLOGY_VERSION, DataTier, RunStatus
from app.logging_setup import configure_logging
from app.observability import tracing
from app.pipeline.runner import (
    Pipeline,
    PipelineError,
    RunReport,
    backfill,
    coverage,
    finalize,
    seed_cities,
)

logger = logging.getLogger("app.pipeline")

EXIT_OK = 0
EXIT_FAILED = 1


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:  # argparse renders this as a usage error
        raise argparse.ArgumentTypeError(
            f"expected an ISO calendar date, YYYY-MM-DD (got {value!r})"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.pipeline",
        description="Weather Outliers scheduled pipeline.",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Override LOG_LEVEL for this invocation (e.g. DEBUG).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser(
        "run",
        help="Analyse one date and publish it.",
        description=(
            "With no --date, analyses the most recent local calendar day that is "
            "complete in every city in the registry. This is the command the daily "
            "schedule runs."
        ),
    )
    run.add_argument("--date", type=_iso_date, default=None)
    run.add_argument(
        "--skip-explanations",
        action="store_true",
        help="Rank and publish without generating explanations. Useful for a "
        "cheap re-run when only the statistics changed.",
    )
    run.add_argument(
        "--tier",
        choices=["final", "provisional"],
        default=None,
        help="Force a data tier instead of deriving it from the date's age.",
    )

    back = sub.add_parser(
        "backfill",
        help="Re-run a closed date range, oldest first.",
        description=(
            "Each date is an independent run: one bad day does not abort the rest. "
            "Reruns are idempotent, so this is safe to repeat over a range that "
            "has already been analysed."
        ),
    )
    back.add_argument("--start", type=_iso_date, required=True)
    back.add_argument("--end", type=_iso_date, required=True)
    back.add_argument("--skip-explanations", action="store_true")

    fin = sub.add_parser(
        "finalize",
        help="Recompute recent dates whose provisional data has since settled.",
    )
    fin.add_argument(
        "--lookback-days",
        type=int,
        default=14,
        help="How far back to look for provisional runs (default: 14).",
    )

    base = sub.add_parser(
        "build-baselines",
        help="Populate the climatology cache. Slow; run once per city.",
    )
    base.add_argument(
        "--city",
        action="append",
        dest="cities",
        default=None,
        help="Limit to one city id. Repeatable.",
    )
    base.add_argument(
        "--force",
        action="store_true",
        help="Rebuild baselines that already exist.",
    )

    sub.add_parser("seed-cities", help="Load data/cities.json into the database.")
    sub.add_parser("status", help="Print baseline coverage and the last runs.")

    return parser


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _report_exit(reports: list[RunReport]) -> int:
    """Print each report and fail the process if any run did not publish."""
    for report in reports:
        print(report.summary())
    if not reports:
        print("nothing to do")
        return EXIT_OK
    failed = [r for r in reports if r.status != RunStatus.SUCCEEDED.value]
    return EXIT_FAILED if failed else EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    force_tier = DataTier(args.tier) if args.tier else None
    with session_scope() as session:
        pipeline = Pipeline(session)
        try:
            report = pipeline.run_daily(
                args.date,
                force_tier=force_tier,
                skip_explanations=args.skip_explanations,
            )
        finally:
            pipeline.close()
    return _report_exit([report])


def cmd_backfill(args: argparse.Namespace) -> int:
    with session_scope() as session:
        reports = backfill(
            session,
            args.start,
            args.end,
            skip_explanations=args.skip_explanations,
        )
    return _report_exit(reports)


def cmd_finalize(args: argparse.Namespace) -> int:
    with session_scope() as session:
        reports = finalize(session, lookback_days=args.lookback_days)
    if not reports:
        print("no provisional runs are ready to finalise")
        return EXIT_OK
    return _report_exit(reports)


def cmd_build_baselines(args: argparse.Namespace) -> int:
    with session_scope() as session:
        pipeline = Pipeline(session)
        try:
            report = pipeline.build_baselines(city_ids=args.cities, force=args.force)
        finally:
            pipeline.close()
        print(report.summary())
        stats = coverage(session)
    print(
        f"  coverage:     {stats.get('cities_with_baselines', 0)} cities, "
        f"{stats.get('rows', 0)} rows "
        f"({stats.get('sufficient_rows', 0)} with a sufficient sample)"
    )
    return EXIT_FAILED if report.status != RunStatus.SUCCEEDED.value else EXIT_OK


def cmd_seed_cities(_: argparse.Namespace) -> int:
    with session_scope() as session:
        summary = seed_cities(session)
    print(
        f"registry {summary['registry_version']}: "
        f"{summary['inserted']} inserted, {summary['updated']} updated, "
        f"{summary['unchanged']} unchanged, {summary['deactivated']} deactivated "
        f"({summary['total_in_registry']} in file)"
    )
    return EXIT_OK


def cmd_status(_: argparse.Namespace) -> int:
    """A one-screen answer to "is this deployment healthy?" without curl."""
    from sqlalchemy import select

    from app.models import PipelineRun

    settings = get_settings()
    with session_scope() as session:
        stats = coverage(session)
        runs = (
            session.execute(
                select(PipelineRun)
                .order_by(PipelineRun.started_at.desc())
                .limit(5)
            )
            .scalars()
            .all()
        )

    print(f"environment:  {settings.environment}")
    print(f"weather:      {settings.weather_provider}")
    print(f"llm:          {settings.llm_provider} (enabled={settings.llm_enabled})")
    print(
        f"baselines:    {stats.get('cities_with_baselines', 0)} cities, "
        f"{stats.get('rows', 0)} rows, period "
        f"{stats.get('reference_period', 'unknown')}"
    )
    if not runs:
        print("runs:         none yet — run `seed-cities`, `build-baselines`, then `run`")
        return EXIT_OK

    print("runs:")
    for run in runs:
        flag = "published" if run.published else "unpublished"
        print(
            f"  {run.started_at:%Y-%m-%d %H:%M} {run.kind:<10} "
            f"{run.analysis_date} {run.status:<9} {flag:<11} "
            f"events={run.events_published or 0} "
            f"completeness={(run.completeness or 0):.0%}"
        )
    return EXIT_OK


HANDLERS = {
    "run": cmd_run,
    "backfill": cmd_backfill,
    "finalize": cmd_finalize,
    "build-baselines": cmd_build_baselines,
    "seed-cities": cmd_seed_cities,
    "status": cmd_status,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    settings = get_settings()
    if args.log_level:
        settings = settings.model_copy(update={"log_level": args.log_level})
    configure_logging(settings)
    # Tracing is configured once per process, here, and nowhere inside the
    # pipeline itself. A run must behave identically whether or not this call
    # found MLflow installed, so its return value is deliberately ignored: it is
    # already logged, and there is no branch in the pipeline that depends on it.
    tracing.configure(
        settings,
        extra_defaults={
            "environment": settings.environment,
            "methodology_version": METHODOLOGY_VERSION,
            "command": args.command,
            **prompt_identity(),
        },
    )

    started = datetime.now()
    try:
        return HANDLERS[args.command](args)
    except PipelineError as exc:
        # An expected refusal — bad arguments, insufficient data. One line, no
        # traceback, because there is nothing in the stack worth reading.
        logger.error("%s", exc)
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted", file=sys.stderr)
        return EXIT_FAILED
    except Exception:
        logger.exception("unhandled failure in `%s`", args.command)
        return EXIT_FAILED
    finally:
        dispose_engine()
        logger.info(
            "%s finished in %.1fs",
            args.command,
            (datetime.now() - started).total_seconds(),
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
