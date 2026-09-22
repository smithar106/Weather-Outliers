"""A disposable world the suites can run a real pipeline against.

The reproducibility and API suites need a populated database: cities, a cached
baseline climatology, a scored and published board, and explanations. Building
one takes a few seconds, so it is built once per harness run and shared.

Everything is synthetic and labelled as such. The cities, their coordinates and
their timezones are the real registry — a subset of it, for speed — but every
weather value comes from ``FixtureProvider``, which records
``source_dataset="synthetic_fixture_v1"``. The suites therefore measure whether
the machinery is correct and reproducible; they say nothing about the accuracy of
real weather data, and the report states that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.cities.registry import load_registry, sync_registry
from app.config import Settings, get_settings
from app.models import Base, City
from app.pipeline.runner import Pipeline, RunReport
from app.providers.fixture import FixtureProvider

#: How many cities the scratch world holds. Ten is the smallest number that still
#: fills a ten-slot board with ten distinct cities, so the one-event-per-city rule
#: is exercised rather than bypassed by a short board.
EVAL_CITY_COUNT = 10

#: A fixed analysis date, so two harness runs on different days produce
#: comparable reports. It is in the past relative to any plausible run, which
#: also means the pipeline treats it as settled reanalysis rather than
#: provisional data.
EVAL_ANALYSIS_DATE = date(2025, 7, 15)

#: The wall-clock instant the pipeline is told it is running at. Pinned for the
#: same reason as the date: a harness whose results drift with the calendar
#: cannot be compared against last week's report.
EVAL_NOW_UTC = datetime(2025, 8, 1, 12, 0, tzinfo=UTC)


def eval_settings() -> Settings:
    """Configuration for the scratch world, derived from the real defaults."""
    return get_settings()


def city_subset() -> list[str]:
    """A deterministic, timezone-spread slice of the real registry.

    Evenly sampling the file rather than taking the first N keeps several
    timezones and all three countries in play, which matters because the analysis
    date is the most recent day complete in *every* city.
    """
    registry = load_registry()
    cities = sorted(registry.cities, key=lambda c: c.id)
    if len(cities) <= EVAL_CITY_COUNT:
        return [c.id for c in cities]
    step = len(cities) / EVAL_CITY_COUNT
    return [cities[int(i * step)].id for i in range(EVAL_CITY_COUNT)]


def new_engine() -> Engine:
    """A fresh in-memory database.

    ``StaticPool`` pins every connection to the one in-memory database; without it
    the API suite's request thread would open an empty second database.
    """
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_conn, _record):  # pragma: no cover - driver callback
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


@dataclass(slots=True)
class World:
    """A populated scratch database plus the timings taken while populating it."""

    engine: Engine
    session_factory: sessionmaker
    settings: Settings
    city_ids: list[str]
    analysis_date: date
    baseline_ms: int
    baseline_rows: int
    provider_requests: int
    run_report: RunReport | None = None
    run_ms: int = 0

    def session(self) -> Session:
        return self.session_factory()

    def install_as_app_db(self) -> None:
        """Point ``app.db`` at this world.

        ``create_app`` resolves its sessions through ``app.db``, so the API suite
        has to redirect the module globals rather than pass an engine in. The
        redirect is process-wide and never undone, which is acceptable because the
        harness process does nothing else afterwards.
        """
        from app import db as db_module

        db_module._engine = self.engine
        db_module._SessionFactory = self.session_factory

    def dispose(self) -> None:
        self.engine.dispose()


def build_world(*, with_board: bool = True) -> World:
    """Seed cities, cache baselines, and optionally publish one board."""
    import time

    settings = eval_settings()
    engine = new_engine()
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    wanted = city_subset()

    registry = load_registry()
    trimmed = registry.model_copy(
        update={"cities": [c for c in registry.cities if c.id in set(wanted)]}
    )

    with factory() as session:
        sync_registry(session, trimmed)
        session.commit()

        provider = FixtureProvider()
        pipeline = Pipeline(session, settings, provider)

        started = time.perf_counter()
        baseline_report = pipeline.build_baselines()
        session.commit()
        baseline_ms = int((time.perf_counter() - started) * 1000)
        baseline_rows = sum(o.baselines_found for o in baseline_report.city_outcomes)

        run_report: RunReport | None = None
        run_ms = 0
        if with_board:
            started = time.perf_counter()
            run_report = pipeline.run_daily(EVAL_ANALYSIS_DATE, now_utc=EVAL_NOW_UTC)
            run_ms = int((time.perf_counter() - started) * 1000)
        pipeline.close()

        city_ids = list(session.scalars(select(City.id).order_by(City.id)).all())

    return World(
        engine=engine,
        session_factory=factory,
        settings=settings,
        city_ids=city_ids,
        analysis_date=EVAL_ANALYSIS_DATE,
        baseline_ms=baseline_ms,
        baseline_rows=baseline_rows,
        provider_requests=baseline_report.provider_requests,
        run_report=run_report,
        run_ms=run_ms,
    )
