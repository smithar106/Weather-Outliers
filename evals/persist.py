"""Persist a finished evaluation report into the application database.

The harness itself is hermetic — it runs against an in-memory SQLite world — so
this module is deliberately separate: it takes the finished :class:`Report` and
the *real* application database URL, and writes normalized rows that ``wo`` and
(later) the NL→SQL agent can query with plain SQL. It never creates tables: the
schema comes from Alembic, like every other table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from evals.harness import Report


def _ensure_backend_on_path() -> None:
    import sys

    backend = Path(__file__).resolve().parents[1] / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))


def normalise_url(url: str) -> str:
    """Accept the URL shape Railway injects and upgrade the driver, as ``app.config`` does."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def classify_value(value: Any) -> tuple[str, float | None, str | None]:
    """Split a metric value into ``(type, numeric, text)``.

    The harness's :class:`Metric` allows ``float | int | str | bool | None``. A
    ``None`` ("not measured") must never be persisted as a zero, so the type is
    recorded explicitly and only the appropriate column is populated.
    """
    if value is None:
        return "null", None, None
    if isinstance(value, bool):
        return "boolean", float(int(value)), "true" if value else "false"
    if isinstance(value, (int, float)):
        return "number", float(value), None
    return "text", None, str(value)


def _generated_at(report: Report) -> datetime:
    try:
        return datetime.fromisoformat(report.generated_at)
    except ValueError:
        return datetime.now(UTC)


def persist_report(report: Report, database_url: str) -> int:
    """Write one report (and its suites and metrics) and return the report id."""
    _ensure_backend_on_path()
    from app.models import EvaluationMetric, EvaluationReport, EvaluationSuite

    engine = create_engine(normalise_url(database_url), future=True)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    suites_total = len(report.suites)
    suites_passed = sum(1 for suite in report.suites if suite.status == "passed")
    cases_total = sum(len(suite.cases) for suite in report.suites)
    cases_passed = sum(suite.cases_passed for suite in report.suites)

    with factory() as session:
        row = EvaluationReport(
            generated_at=_generated_at(report),
            status=report.status,
            git_commit=report.git_commit,
            git_dirty=report.git_dirty,
            methodology_version=report.methodology_version,
            suites_total=suites_total,
            suites_passed=suites_passed,
            cases_total=cases_total,
            cases_passed=cases_passed,
            duration_ms=sum(suite.duration_ms for suite in report.suites),
            report_json=report.to_dict(),
        )
        session.add(row)
        session.flush()

        for suite in report.suites:
            session.add(
                EvaluationSuite(
                    report_id=row.id,
                    suite_id=suite.id,
                    title=suite.title,
                    status=suite.status,
                    duration_ms=suite.duration_ms,
                    cases_total=len(suite.cases),
                    cases_passed=suite.cases_passed,
                    error=suite.error,
                )
            )
            for metric in suite.metrics:
                value_type, value_num, value_text = classify_value(metric.value)
                session.add(
                    EvaluationMetric(
                        report_id=row.id,
                        suite_id=suite.id,
                        suite_title=suite.title,
                        label=metric.label,
                        value_type=value_type,
                        value_num=value_num,
                        value_text=value_text,
                        unit=metric.unit,
                        detail=metric.detail,
                    )
                )
        session.commit()
        return row.id
