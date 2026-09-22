"""Shared test fixtures.

The whole suite runs on SQLite. That is a deliberate constraint on the models
rather than a shortcut: the schema uses portable column types (``JSON``, not
``JSONB``) precisely so the tests need no running database, which in turn means
CI needs no service container. The Postgres-specific behaviour that remains —
migrations, connection pooling — is exercised separately against a real server.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import db as db_module
from app.api.deps import reset_limiter
from app.config import Settings, get_settings, reset_settings_cache
from app.domain import (
    METHODOLOGY_VERSION,
    DataQuality,
    DataTier,
    Direction,
    Metric,
    ObservationType,
    RunKind,
    RunStatus,
    build_event_id,
)
from app.ingest.baselines import _make_baseline_row, row_to_baseline
from app.main import create_app
from app.models import (
    AnomalyEvent,
    Base,
    BaselineStatistic,
    City,
    DailyRanking,
    PipelineRun,
    WeatherObservation,
)
from app.stats.anomaly import Baseline, compute_anomaly

TEST_DATABASE_URL = "sqlite+pysqlite:///:memory:"


# ---------------------------------------------------------------------------
# Settings and database
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def test_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Pin every test to a known configuration.

    Autouse because a stray ``ANTHROPIC_API_KEY`` in the developer's shell would
    otherwise make the agent tests reach the network.
    """
    for var in (
        "DATABASE_URL",
        "LLM_PROVIDER",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "WEATHER_PROVIDER",
        "OPEN_METEO_API_KEY",
        "AGENT_USD_PER_MTOK_INPUT",
        "AGENT_USD_PER_MTOK_OUTPUT",
    ):
        monkeypatch.delenv(var, raising=False)

    # Explicit env vars outrank any .env file pydantic-settings would find, so
    # setting these is what keeps a developer's local .env out of the tests.
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("WEATHER_PROVIDER", "fixture")
    monkeypatch.setenv("LLM_PROVIDER", "none")

    reset_settings_cache()
    settings = get_settings()
    yield settings
    reset_settings_cache()


@pytest.fixture
def engine():
    """One in-memory engine per test, with foreign keys enforced.

    ``StaticPool`` is load-bearing, not tuning. An in-memory SQLite database lives
    inside its connection, and the default pool hands a *new* connection to each
    thread — so a sync FastAPI handler, which Starlette runs on a worker thread,
    would open an empty database and fail on every table. Pinning the pool to one
    shared connection is what makes the API tests see the seeded rows.
    """
    eng = create_engine(
        TEST_DATABASE_URL,
        future=True,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng, "connect")
    def _fk_pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        yield s


@pytest.fixture
def app_db(engine, monkeypatch: pytest.MonkeyPatch):
    """Point ``app.db`` at the test engine, for code that calls ``session_scope()``."""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_module, "_engine", engine, raising=False)
    monkeypatch.setattr(db_module, "_SessionFactory", factory, raising=False)
    return factory


@pytest.fixture
def client(app_db, test_settings) -> Iterator[TestClient]:
    """A test client over a freshly built app sharing the test engine.

    The limiter is a process-global, so it is reset on both sides: otherwise one
    test's traffic counts against the next one's window and the failures land in
    whichever test happens to run last.
    """
    reset_limiter()
    app = create_app(test_settings)
    with TestClient(app) as c:
        yield c
    reset_limiter()


# ---------------------------------------------------------------------------
# Object factories
# ---------------------------------------------------------------------------


def make_city(
    session: Session,
    *,
    city_id: str = "us-phoenix-az",
    name: str = "Phoenix",
    admin: str = "Arizona",
    country: str = "US",
    region: str = "US West",
    latitude: float = 33.4484,
    longitude: float = -112.074,
    timezone: str = "America/Phoenix",
    population: int | None = 1650070,
) -> City:
    city = City(
        id=city_id,
        name=name,
        admin=admin,
        country=country,
        region=region,
        latitude=latitude,
        longitude=longitude,
        timezone=timezone,
        population=population,
        population_source="test fixture",
        registry_version="test",
        is_active=True,
    )
    session.add(city)
    session.commit()
    return city


def make_run(
    session: Session,
    *,
    run_id: str = "run-test-1",
    analysis_date: date = date(2026, 9, 21),
    published: bool = True,
    status: str = RunStatus.SUCCEEDED.value,
    kind: str = RunKind.DAILY.value,
    started_at: datetime | None = None,
    llm_calls: int = 0,
    llm_estimated_usd: float = 0.0,
) -> PipelineRun:
    run = PipelineRun(
        id=run_id,
        kind=kind,
        analysis_date=analysis_date,
        status=status,
        data_tier=DataTier.FINAL.value,
        published=published,
        published_at=datetime.now(UTC) if published else None,
        started_at=started_at or datetime.now(UTC),
        methodology_version=METHODOLOGY_VERSION,
        registry_version="test",
        llm_calls=llm_calls,
        llm_estimated_usd=llm_estimated_usd,
    )
    session.add(run)
    session.commit()
    return run


def make_observation(
    session: Session,
    city: City,
    *,
    local_date: date = date(2026, 9, 21),
    temp_max_c: float | None = 31.8,
    temp_min_c: float | None = 19.4,
    temp_mean_c: float | None = 25.6,
    precipitation_mm: float | None = 0.0,
    wind_gust_max_kmh: float | None = 38.0,
    data_tier: str = DataTier.FINAL.value,
    data_quality: str = DataQuality.OK.value,
    source_dataset: str = "era5_seamless",
    observation_type: str = ObservationType.REANALYSIS.value,
) -> WeatherObservation:
    obs = WeatherObservation(
        city_id=city.id,
        local_date=local_date,
        temp_max_c=temp_max_c,
        temp_min_c=temp_min_c,
        temp_mean_c=temp_mean_c,
        precipitation_mm=precipitation_mm,
        wind_gust_max_kmh=wind_gust_max_kmh,
        source_provider="open_meteo",
        source_dataset=source_dataset,
        source_endpoint="https://archive-api.open-meteo.com/v1/archive",
        observation_type=observation_type,
        grid_latitude=city.latitude,
        grid_longitude=city.longitude,
        grid_elevation_m=331.0,
        data_tier=data_tier,
        data_quality=data_quality,
        units={"temperature": "°C", "precipitation": "mm", "wind": "km/h"},
        utc_offset_seconds=-25200,
    )
    session.add(obs)
    session.commit()
    return obs


def synthetic_temperatures(
    *,
    n: int = 450,
    centre: float = 22.0,
    spread: float = 3.0,
    seed: int = 7,
) -> list[float]:
    """A reproducible, roughly bell-shaped sample without importing numpy.

    A linear congruential generator plus a 12-uniform sum is the classic
    Irwin-Hall trick: cheap, deterministic across platforms, and close enough to
    normal for tests about the *plumbing* rather than about distributional fit.
    """
    state = seed
    values: list[float] = []
    for _ in range(n):
        total = 0.0
        for _ in range(12):
            state = (1103515245 * state + 12345) % (2**31)
            total += state / (2**31)
        values.append(centre + (total - 6.0) * spread)
    return values


def make_baseline_row(
    session: Session,
    city: City,
    *,
    metric: Metric = Metric.TEMP_MAX,
    day_of_year: int = 264,
    values: list[float] | None = None,
    window_days: int = 7,
    sufficient: bool = True,
    source_dataset: str = "era5_seamless",
) -> BaselineStatistic:
    """Persist a baseline row built by the production code path.

    ``_make_baseline_row`` is private, and using it here is deliberate: a
    hand-rolled copy of the serialisation would drift from what
    ``row_to_baseline`` expects to read back, and the tests would stop proving
    anything about the real round trip.
    """
    values = values if values is not None else synthetic_temperatures()
    row = _make_baseline_row(
        city_id=city.id,
        metric=metric,
        target_doy=day_of_year,
        sample=values,
        n_years=30,
        settings=get_settings(),
        dataset=source_dataset,
    )
    if not sufficient:
        row.sufficient = False
    session.add(row)
    session.commit()
    return row


def baseline_from_values(
    values: list[float],
    *,
    metric: Metric = Metric.TEMP_MAX,
    day_of_year: int = 264,
    sufficient: bool = True,
    source_dataset: str = "era5_seamless",
) -> Baseline:
    """An in-memory :class:`Baseline`, built and rehydrated exactly as production does."""
    row = _make_baseline_row(
        city_id="unused",
        metric=metric,
        target_doy=day_of_year,
        sample=values,
        n_years=30,
        settings=get_settings(),
        dataset=source_dataset,
    )
    if not sufficient:
        row.sufficient = False
    return row_to_baseline(row)


def make_event(
    session: Session,
    city: City,
    *,
    run: PipelineRun | None = None,
    local_date: date = date(2026, 9, 21),
    metric: Metric = Metric.TEMP_MAX,
    observed_value: float = 41.6,
    values: list[float] | None = None,
    rank: int | None = 1,
    data_tier: str = DataTier.FINAL.value,
    data_quality: str = DataQuality.OK.value,
    observation_type: str = ObservationType.REANALYSIS.value,
) -> AnomalyEvent:
    """Create an event whose numbers come from the real anomaly engine.

    Hand-written evidence dicts drift from the calculation they claim to
    describe. Running ``compute_anomaly`` here means the agent tests are
    grounded in the same arithmetic production uses.
    """
    baseline = baseline_from_values(
        values if values is not None else synthetic_temperatures(),
        metric=metric,
        day_of_year=local_date.timetuple().tm_yday,
    )
    candidate = compute_anomaly(
        city_id=city.id,
        local_date=local_date.isoformat(),
        metric=metric,
        observed_value=observed_value,
        baseline=baseline,
    )
    assert candidate is not None

    event = AnomalyEvent(
        id=build_event_id(local_date.isoformat(), city.id, metric),
        run_id=run.id if run is not None else None,
        city_id=city.id,
        local_date=local_date,
        metric=metric.value,
        direction=candidate.direction.value,
        observed_value=candidate.observed_value,
        unit=candidate.unit,
        baseline_mean=candidate.baseline_mean,
        baseline_median=candidate.baseline_median,
        baseline_std=candidate.baseline_std,
        baseline_p25=candidate.baseline_p25,
        baseline_p75=candidate.baseline_p75,
        baseline_min=candidate.baseline_min,
        baseline_max=candidate.baseline_max,
        baseline_n=candidate.baseline_n,
        baseline_sufficient=candidate.baseline_sufficient,
        deviation=candidate.deviation,
        robust_deviation=candidate.robust_deviation,
        z_score=candidate.z_score,
        z_valid=candidate.z_valid,
        percentile=candidate.percentile,
        tail_probability=candidate.tail_probability,
        return_period_years=candidate.return_period_years,
        tail_probability_is_bounded=candidate.tail_probability_is_bounded,
        beyond_baseline_sample=candidate.beyond_baseline_sample,
        surprisal=candidate.surprisal,
        margin_bonus=candidate.margin_bonus,
        anomaly_score=candidate.anomaly_score,
        data_tier=data_tier,
        data_quality=data_quality,
        source_dataset="era5_seamless",
        observation_type=observation_type,
        eligible=candidate.eligible,
        excluded_reason=candidate.excluded_reason,
        evidence=candidate.evidence,
        methodology_version=METHODOLOGY_VERSION,
    )
    session.add(event)
    session.commit()

    if run is not None and rank is not None:
        session.add(
            DailyRanking(
                run_id=run.id,
                analysis_date=local_date,
                rank=rank,
                event_id=event.id,
                score=event.anomaly_score,
                methodology_version=METHODOLOGY_VERSION,
            )
        )
        session.commit()
    return event


@pytest.fixture
def seeded(session: Session):
    """A published run with one city, one observation, one ranked event."""
    city = make_city(session)
    run = make_run(session)
    make_observation(session, city)
    make_baseline_row(session, city)
    event = make_event(session, city, run=run, rank=1)
    return {"session": session, "city": city, "run": run, "event": event}


# Re-export for convenience in test modules.
__all__ = [
    "Direction",
    "Metric",
    "baseline_from_values",
    "make_baseline_row",
    "make_city",
    "make_event",
    "make_observation",
    "make_run",
    "synthetic_temperatures",
]
