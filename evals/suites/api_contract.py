"""Suite 4 — does the public API serve exactly what was stored, and nothing more?

The site's promise is that the numbers on the page are the numbers the pipeline
computed. That is a claim about the serialisation layer, so it is checked by
reading the board out of the database and comparing it field by field against the
JSON the API returns.

The rest of the suite checks the shape of the public surface, which is a cost and
safety property rather than a correctness one:

* every documented endpoint answers, and unknown ids 404 rather than 500;
* a malformed date is a 422, not a stack trace;
* writes are rejected — the API is read-only by construction, not by convention;
* the two endpoints that could be asked for an unbounded amount of data clamp the
  request instead of honouring it;
* published responses carry a cache header, so normal traffic does not re-query.

Latency here is measured over an in-memory SQLite database in the same process as
the client. It is a floor for the production figure, and is labelled as one.
"""

from __future__ import annotations

import time
import traceback

from evals.harness import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_PASSED,
    Case,
    Metric,
    Suite,
    Timer,
)
from evals.world import World

SUITE_ID = "api_contract"
TITLE = "Public API contract"
DESCRIPTION = (
    "Exercises every public endpoint over the published board, then compares the "
    "served JSON against the stored calculations field by field."
)

#: Compared between the API payload and the database row behind it.
SERVED_FIELDS: tuple[tuple[str, str], ...] = (
    ("observed_value", "observed_value"),
    ("calculation.deviation", "deviation"),
    ("calculation.robust_deviation", "robust_deviation"),
    ("calculation.z_score", "z_score"),
    ("calculation.z_valid", "z_valid"),
    ("calculation.percentile", "percentile"),
    ("calculation.tail_probability", "tail_probability"),
    ("calculation.return_period_years", "return_period_years"),
    ("calculation.surprisal", "surprisal"),
    ("calculation.margin_bonus", "margin_bonus"),
    ("calculation.anomaly_score", "anomaly_score"),
    ("baseline.mean", "baseline_mean"),
    ("baseline.median", "baseline_median"),
    ("baseline.std", "baseline_std"),
    ("baseline.n", "baseline_n"),
)


def _dig(payload: dict, path: str):
    node = payload
    for part in path.split("."):
        node = node[part]
    return node


def run(world: World) -> Suite:
    suite = Suite(id=SUITE_ID, title=TITLE, description=DESCRIPTION)

    with Timer() as timer:
        try:
            suite_body(suite, world)
        except Exception:
            suite.status = STATUS_ERROR
            suite.error = traceback.format_exc(limit=8)
            suite.duration_ms = timer.elapsed_ms
            return suite
    suite.duration_ms = timer.elapsed_ms
    suite.status = STATUS_FAILED if any(not c.passed for c in suite.cases) else STATUS_PASSED
    return suite


def suite_body(suite: Suite, world: World) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.api.deps import reset_limiter
    from app.domain import METHODOLOGY_VERSION
    from app.main import create_app
    from app.models import AnomalyEvent, DailyRanking, PipelineRun

    world.install_as_app_db()
    reset_limiter()

    latencies: list[float] = []
    #: Route templates actually exercised, so the count in the report is derived
    #: from what ran rather than asserted.
    exercised: set[str] = set()

    def expect(
        case_id: str,
        title: str,
        method: str,
        path: str,
        status_code: int,
        *,
        client: TestClient,
        params: dict | None = None,
        category: str = "endpoint",
        endpoint: str | None = None,
    ) -> dict | None:
        started = time.perf_counter()
        response = client.request(method, path, params=params)
        elapsed = (time.perf_counter() - started) * 1000.0
        latencies.append(elapsed)
        exercised.add(endpoint or path)
        ok = response.status_code == status_code
        suite.cases.append(
            Case(
                id=case_id,
                title=title,
                passed=ok,
                category=category,
                expected=f"{method} {path} → {status_code}",
                observed=f"{response.status_code} in {elapsed:.1f} ms",
            )
        )
        if not ok:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    with world.session() as session:
        run_row = session.scalars(
            select(PipelineRun)
            .where(PipelineRun.published.is_(True))
            .order_by(PipelineRun.analysis_date.desc())
            .limit(1)
        ).first()
        if run_row is None:
            raise RuntimeError("the scratch world published no board")
        analysis_date = run_row.analysis_date
        stored = dict(
            session.execute(
                select(DailyRanking.rank, AnomalyEvent)
                .join(AnomalyEvent, AnomalyEvent.id == DailyRanking.event_id)
                .where(DailyRanking.run_id == run_row.id)
                .order_by(DailyRanking.rank)
            ).all()
        )
        city_id = next(iter(stored.values())).city_id
        event_id = next(iter(stored.values())).id

    app = create_app(world.settings)
    with TestClient(app) as client:
        health = expect("health", "Health endpoint answers", "GET", "/health", 200, client=client)
        suite.cases.append(
            Case(
                id="health_reports_publication",
                title="Health reports the published analysis date",
                passed=bool(health) and health.get("latest_published_date") == str(analysis_date),
                category="endpoint",
                expected=f"latest_published_date == {analysis_date}",
                observed=str(health.get("latest_published_date")) if health else "no payload",
            )
        )

        latest = expect(
            "rankings_latest",
            "Latest board is served",
            "GET",
            "/api/rankings/latest",
            200,
            client=client,
        )
        by_date = expect(
            "rankings_by_date",
            "Board for an explicit date is served",
            "GET",
            f"/api/rankings/{analysis_date}",
            200,
            client=client,
            endpoint="/api/rankings/{analysis_date}",
        )
        expect(
            "rankings_bad_date",
            "A malformed date is rejected, not crashed on",
            "GET",
            "/api/rankings/not-a-date",
            422,
            client=client,
            category="validation",
            endpoint="/api/rankings/{analysis_date}",
        )
        archive = expect(
            "rankings_archive",
            "Archive of published dates is served",
            "GET",
            "/api/rankings",
            200,
            client=client,
        )
        cities = expect(
            "cities_list", "City registry is served", "GET", "/api/cities", 200, client=client
        )
        expect(
            "city_detail",
            "City detail is served",
            "GET",
            f"/api/cities/{city_id}",
            200,
            client=client,
            endpoint="/api/cities/{city_id}",
        )
        expect(
            "city_unknown",
            "An unknown city is a 404",
            "GET",
            "/api/cities/not-a-city",
            404,
            client=client,
            category="validation",
            endpoint="/api/cities/{city_id}",
        )
        # Anchored on the analysed date rather than today, so the endpoint is asked
        # for a window that actually contains data and the point count below means
        # something.
        history = expect(
            "city_history",
            "City history is served",
            "GET",
            f"/api/cities/{city_id}/history",
            200,
            client=client,
            params={"end": str(analysis_date), "days": 30},
            endpoint="/api/cities/{city_id}/history",
        )
        suite.cases.append(
            Case(
                id="city_history_has_points",
                title="City history returns the days the pipeline stored",
                passed=bool(history) and history["count"] > 0,
                category="endpoint",
                expected="at least one observation in the requested window",
                observed=f"{history['count']} points" if history else "no payload",
            )
        )
        expect(
            "event_detail",
            "Event detail is served",
            "GET",
            f"/api/events/{event_id}",
            200,
            client=client,
            endpoint="/api/events/{event_id}",
        )
        expect(
            "event_unknown",
            "An unknown event is a 404",
            "GET",
            "/api/events/not-an-event",
            404,
            client=client,
            category="validation",
            endpoint="/api/events/{event_id}",
        )
        methodology = expect(
            "methodology",
            "Methodology is served from live configuration",
            "GET",
            "/api/methodology",
            200,
            client=client,
        )

        # Read-only: no write verb is routed anywhere on the public surface.
        for verb in ("POST", "PUT", "PATCH", "DELETE"):
            expect(
                f"read_only_{verb.lower()}",
                f"{verb} to a published resource is refused",
                verb,
                "/api/rankings/latest",
                405,
                client=client,
                category="read_only",
            )

        # Unbounded-query protection: both clamps are asserted against the
        # configured ceiling rather than a literal, so raising a limit in config
        # cannot silently pass a stale test.
        clamped = expect(
            "pagination_clamped",
            "An oversized page request is clamped",
            "GET",
            "/api/rankings",
            200,
            client=client,
            params={"limit": 100_000},
            category="bounded_query",
        )
        suite.cases.append(
            Case(
                id="pagination_ceiling",
                title="Page size never exceeds MAX_PAGE_SIZE",
                passed=bool(clamped) and clamped["limit"] <= world.settings.max_page_size,
                category="bounded_query",
                expected=f"limit <= {world.settings.max_page_size}",
                observed=str(clamped.get("limit")) if clamped else "no payload",
            )
        )
        long_history = expect(
            "history_clamped",
            "An oversized history window is clamped",
            "GET",
            f"/api/cities/{city_id}/history",
            200,
            client=client,
            params={"days": 100_000},
            category="bounded_query",
            endpoint="/api/cities/{city_id}/history",
        )
        if long_history:
            from datetime import date as date_cls

            start = date_cls.fromisoformat(long_history["start_date"])
            end = date_cls.fromisoformat(long_history["end_date"])
            span = (end - start).days + 1
        else:
            span = None
        suite.cases.append(
            Case(
                id="history_ceiling",
                title="History window never exceeds MAX_HISTORY_DAYS",
                passed=span is not None and span <= world.settings.max_history_days,
                category="bounded_query",
                expected=f"window <= {world.settings.max_history_days} days",
                observed=f"{span} days" if span is not None else "no payload",
            )
        )

        cache_probe = client.get("/api/rankings/latest")
        suite.cases.append(
            Case(
                id="cache_header",
                title="Published responses are cacheable",
                passed="max-age" in cache_probe.headers.get("cache-control", ""),
                category="caching",
                expected="Cache-Control carries a max-age",
                observed=cache_probe.headers.get("cache-control", "absent"),
                detail="Keeps normal page views off the database and away from any LLM.",
            )
        )

    # -- the board the API served must be the board that was stored ----------
    mismatches: list[str] = []
    compared = 0
    if latest:
        for entry in latest["events"]:
            event = stored.get(entry["rank"])
            if event is None:
                mismatches.append(f"rank {entry['rank']} not in the stored board")
                continue
            if entry["event"]["id"] != event.id:
                mismatches.append(f"rank {entry['rank']} event id")
            for json_path, column in SERVED_FIELDS:
                compared += 1
                served = _dig(entry["event"], json_path)
                actual = getattr(event, column)
                if served != actual:
                    mismatches.append(f"rank {entry['rank']} {json_path}: {served} vs {actual}")

    suite.cases.append(
        Case(
            id="served_matches_stored",
            title="Every served figure equals the stored calculation",
            passed=bool(latest) and not mismatches,
            category="integrity",
            expected=f"{compared} fields identical",
            observed="identical" if not mismatches else "; ".join(mismatches[:5]),
        )
    )
    suite.cases.append(
        Case(
            id="latest_matches_explicit_date",
            title="The latest board and the board for its date are the same payload",
            passed=bool(latest) and bool(by_date) and latest["events"] == by_date["events"],
            category="integrity",
            expected="identical event lists",
            observed="identical"
            if latest and by_date and latest["events"] == by_date["events"]
            else "differ",
        )
    )
    suite.cases.append(
        Case(
            id="result_type_is_not_a_record",
            title="Results are labelled statistical outliers, never records",
            passed=bool(latest)
            and latest["result_type"] == "statistical_outliers"
            and all(
                e["event"]["is_statistical_outlier"] and not e["event"]["is_verified_official_record"]
                for e in latest["events"]
            ),
            category="integrity",
            expected="result_type=statistical_outliers on the board and on every event",
            observed=latest["result_type"] if latest else "no payload",
            detail="No official records archive is consulted anywhere in this project.",
        )
    )
    suite.cases.append(
        Case(
            id="methodology_version_agrees",
            title="Served methodology version matches the code",
            passed=bool(methodology)
            and methodology["methodology_version"] == METHODOLOGY_VERSION,
            category="integrity",
            expected=METHODOLOGY_VERSION,
            observed=methodology.get("methodology_version") if methodology else "no payload",
        )
    )

    suite.metrics = [
        Metric(
            "Endpoints exercised",
            len(exercised),
            "endpoints",
            ", ".join(sorted(exercised)),
        ),
        Metric("Requests issued", len(latencies), "requests"),
        Metric(
            "Median response",
            round(sorted(latencies)[len(latencies) // 2], 2) if latencies else None,
            "ms",
            "in-process client over in-memory SQLite; a floor for production, not a "
            "production measurement",
        ),
        Metric(
            "Slowest response",
            round(max(latencies), 2) if latencies else None,
            "ms",
        ),
        Metric("Board rows served", len(latest["events"]) if latest else None, "events"),
        Metric("Fields cross-checked against the database", compared, "fields"),
        Metric("Mismatches", len(mismatches), "fields"),
        Metric("Cities served", cities["count"] if cities else None, "cities"),
        Metric("History points served", history["count"] if history else None, "days"),
        Metric("Published dates in the archive", archive["total"] if archive else None, "dates"),
    ]
