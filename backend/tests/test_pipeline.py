"""Pipeline integration tests.

These run the real thing end to end — ingestion, cached climatology, anomaly
scoring, ranking, deterministic explanations, atomic publish — against the
deterministic fixture provider, so they are offline and reproducible.

Four properties are worth more than the rest, and each has a test named after it:

* a failed run leaves the previously published board serving unchanged;
* a rerun of the same date is idempotent, producing no duplicate events and no
  second rank 1;
* a day whose fetches mostly failed is refused rather than published thin;
* the analysis date is the latest local day that is complete in *every* city.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, insert, select

from app.config import Settings, get_settings
from app.domain import DataTier, RunKind, RunStatus
from app.models import (
    AgentExplanation,
    AnomalyEvent,
    BaselineStatistic,
    City,
    DailyRanking,
    PipelineRun,
    WeatherObservation,
)
from app.pipeline.runner import (
    Pipeline,
    PipelineError,
    backfill,
    finalize,
    resolve_analysis_date,
)
from app.providers.base import ProviderError
from app.providers.fixture import FixtureProvider
from tests.conftest import make_city

ANALYSIS_DATE = date(2026, 9, 21)
# Two days after the analysed date: late enough that every city's local day is
# closed, early enough that the reanalysis has not landed, so a default run is
# provisional and `finalize` has something to do.
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

CITY_SPECS = [
    # id, name, admin, country, region, lat, lon, tz
    ("us-phoenix-az", "Phoenix", "Arizona", "US", "US West", 33.4484, -112.0740, "America/Phoenix"),
    ("ca-toronto-on", "Toronto", "Ontario", "CA", "Canada Central", 43.6532, -79.3832, "America/Toronto"),
    ("mx-monterrey-nl", "Monterrey", "Nuevo León", "MX", "Mexico North", 25.6866, -100.3161, "America/Monterrey"),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_BASELINE_CACHE: dict[tuple, list[dict]] = {}


def _baseline_rows(settings: Settings) -> list[dict]:
    """Build the climatology for the test cities once per session, then reuse it.

    Building three cities takes a couple of seconds, which is cheap once and
    tiresome fifteen times. The rows are cached as plain column dicts keyed by the
    settings that affect them, so a test that changed the reference period would
    get its own build rather than a stale one.
    """
    key = (
        settings.baseline_start_year,
        settings.baseline_end_year,
        settings.baseline_seasonal_window_days,
        settings.weather_provider,
    )
    cached = _BASELINE_CACHE.get(key)
    if cached is not None:
        return cached

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.ingest.baselines import build_city_baselines
    from app.models import Base

    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as scratch:
        for spec in CITY_SPECS:
            make_city(
                scratch,
                city_id=spec[0],
                name=spec[1],
                admin=spec[2],
                country=spec[3],
                region=spec[4],
                latitude=spec[5],
                longitude=spec[6],
                timezone=spec[7],
            )
        provider = FixtureProvider()
        for city in scratch.execute(select(City)).scalars().all():
            build_city_baselines(scratch, city, provider, settings)
        scratch.commit()

        columns = [c.name for c in BaselineStatistic.__table__.columns if c.name != "id"]
        rows = [
            {name: getattr(row, name) for name in columns}
            for row in scratch.execute(select(BaselineStatistic)).scalars().all()
        ]
    engine.dispose()

    _BASELINE_CACHE[key] = rows
    return rows


@pytest.fixture
def world(session, test_settings):
    """Three cities across three time zones, with their climatology cached."""
    for spec in CITY_SPECS:
        make_city(
            session,
            city_id=spec[0],
            name=spec[1],
            admin=spec[2],
            country=spec[3],
            region=spec[4],
            latitude=spec[5],
            longitude=spec[6],
            timezone=spec[7],
        )
    rows = _baseline_rows(test_settings)
    session.execute(insert(BaselineStatistic), rows)
    session.commit()
    return session


@pytest.fixture
def pipeline(world, test_settings):
    p = Pipeline(world, test_settings, FixtureProvider())
    yield p
    p.close()


class BrokenProvider:
    """A provider that fails for every city, or for all but the first `ok` of them."""

    name = "broken"

    def __init__(self, ok: int = 0) -> None:
        self.ok = ok
        self.calls = 0
        self._real = FixtureProvider()

    def fetch_daily(self, **kwargs):
        self.calls += 1
        if self.calls <= self.ok:
            return self._real.fetch_daily(**kwargs)
        raise ProviderError("synthetic upstream failure")

    def close(self) -> None:
        self._real.close()


def _counts(session) -> dict[str, int]:
    return {
        "events": session.scalar(select(func.count()).select_from(AnomalyEvent)),
        "rankings": session.scalar(select(func.count()).select_from(DailyRanking)),
        "observations": session.scalar(select(func.count()).select_from(WeatherObservation)),
        "explanations": session.scalar(select(func.count()).select_from(AgentExplanation)),
    }


# ---------------------------------------------------------------------------
# Date resolution
# ---------------------------------------------------------------------------


def test_analysis_date_is_the_last_day_complete_in_every_city(world, test_settings):
    """A board must compare one calendar day, so the earliest city wins."""
    cities = world.execute(select(City)).scalars().all()
    resolved = resolve_analysis_date(cities, test_settings, now_utc=NOW)

    from zoneinfo import ZoneInfo

    from app.stats.seasonal import latest_eligible_local_date

    per_city = {
        c.id: latest_eligible_local_date(
            ZoneInfo(c.timezone), NOW, test_settings.pipeline_source_lag_hours
        )
        for c in cities
    }
    assert resolved == min(per_city.values())
    assert all(resolved <= d for d in per_city.values())


def test_resolving_a_date_without_a_registry_is_a_clear_error(session, test_settings):
    with pytest.raises(PipelineError, match="seed-cities"):
        resolve_analysis_date([], test_settings, now_utc=NOW)


def test_a_date_whose_local_day_is_still_open_is_skipped_not_invented(
    pipeline, test_settings
):
    """Asking for today must not fabricate a partial day's numbers."""
    report = pipeline.run_daily(NOW.date(), now_utc=NOW)

    assert report.status == RunStatus.FAILED.value
    assert report.published is False
    assert report.cities_with_data == 0
    assert "completeness" in report.error


# ---------------------------------------------------------------------------
# A successful run
# ---------------------------------------------------------------------------


def test_a_daily_run_publishes_a_board_with_full_provenance(pipeline, world):
    report = pipeline.run_daily(ANALYSIS_DATE, now_utc=NOW)

    assert report.status == RunStatus.SUCCEEDED.value, report.error
    assert report.published is True
    assert report.analysis_date == ANALYSIS_DATE
    assert report.cities_with_data == len(CITY_SPECS)
    assert report.completeness == pytest.approx(1.0)
    assert report.data_tier == DataTier.PROVISIONAL.value  # 2 days old
    assert 1 <= report.events_published <= 10

    run = world.get(PipelineRun, report.run_id)
    assert run.published is True
    assert run.published_at is not None
    assert run.registry_version == "test"
    assert run.config_snapshot["reference_period"] == "1991-2020"
    # The snapshot exists to make a run reproducible, not to leak configuration.
    assert not any("key" in k or "token" in k for k in run.config_snapshot)


def _ranked_city_ids(session) -> list[str]:
    return [
        event.city_id
        for event in session.execute(
            select(AnomalyEvent)
            .join(DailyRanking, DailyRanking.event_id == AnomalyEvent.id)
            .order_by(DailyRanking.rank)
        )
        .scalars()
        .all()
    ]


def test_no_city_can_dominate_a_board_that_has_more_cities_than_slots(
    world, test_settings
):
    """The anti-domination rule, in the regime the production registry is in.

    With fifty cities and ten slots the first pass always fills the board with
    distinct cities, so this is the case that matters. It is forced here by
    shrinking the board rather than by growing the registry.
    """
    settings = test_settings.model_copy(update={"ranking_top_n": 2})
    pipeline = Pipeline(world, settings, FixtureProvider())
    report = pipeline.run_daily(ANALYSIS_DATE, now_utc=NOW)
    pipeline.close()

    city_ids = _ranked_city_ids(world)
    assert len(city_ids) == 2
    assert len(set(city_ids)) == 2
    # Every candidate is still stored, including the ones that missed the board.
    assert report.events_total > report.events_published


def test_a_board_short_of_cities_is_backfilled_rather_than_left_thin(pipeline, world):
    """The documented second pass, exercised by a three-city test registry.

    When distinct cities run out before the slots do, the board is topped up with
    the next strongest events regardless of city. Three cities cannot fill ten
    slots, so repeats here are the intended behaviour — and every city that had an
    eligible event is still represented.
    """
    report = pipeline.run_daily(ANALYSIS_DATE, now_utc=NOW)

    city_ids = _ranked_city_ids(world)
    assert len(city_ids) == report.events_published <= 10
    assert set(city_ids) == {spec[0] for spec in CITY_SPECS}
    assert report.events_total >= report.events_published


def test_ranks_are_a_dense_sequence_ordered_by_descending_score(pipeline, world):
    pipeline.run_daily(ANALYSIS_DATE, now_utc=NOW)

    rows = (
        world.execute(
            select(DailyRanking.rank, DailyRanking.score).order_by(DailyRanking.rank)
        )
        .tuples()
        .all()
    )
    assert [r for r, _ in rows] == list(range(1, len(rows) + 1))
    scores = [s for _, s in rows]
    assert scores == sorted(scores, reverse=True)


def test_every_ranked_event_gets_a_deterministic_explanation_without_an_llm(
    pipeline, world, test_settings
):
    """The no-key fallback is the default path, not a degraded one."""
    assert test_settings.llm_enabled is False
    report = pipeline.run_daily(ANALYSIS_DATE, now_utc=NOW)

    assert report.llm_calls == 0
    assert report.llm_estimated_usd == 0.0
    assert report.template_generated == report.events_published

    explanations = world.execute(select(AgentExplanation)).scalars().all()
    assert len(explanations) == report.events_published
    for row in explanations:
        assert row.generator == "template"
        assert row.headline
        assert row.evidence
        assert row.run_id == report.run_id


def test_skipping_explanations_still_publishes_a_board(pipeline, world):
    report = pipeline.run_daily(ANALYSIS_DATE, now_utc=NOW, skip_explanations=True)

    assert report.published is True
    assert world.scalar(select(func.count()).select_from(AgentExplanation)) == 0


def test_stored_observations_are_labelled_synthetic_not_observed(pipeline, world):
    """The fixture provider must never be presentable as a station reading."""
    pipeline.run_daily(ANALYSIS_DATE, now_utc=NOW)

    for obs in world.execute(select(WeatherObservation)).scalars().all():
        assert obs.source_dataset == "synthetic_fixture_v1"
        assert obs.observation_type != "observation"
        assert obs.data_tier == DataTier.PROVISIONAL.value


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_rerunning_the_same_date_is_idempotent(pipeline, world):
    """The guarantee that makes retries safe and backfills repeatable."""
    first = pipeline.run_daily(ANALYSIS_DATE, now_utc=NOW)
    before = _counts(world)

    second = pipeline.run_daily(ANALYSIS_DATE, now_utc=NOW)
    after = _counts(world)

    assert second.status == RunStatus.SUCCEEDED.value, second.error
    assert second.run_id != first.run_id
    assert after == before
    assert second.events_published == first.events_published

    # One board for the day, and it belongs to the newer run.
    ranks = world.execute(
        select(DailyRanking.rank).where(DailyRanking.analysis_date == ANALYSIS_DATE)
    ).scalars().all()
    assert sorted(ranks) == list(range(1, len(ranks) + 1))
    assert all(
        r.run_id == second.run_id
        for r in world.execute(select(DailyRanking)).scalars().all()
    )


def test_a_rerun_publishes_exactly_one_run_for_the_date(pipeline, world):
    first = pipeline.run_daily(ANALYSIS_DATE, now_utc=NOW)
    second = pipeline.run_daily(ANALYSIS_DATE, now_utc=NOW)

    published = (
        world.execute(
            select(PipelineRun).where(
                PipelineRun.analysis_date == ANALYSIS_DATE,
                PipelineRun.published.is_(True),
            )
        )
        .scalars()
        .all()
    )
    assert [r.id for r in published] == [second.run_id]
    assert world.get(PipelineRun, first.run_id).status == RunStatus.SUCCEEDED.value


def test_event_ids_are_stable_across_reruns(pipeline, world):
    pipeline.run_daily(ANALYSIS_DATE, now_utc=NOW)
    first_ids = set(world.execute(select(AnomalyEvent.id)).scalars().all())

    pipeline.run_daily(ANALYSIS_DATE, now_utc=NOW)
    second_ids = set(world.execute(select(AnomalyEvent.id)).scalars().all())

    assert first_ids == second_ids
    for event_id in first_ids:
        assert event_id.endswith("@1.0.0")
        assert event_id.startswith(ANALYSIS_DATE.isoformat())


# ---------------------------------------------------------------------------
# Failure containment
# ---------------------------------------------------------------------------


def test_a_failed_run_leaves_the_published_board_untouched(world, test_settings):
    """The central operational guarantee of the whole project."""
    good = Pipeline(world, test_settings, FixtureProvider())
    published = good.run_daily(ANALYSIS_DATE, now_utc=NOW)
    good.close()
    assert published.published is True
    board_before = _board(world)

    broken = Pipeline(world, test_settings, BrokenProvider())
    failed = broken.run_daily(ANALYSIS_DATE + timedelta(days=1), now_utc=NOW + timedelta(days=1))
    broken.close()

    assert failed.status == RunStatus.FAILED.value
    assert failed.published is False
    assert failed.provider_errors == len(CITY_SPECS)

    run = world.get(PipelineRun, published.run_id)
    assert run.published is True
    assert run.status == RunStatus.SUCCEEDED.value
    assert _board(world) == board_before

    failed_run = world.get(PipelineRun, failed.run_id)
    assert failed_run.published is False
    assert failed_run.error
    assert failed_run.error_type


def test_the_api_keeps_serving_the_last_good_board_after_a_failure(
    client, world, test_settings
):
    """End to end: pipeline failure, then a real HTTP request."""
    good = Pipeline(world, test_settings, FixtureProvider())
    good.run_daily(ANALYSIS_DATE, now_utc=NOW)
    good.close()

    broken = Pipeline(world, test_settings, BrokenProvider())
    broken.run_daily(ANALYSIS_DATE + timedelta(days=1), now_utc=NOW + timedelta(days=1))
    broken.close()

    latest = client.get("/api/rankings/latest").json()
    assert latest["analysis_date"] == ANALYSIS_DATE.isoformat()
    assert latest["count"] >= 1

    asked_for = (ANALYSIS_DATE + timedelta(days=1)).isoformat()
    stale = client.get(f"/api/rankings/{asked_for}").json()
    assert stale["is_latest_available"] is False
    assert stale["requested_date"] == asked_for
    assert stale["analysis_date"] == ANALYSIS_DATE.isoformat()


def test_a_thin_day_is_refused_rather_than_published(world, test_settings):
    """Publishing a board built from one city in three would change the question."""
    pipeline = Pipeline(world, test_settings, BrokenProvider(ok=1))
    report = pipeline.run_daily(ANALYSIS_DATE, now_utc=NOW)
    pipeline.close()

    assert report.status == RunStatus.FAILED.value
    assert report.cities_with_data == 1
    assert report.completeness == pytest.approx(1 / 3)
    assert "below the required" in report.error
    assert world.scalar(select(func.count()).select_from(DailyRanking)) == 0
    # The one successful fetch is still stored: the data is fine, the board is not.
    assert world.scalar(select(func.count()).select_from(WeatherObservation)) == 1


def test_a_run_without_baselines_says_so(session, test_settings):
    """The most likely first-deploy mistake gets a fixable message."""
    for spec in CITY_SPECS:
        make_city(session, city_id=spec[0], name=spec[1], timezone=spec[7])

    pipeline = Pipeline(session, test_settings, FixtureProvider())
    report = pipeline.run_daily(ANALYSIS_DATE, now_utc=NOW)
    pipeline.close()

    assert report.status == RunStatus.FAILED.value
    assert "build-baselines" in report.error


def _board(session) -> list[tuple[int, str, float]]:
    return (
        session.execute(
            select(DailyRanking.rank, DailyRanking.event_id, DailyRanking.score)
            .order_by(DailyRanking.rank)
        )
        .tuples()
        .all()
    )


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------


def test_building_baselines_is_skipped_on_the_second_pass(session, test_settings):
    """Thirty years per city is not something a daily job should redo."""
    make_city(session, city_id=CITY_SPECS[0][0], name="Phoenix")
    provider = FixtureProvider()
    pipeline = Pipeline(session, test_settings, provider)

    first = pipeline.build_baselines()
    assert first.status == RunStatus.SUCCEEDED.value
    assert first.cities_with_data == 1
    requests_after_first = provider.request_count
    rows = session.scalar(select(func.count()).select_from(BaselineStatistic))
    assert rows == 365 * 5

    second = pipeline.build_baselines()
    pipeline.close()

    assert second.cities_with_data == 0  # nothing rebuilt
    assert provider.request_count == requests_after_first  # nothing refetched
    assert session.scalar(select(func.count()).select_from(BaselineStatistic)) == rows


def test_baselines_record_their_reference_period_and_are_not_called_normals(
    session, test_settings
):
    make_city(session, city_id=CITY_SPECS[0][0], name="Phoenix")
    pipeline = Pipeline(session, test_settings, FixtureProvider())
    pipeline.build_baselines()
    pipeline.close()

    row = session.execute(select(BaselineStatistic).limit(1)).scalars().first()
    assert row.reference_start_year == 1991
    assert row.reference_end_year == 2020
    assert row.window_days == 7
    assert row.source_dataset == "synthetic_fixture_v1"
    assert row.n_years >= 29


def test_an_unknown_city_id_is_rejected_before_any_fetching(session, test_settings):
    make_city(session, city_id=CITY_SPECS[0][0], name="Phoenix")
    provider = FixtureProvider()
    pipeline = Pipeline(session, test_settings, provider)
    report = pipeline.build_baselines(city_ids=["atlantis"])
    pipeline.close()

    assert report.status == RunStatus.FAILED.value
    assert "atlantis" in report.error
    assert provider.request_count == 0


# ---------------------------------------------------------------------------
# Backfill and finalize
# ---------------------------------------------------------------------------


def test_backfill_publishes_each_date_independently(world, test_settings):
    start = ANALYSIS_DATE - timedelta(days=2)
    reports = backfill(
        world, start, ANALYSIS_DATE, test_settings, FixtureProvider()
    )

    assert [r.analysis_date for r in reports] == [
        start,
        start + timedelta(days=1),
        ANALYSIS_DATE,
    ]
    assert all(r.published for r in reports), [r.error for r in reports]

    published = world.execute(
        select(PipelineRun.analysis_date)
        .where(PipelineRun.published.is_(True))
        .order_by(PipelineRun.analysis_date)
    ).scalars().all()
    assert published == [start, start + timedelta(days=1), ANALYSIS_DATE]


def test_backfill_rejects_an_inverted_range_before_doing_any_work(world, test_settings):
    with pytest.raises(PipelineError, match="before start"):
        backfill(world, ANALYSIS_DATE, ANALYSIS_DATE - timedelta(days=3), test_settings)


def test_finalize_upgrades_a_provisional_day_to_the_settled_reanalysis(
    world, test_settings
):
    pipeline = Pipeline(world, test_settings, FixtureProvider())
    provisional = pipeline.run_daily(ANALYSIS_DATE, now_utc=NOW)
    pipeline.close()
    assert provisional.data_tier == DataTier.PROVISIONAL.value

    later = NOW + timedelta(days=8)
    reports = finalize(world, test_settings, FixtureProvider(), now_utc=later)

    assert len(reports) == 1
    report = reports[0]
    assert report.kind == RunKind.FINALIZE.value
    assert report.data_tier == DataTier.FINAL.value
    assert report.analysis_date == ANALYSIS_DATE
    assert report.published is True

    run = world.get(PipelineRun, report.run_id)
    assert run.data_tier == DataTier.FINAL.value
    assert world.get(PipelineRun, provisional.run_id).published is False


def test_finalize_does_nothing_when_every_published_day_is_already_final(
    world, test_settings
):
    pipeline = Pipeline(
        world, test_settings, FixtureProvider()
    )
    pipeline.run_daily(ANALYSIS_DATE, force_tier=DataTier.FINAL, now_utc=NOW)
    pipeline.close()

    assert finalize(world, test_settings, FixtureProvider(), now_utc=NOW + timedelta(days=8)) == []


def test_finalize_ignores_days_whose_archive_has_not_caught_up(world, test_settings):
    """Re-running a provisional day too early would just refetch the same estimate."""
    pipeline = Pipeline(world, test_settings, FixtureProvider())
    pipeline.run_daily(ANALYSIS_DATE, now_utc=NOW)
    pipeline.close()

    assert finalize(world, test_settings, FixtureProvider(), now_utc=NOW) == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_run_exits_zero_on_a_published_board(world, app_db, capsys, monkeypatch):
    from app.pipeline import cli

    monkeypatch.setattr(cli, "dispose_engine", lambda: None)
    code = cli.main(["run", "--date", ANALYSIS_DATE.isoformat(), "--skip-explanations"])

    assert code == 0
    out = capsys.readouterr().out
    assert "published=True" in out
    assert RunStatus.SUCCEEDED.value in out


def test_cli_run_exits_nonzero_when_the_run_fails(world, app_db, capsys, monkeypatch):
    """The exit code is the contract a scheduler reads."""
    from app.pipeline import cli

    monkeypatch.setattr(cli, "dispose_engine", lambda: None)
    # A date whose local day is still open in every city: nothing to analyse.
    code = cli.main(["run", "--date", "2030-01-01"])

    assert code == 1
    assert "published=False" in capsys.readouterr().out


def test_cli_rejects_a_malformed_date_as_a_usage_error(capsys):
    from app.pipeline import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--date", "21-09-2026"])
    assert exc.value.code == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_cli_seed_cities_loads_the_version_controlled_registry(
    session, app_db, capsys, monkeypatch
):
    from app.pipeline import cli

    monkeypatch.setattr(cli, "dispose_engine", lambda: None)
    assert cli.main(["seed-cities"]) == 0

    out = capsys.readouterr().out
    count = session.scalar(select(func.count()).select_from(City))
    assert count == 50
    assert f"{count} in file" in out

    # Idempotent: a second pass changes nothing.
    assert cli.main(["seed-cities"]) == 0
    assert "0 inserted" in capsys.readouterr().out
    assert session.scalar(select(func.count()).select_from(City)) == count


def test_cli_status_reports_configuration_and_recent_runs(
    world, app_db, capsys, monkeypatch, test_settings
):
    from app.pipeline import cli

    monkeypatch.setattr(cli, "dispose_engine", lambda: None)
    pipeline = Pipeline(world, test_settings, FixtureProvider())
    pipeline.run_daily(ANALYSIS_DATE, now_utc=NOW, skip_explanations=True)
    pipeline.close()

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "environment:  test" in out
    assert "weather:      fixture" in out
    assert "enabled=False" in out
    assert ANALYSIS_DATE.isoformat() in out
    assert "published" in out


def test_cli_status_tells_a_fresh_deployment_what_to_do_next(
    session, app_db, capsys, monkeypatch
):
    from app.pipeline import cli

    monkeypatch.setattr(cli, "dispose_engine", lambda: None)
    assert cli.main(["status"]) == 0
    assert "seed-cities" in capsys.readouterr().out


def test_every_documented_subcommand_is_wired_up():
    """The README and the cron config name these; a rename must break a test."""
    from app.pipeline import cli

    assert set(cli.HANDLERS) == {
        "run",
        "backfill",
        "finalize",
        "build-baselines",
        "seed-cities",
        "status",
    }
    parser = cli.build_parser()
    for command in cli.HANDLERS:
        assert command in parser.format_help()


def test_get_settings_is_not_reread_per_city(pipeline):
    """A run must compute every city under one configuration."""
    assert pipeline.settings is get_settings()
