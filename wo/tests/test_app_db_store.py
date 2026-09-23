"""The application-database store against a real in-memory SQLite database.

The store reuses the application's own ``baseline_coverage`` and ORM models, so
these tests build the same rows production writes and assert the store maps them
into :mod:`wo.models` correctly — and degrades gracefully when the database is
empty.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from wo.stores.app_db import AppDbStore


def _make_engine():
    from app.models import Base

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _store(engine):
    return AppDbStore(engine=engine)


def _city(session):
    from app.models import City

    city = City(
        id="us-phoenix-az",
        name="Phoenix",
        admin="Arizona",
        country="US",
        region="US West",
        latitude=33.4,
        longitude=-112.1,
        timezone="America/Phoenix",
        registry_version="test",
    )
    session.add(city)
    session.commit()
    return city


def _baseline(session, city, *, sufficient=True, day_of_year=264):
    from app.config import get_settings
    from app.domain import METHODOLOGY_VERSION
    from app.models import BaselineStatistic

    settings = get_settings()
    row = BaselineStatistic(
        city_id=city.id,
        metric="temp_max",
        day_of_year=day_of_year,
        reference_start_year=settings.baseline_start_year,
        reference_end_year=settings.baseline_end_year,
        window_days=settings.baseline_seasonal_window_days,
        n_samples=120,
        n_years=20,
        sufficient=sufficient,
        methodology_version=METHODOLOGY_VERSION,
        source_dataset="era5_seamless",
    )
    session.add(row)
    session.commit()
    return row


def _run(session, run_id="run-1", *, status="succeeded", published=True, started_at=None):
    from app.domain import METHODOLOGY_VERSION
    from app.models import PipelineRun

    run = PipelineRun(
        id=run_id,
        kind="daily",
        analysis_date=date(2026, 9, 21),
        status=status,
        published=published,
        started_at=started_at or datetime(2026, 9, 22, 9, 30, tzinfo=UTC),
        methodology_version=METHODOLOGY_VERSION,
        llm_calls=2,
        llm_estimated_usd=0.0,
    )
    session.add(run)
    session.commit()
    return run


def _explanation(session, city, run, *, generator="llm", event_id="evt-1"):
    from app.models import AgentExplanation, AnomalyEvent

    event = AnomalyEvent(
        id=event_id,
        run_id=run.id,
        city_id=city.id,
        local_date=date(2026, 9, 21),
        metric="temp_max",
        direction="high",
        observed_value=41.6,
        unit="C",
        data_tier="final",
        data_quality="ok",
        source_dataset="era5_seamless",
        observation_type="reanalysis",
        methodology_version="1.0.0",
    )
    session.add(event)
    session.commit()

    explanation = AgentExplanation(
        event_id=event.id,
        run_id=run.id,
        headline="Unusually hot day in Phoenix",
        statistical_explanation="The value sat beyond the reference sample.",
        historical_context="Rare for this time of year.",
        caveats="Gridded reanalysis estimate.",
        generator=generator,
    )
    session.add(explanation)
    session.commit()
    return explanation


def test_coverage_is_empty_on_an_empty_database():
    engine = _make_engine()
    coverage = _store(engine).coverage()
    assert coverage.cities_with_baselines == 0
    assert coverage.rows == 0
    assert coverage.sufficient_rows == 0


def test_coverage_counts_baseline_rows():
    engine = _make_engine()
    factory = _factory(engine)
    with factory() as session:
        city = _city(session)
        _baseline(session, city, sufficient=True, day_of_year=100)
        _baseline(session, city, sufficient=False, day_of_year=200)

    coverage = _store(engine).coverage()
    assert coverage.cities_with_baselines == 1
    assert coverage.rows == 2
    assert coverage.sufficient_rows == 1


def test_recent_runs_is_empty_on_an_empty_database():
    engine = _make_engine()
    assert _store(engine).recent_runs(5) == []


def test_recent_runs_orders_newest_first_and_maps_fields():
    engine = _make_engine()
    factory = _factory(engine)
    with factory() as session:
        _run(
            session,
            "run-old",
            published=False,
            started_at=datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
        )
        _run(
            session,
            "run-new",
            status="failed",
            started_at=datetime(2026, 9, 22, 10, 0, tzinfo=UTC),
        )

    runs = _store(engine).recent_runs(5)
    assert [r.run_id for r in runs] == ["run-new", "run-old"]
    assert runs[0].status == "failed"
    assert runs[0].analysis_date == "2026-09-21"
    assert runs[1].published is False


def test_explanation_mix_empty_ids():
    engine = _make_engine()
    assert _store(engine).explanation_mix([]) == {}


def test_explanation_mix_counts_generators():
    engine = _make_engine()
    factory = _factory(engine)
    with factory() as session:
        city = _city(session)
        run = _run(session)
        _explanation(session, city, run, generator="llm", event_id="evt-1")
        _explanation(session, city, run, generator="template", event_id="evt-2")

    mix = _store(engine).explanation_mix([run.id])
    assert mix == {"llm": 1, "template": 1}


def test_pricing_is_not_configured_by_default():
    engine = _make_engine()
    assert _store(engine).pricing_configured() is False
