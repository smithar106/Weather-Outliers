"""Suite 3 — is a published board reproducible, and is publishing atomic?

Three questions, each answered by running the real pipeline rather than by
inspecting it:

1. **Same input, same board.** The pipeline runs a second time over the same date
   in the same database, and a third time in a database built from scratch. All
   three boards are compared field by field — every stored statistic, not just the
   ordering. A difference anywhere is a failure, because the site's claim is that
   the numbers are auditable, and a number that changes between runs cannot be.
2. **Idempotency.** Rerunning a date must not duplicate events. The event id is
   derived from ``(date, city, metric, methodology version)`` precisely so a retry
   is safe, and this checks that the derivation holds end to end.
3. **A failed run must not overwrite a good one.** The pipeline is run against a
   provider that fails on every request. The run must fail, and the previously
   published board must still be the board the API serves.

Everything runs against ``synthetic_fixture_v1``. That makes the determinism claim
meaningful — the fixture is a pure function of ``(city, date)`` — and makes any
claim about real-world accuracy impossible. Only the former is claimed.
"""

from __future__ import annotations

import logging
import traceback
from datetime import date

from evals.harness import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_PASSED,
    Case,
    Metric,
    Suite,
    Timer,
    ratio,
)
from evals.world import EVAL_NOW_UTC, World, build_world

SUITE_ID = "reproducibility"
TITLE = "Pipeline reproducibility"
DESCRIPTION = (
    "Runs the pipeline repeatedly over one synthetic date — twice in one database "
    "and once in a fresh one — and compares every stored statistic. Also verifies "
    "that a failing run leaves the published board alone."
)

#: Every persisted number behind a ranked row. Comparing the whole trace rather
#: than the ordering is the point: a board can be ordered identically while the
#: figures under it drift.
BOARD_FIELDS: tuple[str, ...] = (
    "id",
    "city_id",
    "metric",
    "direction",
    "observed_value",
    "unit",
    "baseline_mean",
    "baseline_median",
    "baseline_std",
    "baseline_p25",
    "baseline_p75",
    "baseline_min",
    "baseline_max",
    "baseline_n",
    "baseline_sufficient",
    "deviation",
    "robust_deviation",
    "z_score",
    "z_valid",
    "percentile",
    "tail_probability",
    "return_period_years",
    "tail_probability_is_bounded",
    "beyond_baseline_sample",
    "surprisal",
    "margin_bonus",
    "anomaly_score",
    "methodology_version",
)


class _FailingProvider:
    """A provider that cannot answer, to prove a failed run publishes nothing."""

    name = "failing_fixture"

    def __init__(self) -> None:
        self.request_count = 0
        self.error_count = 0

    def fetch_daily(self, **_kwargs):
        from app.providers.base import ProviderError

        self.request_count += 1
        self.error_count += 1
        raise ProviderError("synthetic outage injected by the evaluation harness")

    def close(self) -> None:
        return None


def _read_board(world: World, analysis_date: date) -> list[dict]:
    """The published board for a date, as a list of comparable dicts in rank order."""
    from sqlalchemy import select

    from app.models import AnomalyEvent, DailyRanking, PipelineRun

    with world.session() as session:
        run = session.scalars(
            select(PipelineRun).where(
                PipelineRun.published.is_(True),
                PipelineRun.analysis_date == analysis_date,
            )
        ).first()
        if run is None:
            return []
        rows = session.execute(
            select(DailyRanking.rank, AnomalyEvent)
            .join(AnomalyEvent, AnomalyEvent.id == DailyRanking.event_id)
            .where(DailyRanking.run_id == run.id)
            .order_by(DailyRanking.rank)
        ).all()

    board: list[dict] = []
    for rank, event in rows:
        entry: dict = {"rank": rank}
        for field in BOARD_FIELDS:
            entry[field] = getattr(event, field)
        board.append(entry)
    return board


def _compare(left: list[dict], right: list[dict]) -> tuple[int, list[str]]:
    """``(fields compared, differences)``."""
    compared = 0
    diffs: list[str] = []
    if len(left) != len(right):
        diffs.append(f"board length {len(left)} vs {len(right)}")
    for a, b in zip(left, right, strict=False):
        for field in ("rank", *BOARD_FIELDS):
            compared += 1
            if a[field] != b[field]:
                diffs.append(f"rank {a['rank']} {field}: {a[field]!r} vs {b[field]!r}")
    return compared, diffs


def _rerun_same_database(world: World) -> tuple[list[dict], int, int]:
    """Run the same date again in place. Returns (board, events before, events after)."""
    from sqlalchemy import func, select

    from app.models import AnomalyEvent
    from app.pipeline.runner import Pipeline
    from app.providers.fixture import FixtureProvider

    with world.session() as session:
        before = session.execute(select(func.count(AnomalyEvent.id))).scalar_one()
        pipeline = Pipeline(session, world.settings, FixtureProvider())
        report = pipeline.run_daily(world.analysis_date, now_utc=EVAL_NOW_UTC)
        pipeline.close()
        after = session.execute(select(func.count(AnomalyEvent.id))).scalar_one()

    if report.status != "succeeded":
        raise RuntimeError(f"rerun failed: {report.error}")
    return _read_board(world, world.analysis_date), before, after


def _failed_run_leaves_board(world: World) -> tuple[bool, str]:
    """``(publish survived, what the failed run reported)``."""
    from app.pipeline.runner import Pipeline

    # The outage is deliberate and its failure is the passing result, so the
    # pipeline's own error logging is muted for the duration rather than printed
    # into the harness summary where it would read as a problem.
    quiet = logging.getLogger("app.pipeline.runner")
    previous = quiet.level
    quiet.setLevel(logging.CRITICAL)
    try:
        with world.session() as session:
            pipeline = Pipeline(session, world.settings, _FailingProvider())
            report = pipeline.run_daily(world.analysis_date, now_utc=EVAL_NOW_UTC)
            pipeline.close()
    finally:
        quiet.setLevel(previous)
    return (report.status == "failed" and not report.published), (report.error or "no error")


def _run_status_counts(world: World) -> dict[str, int]:
    from sqlalchemy import func, select

    from app.models import PipelineRun

    with world.session() as session:
        rows = session.execute(
            select(PipelineRun.status, func.count(PipelineRun.id)).group_by(PipelineRun.status)
        ).all()
    return dict(rows)


def run(world: World) -> Suite:
    suite = Suite(id=SUITE_ID, title=TITLE, description=DESCRIPTION)
    fresh: World | None = None

    with Timer() as timer:
        try:
            first = _read_board(world, world.analysis_date)
            if not first:
                raise RuntimeError("the scratch world published no board to compare")

            second, events_before, events_after = _rerun_same_database(world)
            rerun_fields, rerun_diffs = _compare(first, second)

            with Timer() as fresh_timer:
                fresh = build_world()
            third = _read_board(fresh, fresh.analysis_date)
            cross_fields, cross_diffs = _compare(first, third)

            publish_survived, failure_detail = _failed_run_leaves_board(world)
            after_failure = _read_board(world, world.analysis_date)
            _, failure_diffs = _compare(second, after_failure)

            statuses = _run_status_counts(world)
        except Exception:
            suite.status = STATUS_ERROR
            suite.error = traceback.format_exc(limit=8)
            suite.duration_ms = timer.elapsed_ms
            if fresh is not None:
                fresh.dispose()
            return suite
    suite.duration_ms = timer.elapsed_ms

    suite.cases = [
        Case(
            id="rerun_same_database",
            title="Rerunning the same date reproduces the board exactly",
            passed=not rerun_diffs,
            category="determinism",
            expected=f"{rerun_fields} fields identical",
            observed="identical" if not rerun_diffs else "; ".join(rerun_diffs[:4]),
        ),
        Case(
            id="fresh_database",
            title="A database built from scratch produces the same board",
            passed=not cross_diffs,
            category="determinism",
            expected=f"{cross_fields} fields identical",
            observed="identical" if not cross_diffs else "; ".join(cross_diffs[:4]),
        ),
        Case(
            id="idempotent_events",
            title="Rerunning a date creates no duplicate events",
            passed=events_after == events_before,
            category="idempotency",
            expected=f"{events_before} events before and after",
            observed=f"{events_after} events after the rerun",
            detail="Event ids are derived from (date, city, metric, methodology version).",
        ),
        Case(
            id="failed_run_does_not_publish",
            title="A run whose provider fails does not publish",
            passed=publish_survived,
            category="atomicity",
            expected="run status failed, published false",
            observed=failure_detail,
        ),
        Case(
            id="failed_run_preserves_board",
            title="The previously published board survives a failed run",
            passed=not failure_diffs,
            category="atomicity",
            expected="board unchanged",
            observed="unchanged" if not failure_diffs else "; ".join(failure_diffs[:4]),
        ),
    ]

    report = world.run_report
    succeeded = statuses.get("succeeded", 0)
    total_runs = sum(statuses.values())
    #: The outage this suite injects. Counted so the completion rate above is a
    #: statement about the pipeline rather than about the harness.
    injected_failures = 1

    suite.metrics = [
        Metric("Cities analysed", report.cities_total if report else None, "cities"),
        Metric(
            "Data completeness",
            round(report.completeness, 4) if report else None,
            "fraction",
            "cities with a usable observation for the analysed date",
        ),
        Metric("Events computed", report.events_total if report else None, "events"),
        Metric("Events published", report.events_published if report else None, "events"),
        Metric(
            "Events ineligible",
            report.events_excluded if report else None,
            "events",
            "computed but excluded from the board by a documented rule",
        ),
        Metric("Fields compared per board pair", rerun_fields, "fields"),
        Metric("Differences across reruns", len(rerun_diffs), "fields"),
        Metric("Differences across databases", len(cross_diffs), "fields"),
        Metric("Pipeline runs recorded", total_runs, "runs"),
        Metric(
            "Pipeline completion rate",
            ratio(succeeded, total_runs - injected_failures),
            "fraction",
            f"{succeeded} of {total_runs - injected_failures} runs given a working "
            f"provider succeeded. A further {injected_failures} run was failed on "
            "purpose to test atomic publish and is excluded from the rate.",
        ),
        Metric(
            "Runs that failed",
            statuses.get("failed", 0),
            "runs",
            "all of them deliberately injected by this suite",
        ),
        Metric(
            "Baseline build",
            world.baseline_ms,
            "ms",
            f"{world.baseline_rows} cached rows from {world.provider_requests} "
            "provider requests, done once rather than daily",
        ),
        Metric(
            "Daily run end to end",
            world.run_ms,
            "ms",
            "fetch, score, rank, explain, publish — against the synthetic provider "
            "and an in-memory database, so this is a floor, not a production figure",
        ),
        Metric("Fresh world build", fresh_timer.elapsed_ms, "ms"),
        Metric(
            "Weather requests per daily run",
            report.provider_requests if report else None,
            "requests",
            "one per city; the cost driver the baseline cache exists to avoid "
            "multiplying by thirty years",
        ),
        Metric(
            "Weather requests to build the cache",
            world.provider_requests,
            "requests",
            "paid once, not daily",
        ),
        Metric(
            "LLM calls per daily run",
            report.llm_calls if report else None,
            "calls",
            "bounded by AGENT_INVESTIGATE_TOP_N and the monthly ceiling; zero here "
            "because no key was configured",
        ),
        Metric(
            "Estimated LLM spend per run",
            round(report.llm_estimated_usd, 6) if report else None,
            "USD",
            "derived from configured token prices. With no prices configured this is "
            "0.0 and means 'not priced', not 'free'.",
        ),
    ]

    suite.notes.append(
        "Runs against synthetic_fixture_v1, a deterministic function of (city, date). "
        "This measures reproducibility of the machinery, not accuracy of real weather."
    )
    suite.status = STATUS_FAILED if any(not c.passed for c in suite.cases) else STATUS_PASSED

    if fresh is not None:
        fresh.dispose()
    return suite
