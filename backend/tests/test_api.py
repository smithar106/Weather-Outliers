"""API contract tests.

These check the things a consumer of this API would be burned by: that a number
on the page can be traced back to the row it came from, that the honesty
labelling is structurally present rather than editorial, that a missing daily run
degrades to the last good one instead of an empty page, and that no request shape
can ask the database for an unbounded scan.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.api.deps import reset_limiter
from app.domain import METHODOLOGY_VERSION, DataTier, Metric, RunStatus
from tests.conftest import (
    make_baseline_row,
    make_city,
    make_event,
    make_observation,
    make_run,
    synthetic_temperatures,
)

ANALYSIS_DATE = date(2026, 9, 21)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_is_ok_once_something_is_published(client, seeded):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["cities"] == 1
    assert body["latest_published_date"] == ANALYSIS_DATE.isoformat()
    assert body["methodology_version"] == METHODOLOGY_VERSION
    assert body["llm_enabled"] is False
    assert body["hours_since_publish"] is not None


def test_health_is_degraded_before_the_first_run(client):
    """An empty database is a real state on a fresh deploy, not an error."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["latest_published_date"] is None


def test_health_is_never_cached(client, seeded):
    assert client.get("/health").headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# Rankings
# ---------------------------------------------------------------------------


def test_latest_rankings_carry_the_full_provenance_block(client, seeded):
    body = client.get("/api/rankings/latest").json()

    assert body["analysis_date"] == ANALYSIS_DATE.isoformat()
    assert body["published_at"] is not None
    assert body["methodology_version"] == METHODOLOGY_VERSION
    assert body["result_type"] == "statistical_outliers"
    assert body["is_latest_available"] is True
    assert body["requested_date"] is None
    assert body["count"] == 1
    assert body["run"]["run_id"] == "run-test-1"
    assert len(body["data_sources"]) >= 2
    assert all(src["attribution"] for src in body["data_sources"])


def test_ranked_event_numbers_match_the_stored_calculation(client, seeded):
    """The board is not allowed to round, rescale, or relabel the stored values."""
    stored = seeded["event"]
    ranked = client.get("/api/rankings/latest").json()["events"][0]

    assert ranked["rank"] == 1
    assert ranked["score"] == pytest.approx(stored.anomaly_score)

    event = ranked["event"]
    assert event["id"] == stored.id
    assert event["observed_value"] == pytest.approx(stored.observed_value)
    assert event["unit"] == stored.unit
    assert event["calculation"]["percentile"] == pytest.approx(stored.percentile)
    assert event["calculation"]["tail_probability"] == pytest.approx(
        stored.tail_probability
    )
    assert event["calculation"]["anomaly_score"] == pytest.approx(stored.anomaly_score)
    assert event["baseline"]["n"] == stored.baseline_n


def test_every_event_is_labelled_as_an_outlier_not_a_record(client, seeded):
    """The honesty labels are fields, not prose, so a client cannot omit them."""
    event = client.get("/api/rankings/latest").json()["events"][0]["event"]
    assert event["is_statistical_outlier"] is True
    assert event["is_verified_official_record"] is False
    assert event["observation_type"] == "reanalysis"
    assert event["source_dataset"] == "era5_seamless"


def test_events_expose_their_metric_category_and_label(client, seeded):
    event = client.get("/api/rankings/latest").json()["events"][0]["event"]
    assert event["metric"] == Metric.TEMP_MAX.value
    assert event["metric_label"]
    assert event["category"]
    assert event["direction"] in ("above", "below")


def test_rankings_for_a_date_that_was_published(client, seeded):
    body = client.get(f"/api/rankings/{ANALYSIS_DATE.isoformat()}").json()
    assert body["is_latest_available"] is True
    assert body["requested_date"] is None
    assert body["analysis_date"] == ANALYSIS_DATE.isoformat()


def test_a_missing_run_falls_back_to_the_last_published_board(client, seeded):
    """The requirement this encodes: a failed daily run must not blank the site.

    The response is explicitly marked, so the UI can say "showing 21 September,
    no analysis published for 22 September" rather than silently lying about
    which day the reader is looking at.
    """
    missing = ANALYSIS_DATE + timedelta(days=1)
    body = client.get(f"/api/rankings/{missing.isoformat()}").json()

    assert body["is_latest_available"] is False
    assert body["requested_date"] == missing.isoformat()
    assert body["analysis_date"] == ANALYSIS_DATE.isoformat()
    assert body["count"] == 1


def test_an_unpublished_run_is_invisible(client, session):
    """A failed or in-flight run must not leak into a public response."""
    city = make_city(session)
    run = make_run(
        session,
        run_id="run-failed",
        published=False,
        status=RunStatus.FAILED.value,
    )
    make_baseline_row(session, city)
    make_event(session, city, run=run, rank=1)

    assert client.get("/api/rankings/latest").status_code == 404
    assert client.get("/health").json()["status"] == "degraded"


def test_a_newer_failed_run_does_not_displace_an_older_good_one(client, session):
    city = make_city(session)
    good = make_run(session, run_id="run-good", analysis_date=ANALYSIS_DATE)
    make_baseline_row(session, city)
    make_event(session, city, run=good, rank=1)
    make_run(
        session,
        run_id="run-bad",
        analysis_date=ANALYSIS_DATE + timedelta(days=1),
        published=False,
        status=RunStatus.FAILED.value,
    )

    body = client.get("/api/rankings/latest").json()
    assert body["analysis_date"] == ANALYSIS_DATE.isoformat()
    assert body["run"]["run_id"] == "run-good"


def test_no_published_analysis_is_a_clear_404(client):
    response = client.get("/api/rankings/latest")
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "not_found"
    assert "pipeline" in body["detail"].lower()
    assert body["status_code"] == 404


def test_a_malformed_date_is_a_422_that_says_what_was_expected(client, seeded):
    response = client.get("/api/rankings/21-09-2026")
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid_request"
    assert "YYYY-MM-DD" in body["detail"]


def test_published_results_are_cacheable(client, seeded):
    cache = client.get("/api/rankings/latest").headers["cache-control"]
    assert "public" in cache
    assert "max-age=300" in cache


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


def test_archive_lists_published_dates_newest_first(client, session):
    city = make_city(session)
    make_baseline_row(session, city)
    for offset in range(3):
        day = ANALYSIS_DATE - timedelta(days=offset)
        run = make_run(session, run_id=f"run-{offset}", analysis_date=day)
        make_event(session, city, run=run, local_date=day, rank=1)

    body = client.get("/api/rankings").json()
    assert body["total"] == 3
    assert body["count"] == 3
    dates = [e["analysis_date"] for e in body["entries"]]
    assert dates == sorted(dates, reverse=True)
    assert body["entries"][0]["top_city"] == "Phoenix"
    assert body["entries"][0]["event_count"] == 1


def test_archive_pagination_is_clamped_to_the_configured_maximum(client, seeded):
    body = client.get("/api/rankings?limit=100000").json()
    assert body["limit"] == 200  # max_page_size, not what was asked for
    assert body["count"] <= body["limit"]


def test_archive_rejects_a_nonsense_limit(client, seeded):
    assert client.get("/api/rankings?limit=0").status_code == 422
    assert client.get("/api/rankings?limit=abc").status_code == 422


# ---------------------------------------------------------------------------
# Cities
# ---------------------------------------------------------------------------


def test_city_list_reports_the_registry_version(client, seeded):
    body = client.get("/api/cities").json()
    assert body["count"] == 1
    assert body["registry_version"] == "test"
    city = body["cities"][0]
    assert city["timezone"] == "America/Phoenix"
    assert city["latitude"] == pytest.approx(33.4484)


def test_city_list_filters_by_country(client, session):
    make_city(session)
    make_city(
        session,
        city_id="ca-toronto-on",
        name="Toronto",
        admin="Ontario",
        country="CA",
        region="Canada Central",
        latitude=43.6532,
        longitude=-79.3832,
        timezone="America/Toronto",
    )

    assert client.get("/api/cities?country=CA").json()["count"] == 1
    assert client.get("/api/cities?country=ca").json()["count"] == 1
    assert client.get("/api/cities").json()["count"] == 2
    assert client.get("/api/cities?country=USA").status_code == 422


def test_city_detail_returns_the_reading_its_baselines_and_its_events(client, seeded):
    body = client.get("/api/cities/us-phoenix-az").json()

    assert body["city"]["name"] == "Phoenix"
    assert body["analysis_date"] == ANALYSIS_DATE.isoformat()
    assert body["latest_observation"]["temp_max_c"] == pytest.approx(31.8)
    assert body["latest_observation"]["observation_type"] == "reanalysis"
    assert len(body["events"]) == 1
    assert body["limitations"]


def test_city_baselines_are_flagged_as_not_official_normals(client, seeded):
    baselines = client.get("/api/cities/us-phoenix-az").json()["baselines"]
    assert baselines
    for baseline in baselines:
        assert baseline["not_official_normals"] is True
        assert baseline["reference_period"] == "1991-2020"
        assert baseline["source_dataset"] == "era5_seamless"


def test_unknown_city_is_a_404_naming_the_id(client, seeded):
    response = client.get("/api/cities/atlantis")
    assert response.status_code == 404
    assert "atlantis" in response.json()["detail"]


def test_city_history_prefers_final_data_over_provisional_for_the_same_day(
    client, session
):
    """Both tiers are kept in the table; only the settled one is charted."""
    city = make_city(session)
    make_observation(
        session,
        city,
        temp_max_c=30.0,
        data_tier=DataTier.PROVISIONAL.value,
        source_dataset="best_match",
    )
    make_observation(session, city, temp_max_c=31.8)

    body = client.get("/api/cities/us-phoenix-az/history").json()
    assert body["count"] == 1
    assert body["points"][0]["temp_max_c"] == pytest.approx(31.8)
    assert body["points"][0]["data_tier"] == "final"


def test_city_history_window_defaults_and_is_ordered(client, session):
    city = make_city(session)
    for offset in range(5):
        make_observation(
            session, city, local_date=ANALYSIS_DATE - timedelta(days=offset)
        )

    body = client.get(
        f"/api/cities/us-phoenix-az/history?end={ANALYSIS_DATE.isoformat()}&days=5"
    ).json()
    dates = [p["local_date"] for p in body["points"]]
    assert dates == sorted(dates)
    assert body["start_date"] == (ANALYSIS_DATE - timedelta(days=4)).isoformat()
    assert body["end_date"] == ANALYSIS_DATE.isoformat()


def test_city_history_cannot_be_asked_for_an_unbounded_range(client, seeded):
    """The expensive-query guard. 1900 to today would be a full table scan."""
    body = client.get(
        "/api/cities/us-phoenix-az/history?start=1900-01-01&end=2026-09-21"
    ).json()

    span = (
        date.fromisoformat(body["end_date"]) - date.fromisoformat(body["start_date"])
    ).days + 1
    assert span == 400  # max_history_days
    assert body["end_date"] == "2026-09-21"  # truncated from the start, not the end


def test_city_history_rejects_an_inverted_range(client, seeded):
    response = client.get(
        "/api/cities/us-phoenix-az/history?start=2026-09-21&end=2026-09-01"
    )
    assert response.status_code == 422
    assert "after" in response.json()["detail"]


def test_city_history_for_an_unknown_city_is_a_404(client, seeded):
    assert client.get("/api/cities/nowhere/history").status_code == 404


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def test_event_detail_exposes_the_full_calculation_trace(client, seeded):
    event = seeded["event"]
    body = client.get(f"/api/events/{event.id}").json()

    assert body["id"] == event.id
    assert body["city"]["id"] == "us-phoenix-az"
    assert body["evidence"] is not None
    calc = body["calculation"]
    assert calc["surprisal"] == pytest.approx(event.surprisal)
    assert calc["margin_bonus"] == pytest.approx(event.margin_bonus)
    assert calc["anomaly_score"] == pytest.approx(
        calc["surprisal"] + calc["margin_bonus"]
    )


def test_a_bounded_tail_probability_is_flagged_as_such(client, session):
    """A reading beyond every value in its sample cannot be given an exact rarity."""
    city = make_city(session)
    run = make_run(session)
    values = synthetic_temperatures()
    make_baseline_row(session, city, values=values)
    event = make_event(session, city, run=run, observed_value=max(values) + 8.0)

    calc = client.get(f"/api/events/{event.id}").json()["calculation"]
    assert calc["beyond_baseline_sample"] is True
    assert calc["tail_probability_is_bounded"] is True
    assert calc["margin_bonus"] > 0


def test_z_scores_are_not_presented_as_universally_valid(client, seeded):
    """``z_valid`` must be present on every event, whatever its value."""
    calc = client.get("/api/rankings/latest").json()["events"][0]["event"]["calculation"]
    assert "z_valid" in calc
    assert isinstance(calc["z_valid"], bool)


def test_unknown_event_is_a_404(client, seeded):
    response = client.get("/api/events/2026-09-21_nope_temp_max")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_an_event_without_an_explanation_still_serialises(client, seeded):
    """Explanations are written by a later pipeline step and may legitimately
    be absent — the card has to render anyway."""
    body = client.get(f"/api/events/{seeded['event'].id}").json()
    assert body["explanation"] is None


# ---------------------------------------------------------------------------
# Methodology
# ---------------------------------------------------------------------------


def test_methodology_is_generated_from_live_configuration(client, seeded):
    body = client.get("/api/methodology").json()

    assert body["methodology_version"] == METHODOLOGY_VERSION
    assert body["registry_version"] == "test"
    assert body["reference_period"] == "1991-2020"
    assert body["seasonal_window_days"] == 7
    assert body["seasonal_window_day_count"] == 15
    assert body["ranking_top_n"] == 10
    assert body["one_event_per_city"] is True
    assert "log10" in body["score_formula"]
    assert len(body["tiebreak_chain"]) >= 3
    assert len(body["metrics"]) == len(list(Metric))


def test_methodology_states_its_limitations_including_the_modelled_data_caveat(
    client, seeded
):
    limitations = " ".join(client.get("/api/methodology").json()["limitations"])
    assert "not readings from a weather station" in limitations or "grid" in limitations
    assert "record" in limitations
    assert "normals" in limitations


def test_methodology_describes_the_ai_bounds_and_the_no_key_fallback(client, seeded):
    ai = client.get("/api/methodology").json()["ai"]
    assert ai["llm_enabled"] is False
    assert ai["generator_default"] == "template"
    assert ai["precomputed"] is True
    assert ai["bounds"]["max_tool_calls_per_event"] > 0
    assert ai["bounds"]["monthly_max_llm_calls"] > 0
    assert len(ai["tools"]) == 5


# ---------------------------------------------------------------------------
# Cross-cutting: read-only, rate limiting, headers, docs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_write_methods_are_refused_everywhere(client, seeded, method):
    """Read-only by enforcement, not merely by the absence of handlers."""
    response = getattr(client, method)("/api/rankings/latest")
    assert response.status_code == 405
    assert response.json()["error"] == "method_not_allowed"


def test_responses_carry_a_request_id_and_the_methodology_version(client, seeded):
    headers = client.get("/api/rankings/latest").headers
    assert headers["x-request-id"]
    assert headers["x-methodology-version"] == METHODOLOGY_VERSION


def test_a_supplied_request_id_is_echoed_back(client, seeded):
    headers = client.get(
        "/api/rankings/latest", headers={"X-Request-ID": "trace-me-123"}
    ).headers
    assert headers["x-request-id"] == "trace-me-123"


def test_rate_limit_returns_429_with_a_retry_after(client, seeded, test_settings):
    reset_limiter()
    ceiling = (
        test_settings.rate_limit_requests_per_minute + test_settings.rate_limit_burst
    )
    key = {"X-Forwarded-For": "203.0.113.7"}

    last = None
    for _ in range(ceiling + 1):
        last = client.get("/api/cities", headers=key)

    assert last.status_code == 429
    assert last.json()["error"] == "rate_limited"
    assert int(last.headers["retry-after"]) >= 1


def test_the_health_check_is_never_rate_limited(client, seeded, test_settings):
    """An uptime probe must not be able to lock itself out of the platform."""
    reset_limiter()
    ceiling = (
        test_settings.rate_limit_requests_per_minute + test_settings.rate_limit_burst
    )
    key = {"X-Forwarded-For": "203.0.113.9"}
    for _ in range(ceiling + 5):
        assert client.get("/health", headers=key).status_code == 200


def test_distinct_clients_get_distinct_budgets(client, seeded, test_settings):
    reset_limiter()
    ceiling = (
        test_settings.rate_limit_requests_per_minute + test_settings.rate_limit_burst
    )
    for _ in range(ceiling + 1):
        client.get("/api/cities", headers={"X-Forwarded-For": "198.51.100.1"})

    assert (
        client.get("/api/cities", headers={"X-Forwarded-For": "198.51.100.2"}).status_code
        == 200
    )


def test_openapi_schema_is_served_and_documents_every_public_endpoint(client, seeded):
    schema = client.get("/openapi.json").json()
    paths = set(schema["paths"])
    required = {
        "/health",
        "/api/rankings/latest",
        "/api/rankings/{analysis_date}",
        "/api/cities",
        "/api/cities/{city_id}",
        "/api/cities/{city_id}/history",
        "/api/events/{event_id}",
        "/api/methodology",
    }
    assert required <= paths
    for path in required:
        assert set(schema["paths"][path]) <= {"get"}


def test_root_points_at_the_docs_and_names_the_result_type(client, seeded):
    body = client.get("/").json()
    assert body["result_type"] == "statistical_outliers"
    assert body["docs"] == "/docs"
