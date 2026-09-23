"""Run the evaluation, persist it, and email the summary.

    backend/.venv/bin/python -m evals.deliver

This is the scheduled entry point for the evaluation worker. The evaluation
itself stays hermetic — ``evals.environment`` pins the world to an in-memory
SQLite database and the fixture provider, exactly as ``evals.runner`` does — and
this entrypoint then writes the finished report to the *real* application
database and emails it.

Both delivery steps are best-effort by design: a database or SMTP failure is
reported and the process keeps going, because the evaluation's own verdict is
the only thing that drives the exit code.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from evals import environment, runner
from evals.harness import STATUS_PASSED

DEFAULT_OUTPUT = environment.REPO_ROOT / "evals" / "reports" / "latest.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.deliver",
        description="Run the evaluation suites, persist the report, and email a summary.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--judge",
        action="store_true",
        help="Run the paid model judge. Costs money.",
    )
    parser.add_argument("--skip-unit-tests", action="store_true")
    parser.add_argument("--only", action="append", metavar="SUITE")
    return parser.parse_args(argv)


def _safe_url(url: str) -> str:
    """A database URL fit to print: scheme and host, never credentials."""
    parsed = urlsplit(url)
    if parsed.scheme.startswith("sqlite") or parsed.scheme in ("file", ""):
        return url
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}/…"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Captured before ``environment.configure``, which repoints ``DATABASE_URL``
    # at the hermetic scratch database. The real URL is what persistence needs.
    target_url = os.environ.get("DATABASE_URL")

    environment.configure(keep_keys=args.judge)
    report = runner.execute(
        judge=args.judge,
        skip_unit_tests=args.skip_unit_tests,
        only=args.only,
        verdict_path=args.output.with_name("judge-verdicts.json"),
    )
    if report is None:
        return 2

    runner.write_report(report, args.output)
    runner.print_summary(report)
    print(f"  report written to {args.output}")

    if target_url:
        try:
            from evals.persist import persist_report

            report_id = persist_report(report, target_url)
            print(f"  persisted to {_safe_url(target_url)} as report {report_id}")
        except Exception as exc:  # pragma: no cover - persistence is best-effort
            print(f"  NOT persisted: {exc}", file=sys.stderr)
    else:
        print("  not persisted (DATABASE_URL not set)")

    from app.config import get_settings
    from evals.notify import notify

    settings = get_settings()
    if notify(report, settings):
        print(f"  emailed to {settings.eval_email_to}")
    else:
        print("  not emailed (SMTP not configured or delivery failed)")

    return 0 if report.status == STATUS_PASSED else 1


if __name__ == "__main__":
    raise SystemExit(main())
