"""SQLAlchemy ORM models.

Design notes
------------
* Column types stay portable (``JSON`` rather than ``JSONB``) so the whole test
  suite runs on SQLite while production runs on PostgreSQL.
* Every table that the pipeline writes carries a natural unique key, which is
  what makes reruns idempotent rather than duplicative.
* Provenance is a first-class citizen: source dataset, observation type, data
  tier, quality flags, and methodology version travel with the numbers all the
  way to the API response.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class City(Base):
    """Curated city registry, synced from ``data/cities.json``."""

    __tablename__ = "cities"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    admin: Mapped[str] = mapped_column(String(128), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False, index=True)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    population: Mapped[int | None] = mapped_column(Integer, nullable=True)
    population_source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    registry_version: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    observations: Mapped[list[WeatherObservation]] = relationship(
        back_populates="city", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_cities_country_region", "country", "region"),)


class WeatherObservation(Base):
    """One city-day of daily aggregates, as retrieved from a provider.

    ``observation_type`` is deliberately explicit: these values are reanalysis or
    model analysis output, NOT direct station readings, and the UI says so.
    """

    __tablename__ = "weather_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    city_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("cities.id", ondelete="CASCADE"), nullable=False
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)

    temp_max_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_min_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_mean_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    precipitation_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_gust_max_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)

    source_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    source_dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    source_endpoint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    observation_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # The provider snaps every request to its model grid. These are the
    # coordinates and elevation actually sampled, which can sit kilometres and
    # hundreds of metres away from the city-centre point we asked for. Recording
    # them is the difference between a defensible number and a plausible one.
    grid_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    grid_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    grid_elevation_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    data_quality: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    missing_fields: Mapped[list | None] = mapped_column(JSON, nullable=True)
    units: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Recorded UTC offset for this city on this local date. Proves the pipeline
    # resolved a real IANA offset (including DST) rather than assuming one.
    utc_offset_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    city: Mapped[City] = relationship(back_populates="observations")

    __table_args__ = (
        # One row per city-day-dataset. Rerunning a date upserts rather than
        # duplicating; the provisional and final datasets coexist side by side.
        UniqueConstraint(
            "city_id", "local_date", "source_dataset", name="uq_obs_city_date_dataset"
        ),
        Index("ix_obs_local_date", "local_date"),
        Index("ix_obs_city_date", "city_id", "local_date"),
        Index("ix_obs_date_tier", "local_date", "data_tier"),
    )


class BaselineStatistic(Base):
    """Cached seasonal climatological distribution for one city/metric/day-of-year.

    The expensive part of this project is the 30-year reference period. It is
    downloaded once, reduced to a compact quantile sketch, and cached here, so
    the daily run never re-downloads decades of history.
    """

    __tablename__ = "baseline_statistics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    city_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("cities.id", ondelete="CASCADE"), nullable=False
    )
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    # 1..365 on a fixed no-leap calendar. See app/stats/seasonal.py for the
    # explicit leap-day rule.
    day_of_year: Mapped[int] = mapped_column(Integer, nullable=False)

    reference_start_year: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_end_year: Mapped[int] = mapped_column(Integer, nullable=False)
    window_days: Mapped[int] = mapped_column(Integer, nullable=False)

    n_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    n_years: Mapped[int] = mapped_column(Integer, nullable=False)
    sufficient: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    std: Mapped[float | None] = mapped_column(Float, nullable=True)
    median: Mapped[float | None] = mapped_column(Float, nullable=True)
    p25: Mapped[float | None] = mapped_column(Float, nullable=True)
    p75: Mapped[float | None] = mapped_column(Float, nullable=True)
    iqr: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_value: Mapped[float | None] = mapped_column(Float, nullable=True)

    # {"grid": [p...], "values": [x...]} on a tail-dense probability grid.
    quantiles: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The 5 smallest / 5 largest sampled values, so the extreme tails keep full
    # resolution that a fixed quantile grid would smear away.
    low_order_stats: Mapped[list | None] = mapped_column(JSON, nullable=True)
    high_order_stats: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Fixed-bin histogram of the seasonal sample, so the city-detail
    # distribution chart renders from a published aggregate instead of querying
    # (or storing) 450 raw values per bucket.
    histogram: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Precipitation-specific: the distribution is a mixture of a point mass at
    # zero and a continuous wet-day distribution.
    zero_fraction: Mapped[float | None] = mapped_column(Float, nullable=True)
    nonzero_n: Mapped[int | None] = mapped_column(Integer, nullable=True)
    nonzero_quantiles: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    source_dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    methodology_version: Mapped[str] = mapped_column(String(16), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "city_id",
            "metric",
            "day_of_year",
            "reference_start_year",
            "reference_end_year",
            "window_days",
            "methodology_version",
            name="uq_baseline_identity",
        ),
        Index("ix_baseline_lookup", "city_id", "metric", "day_of_year"),
    )


class PipelineRun(Base):
    """One execution of the pipeline, with its outcome and cost accounting."""

    __tablename__ = "pipeline_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    analysis_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    data_tier: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Exactly one run per analysis_date may be published at a time. A failed run
    # never gets published, so the previous good ranking stays live.
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    cities_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cities_with_data: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cities_missing: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completeness: Mapped[float | None] = mapped_column(Float, nullable=True)

    events_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    events_published: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    events_excluded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    provider_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_estimated_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    llm_budget_exhausted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    methodology_version: Mapped[str] = mapped_column(String(16), nullable=False)
    registry_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    config_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        Index("ix_runs_date_status", "analysis_date", "status"),
        Index("ix_runs_published", "published", "analysis_date"),
        Index("ix_runs_kind_started", "kind", "started_at"),
    )


class AnomalyEvent(Base):
    """A single city-metric anomaly candidate with its full calculation trace.

    Every number the UI or the AI agent can cite is stored here, which is what
    makes the explanations verifiable rather than asserted.
    """

    __tablename__ = "anomaly_events"

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("pipeline_runs.id", ondelete="SET NULL"), nullable=True
    )
    city_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("cities.id", ondelete="CASCADE"), nullable=False
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)

    observed_value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)

    baseline_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_median: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_std: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_p25: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_p75: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_n: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    baseline_sufficient: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # observed - baseline_mean (temperature) or observed - baseline_median (skewed metrics)
    deviation: Mapped[float | None] = mapped_column(Float, nullable=True)
    # (observed - median) / IQR. Always computable, scale-free, robust to skew.
    robust_deviation: Mapped[float | None] = mapped_column(Float, nullable=True)
    z_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # False whenever the distribution does not justify reading the z-score as a
    # normal-theory probability. The API surfaces this flag.
    z_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    percentile: Mapped[float | None] = mapped_column(Float, nullable=True)
    tail_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 1 / tail_probability expressed in years, using the seasonal window's
    # effective independent-sample count. Reported as approximate.
    return_period_years: Mapped[float | None] = mapped_column(Float, nullable=True)
    tail_probability_is_bounded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    beyond_baseline_sample: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    surprisal: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    margin_bonus: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    anomaly_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    data_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    data_quality: Mapped[str] = mapped_column(String(16), nullable=False)
    source_dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_type: Mapped[str] = mapped_column(String(32), nullable=False)

    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    excluded_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Machine-readable calculation trace. This is exactly what
    # get_anomaly_evidence() hands the AI agent.
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    methodology_version: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    city: Mapped[City] = relationship()
    explanations: Mapped[list[AgentExplanation]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_events_date_score", "local_date", "anomaly_score"),
        Index("ix_events_city_metric_date", "city_id", "metric", "local_date"),
        Index("ix_events_run", "run_id"),
    )


class DailyRanking(Base):
    """The published top-N board for one analysis date and run."""

    __tablename__ = "daily_rankings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False
    )
    analysis_date: Mapped[date] = mapped_column(Date, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    event_id: Mapped[str] = mapped_column(
        String(160), ForeignKey("anomaly_events.id", ondelete="CASCADE"), nullable=False
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)
    methodology_version: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    event: Mapped[AnomalyEvent] = relationship()

    __table_args__ = (
        UniqueConstraint("run_id", "rank", name="uq_ranking_run_rank"),
        UniqueConstraint("run_id", "event_id", name="uq_ranking_run_event"),
        Index("ix_rankings_date_rank", "analysis_date", "rank"),
    )


class AgentExplanation(Base):
    """Grounded natural-language explanation for one event, plus its audit trail."""

    __tablename__ = "agent_explanations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(
        String(160), ForeignKey("anomaly_events.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False
    )

    headline: Mapped[str] = mapped_column(String(200), nullable=False)
    statistical_explanation: Mapped[str] = mapped_column(Text, nullable=False)
    historical_context: Mapped[str] = mapped_column(Text, nullable=False)
    caveats: Mapped[str] = mapped_column(Text, nullable=False)
    # [{label, value, unit, source_tool}] — the numbers the text is allowed to cite.
    evidence: Mapped[list | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")

    generator: Mapped[str] = mapped_column(String(16), nullable=False)
    llm_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)

    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Result of the grounding guards: numeric citation check, record-claim check,
    # causal-claim check. Retained so failures are inspectable after the fact.
    validation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    fallback_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    event: Mapped[AnomalyEvent] = relationship(back_populates="explanations")

    __table_args__ = (
        UniqueConstraint("event_id", "run_id", name="uq_explanation_event_run"),
        Index("ix_explanations_event", "event_id"),
    )
