"""Public API response schemas.

These are the contract, not a mirror of the ORM. Two rules shape them:

**Provenance travels with every number.** A reader looking at 41.6 °C can ask,
from the same payload, what dataset it came from, whether it is a reanalysis
estimate or a station reading, how settled it is, and which methodology version
scored it. Nothing here can be quoted without its caveats attached, because the
caveats are in the same object.

**Nothing is renamed on the way out.** ``tail_probability_is_bounded`` is called
that in the database, in the agent's tools, and here. A field that changes name
between layers is a field somebody will eventually misread.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ---------------------------------------------------------------------------
# Cities
# ---------------------------------------------------------------------------


class CityOut(ApiModel):
    id: str
    name: str
    admin: str
    country: str
    region: str
    latitude: float
    longitude: float
    timezone: str
    population: int | None = None
    population_source: str | None = None


class CityListOut(ApiModel):
    registry_version: str
    count: int
    selection_criteria_url: str = "/api/methodology#city-selection"
    cities: list[CityOut]


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


class ObservationOut(ApiModel):
    local_date: date
    temp_max_c: float | None = None
    temp_min_c: float | None = None
    temp_mean_c: float | None = None
    precipitation_mm: float | None = None
    wind_gust_max_kmh: float | None = None

    source_provider: str
    source_dataset: str
    #: ``reanalysis`` / ``model_analysis`` / ``station_observation``. The first two
    #: are model estimates at a grid cell and must never be presented as station
    #: readings.
    observation_type: str
    data_tier: str
    data_quality: str
    missing_fields: list[str] | None = None
    grid_latitude: float | None = None
    grid_longitude: float | None = None
    grid_elevation_m: float | None = None
    utc_offset_seconds: int | None = None
    retrieved_at: datetime


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------


class BaselineOut(ApiModel):
    metric: str
    day_of_year: int
    reference_period: str
    seasonal_window_days: int
    n_samples: int
    n_years: int
    sufficient: bool
    mean: float | None = None
    std: float | None = None
    median: float | None = None
    p25: float | None = None
    p75: float | None = None
    iqr: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    #: Bin counts for the distribution chart. Not a probability density.
    histogram: dict | None = None
    zero_fraction: float | None = None
    nonzero_n: int | None = None
    source_dataset: str
    methodology_version: str
    #: Stated on every baseline payload. These are computed from a reanalysis
    #: archive by this project; they are not official climatological normals.
    not_official_normals: bool = True


# ---------------------------------------------------------------------------
# Explanations
# ---------------------------------------------------------------------------


class EvidenceOut(ApiModel):
    label: str
    value: float | int | str
    unit: str | None = None
    source_tool: str


class ExplanationOut(ApiModel):
    headline: str
    statistical_explanation: str
    historical_context: str
    caveats: str
    evidence: list[EvidenceOut] = Field(default_factory=list)
    confidence: str
    #: ``llm`` or ``template``. Surfaced so a reader can tell which explanations
    #: a model wrote and which are deterministic prose.
    generator: str
    llm_provider: str | None = None
    model: str | None = None
    tool_call_count: int = 0
    fallback_reason: str | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class EventCalculationOut(ApiModel):
    """The scoring trace, flat enough to render in a breakdown panel."""

    deviation: float | None = None
    robust_deviation: float | None = None
    z_score: float | None = None
    #: False whenever the distribution does not justify reading the z-score as a
    #: probability. Clients must not compare z-scores across metrics when this is
    #: false.
    z_valid: bool = False
    percentile: float | None = None
    tail_probability: float | None = None
    #: True when the tail probability is capped by sample size (1/(n+1)) rather
    #: than measured. Present it as "at least this rare", never as exact.
    tail_probability_is_bounded: bool = False
    beyond_baseline_sample: bool = False
    return_period_years: float | None = None
    surprisal: float = 0.0
    margin_bonus: float = 0.0
    anomaly_score: float = 0.0


class EventBaselineOut(ApiModel):
    mean: float | None = None
    median: float | None = None
    std: float | None = None
    p25: float | None = None
    p75: float | None = None
    min: float | None = None
    max: float | None = None
    n: int = 0
    sufficient: bool = False


class EventOut(ApiModel):
    id: str
    city: CityOut
    local_date: date
    metric: str
    metric_label: str
    category: str
    direction: str
    observed_value: float
    unit: str

    baseline: EventBaselineOut
    calculation: EventCalculationOut

    data_tier: str
    data_quality: str
    source_dataset: str
    observation_type: str
    methodology_version: str

    explanation: ExplanationOut | None = None

    #: Always true. Present on every event so a client cannot render one of these
    #: as a weather record by omission.
    is_statistical_outlier: bool = True
    is_verified_official_record: bool = False


class EventDetailOut(EventOut):
    """A single event with its full machine-readable calculation trace."""

    evidence: dict | None = None


# ---------------------------------------------------------------------------
# Rankings
# ---------------------------------------------------------------------------


class RankedEventOut(ApiModel):
    rank: int
    score: float
    event: EventOut


class RunProvenanceOut(ApiModel):
    run_id: str
    analysis_date: date
    data_tier: str | None = None
    published_at: datetime | None = None
    started_at: datetime
    finished_at: datetime | None = None
    cities_total: int
    cities_with_data: int
    completeness: float | None = None
    events_total: int
    events_published: int
    methodology_version: str
    registry_version: str | None = None
    #: Counts, not dollars, unless the operator has configured token prices.
    llm_calls: int = 0
    llm_estimated_usd: float = 0.0
    llm_budget_exhausted: bool = False


class DataSourceOut(ApiModel):
    name: str
    url: str
    dataset: str
    licence: str
    attribution: str
    observation_type: str
    note: str | None = None


class RankingsOut(ApiModel):
    analysis_date: date
    published_at: datetime | None = None
    methodology_version: str
    #: Fixed label. These are statistical outliers against a 30-year seasonal
    #: distribution, not records of any kind.
    result_type: Literal["statistical_outliers"] = "statistical_outliers"
    ranking_basis: str
    one_event_per_city: bool
    count: int
    events: list[RankedEventOut]
    run: RunProvenanceOut
    data_sources: list[DataSourceOut]
    #: True when the requested date had no published run and an earlier one was
    #: served instead, so the UI can say so plainly.
    is_latest_available: bool = True
    requested_date: date | None = None


class ArchiveEntryOut(ApiModel):
    analysis_date: date
    published_at: datetime | None = None
    methodology_version: str
    event_count: int
    top_city: str | None = None
    top_metric: str | None = None
    top_score: float | None = None


class ArchiveOut(ApiModel):
    count: int
    total: int
    limit: int
    offset: int
    entries: list[ArchiveEntryOut]


# ---------------------------------------------------------------------------
# City detail and history
# ---------------------------------------------------------------------------


class CityDetailOut(ApiModel):
    city: CityOut
    analysis_date: date | None = None
    latest_observation: ObservationOut | None = None
    baselines: list[BaselineOut] = Field(default_factory=list)
    events: list[EventOut] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class CityHistoryPointOut(ApiModel):
    local_date: date
    temp_max_c: float | None = None
    temp_min_c: float | None = None
    temp_mean_c: float | None = None
    precipitation_mm: float | None = None
    wind_gust_max_kmh: float | None = None
    data_tier: str
    data_quality: str


class CityHistoryOut(ApiModel):
    city: CityOut
    start_date: date
    end_date: date
    count: int
    points: list[CityHistoryPointOut]
    source_datasets: list[str]


# ---------------------------------------------------------------------------
# Methodology, health, errors
# ---------------------------------------------------------------------------


class MethodologyOut(ApiModel):
    methodology_version: str
    registry_version: str | None = None
    reference_period: str
    seasonal_window_days: int
    seasonal_window_day_count: int
    calendar: str
    leap_day_handling: str
    min_samples: int
    min_years: int
    min_wet_days: int
    ranking_top_n: int
    one_event_per_city: bool
    ranking_basis: str
    tiebreak_chain: list[str]
    score_formula: str
    metrics: list[dict]
    data_sources: list[DataSourceOut]
    limitations: list[str]
    ai: dict


class HealthOut(ApiModel):
    status: Literal["ok", "degraded"]
    environment: str
    methodology_version: str
    database: str
    cities: int | None = None
    latest_published_date: date | None = None
    latest_published_at: datetime | None = None
    #: Hours since the last successful publish. The scheduler alerts on this.
    hours_since_publish: float | None = None
    llm_enabled: bool = False


class ErrorOut(BaseModel):
    error: str
    detail: str
    status_code: int


# ---------------------------------------------------------------------------
# Monitoring (pipeline runs, evaluations, MLflow traces)
# ---------------------------------------------------------------------------


class MonitorRunOut(ApiModel):
    run_id: str
    kind: str
    analysis_date: date | None = None
    status: str
    data_tier: str | None = None
    published: bool
    started_at: datetime | None = None
    duration_ms: int | None = None
    cities_total: int = 0
    cities_with_data: int = 0
    completeness: float | None = None
    events_total: int = 0
    events_published: int = 0
    llm_calls: int = 0
    llm_estimated_usd: float = 0.0
    error: str | None = None
    error_type: str | None = None


class MonitorRunsOut(ApiModel):
    count: int
    runs: list[MonitorRunOut] = Field(default_factory=list)


class MonitorEvalOut(ApiModel):
    id: int
    generated_at: datetime | None = None
    status: str
    git_commit: str | None = None
    git_dirty: bool = False
    suites_total: int = 0
    suites_passed: int = 0
    cases_total: int = 0
    cases_passed: int = 0
    duration_ms: int = 0


class MonitorEvalsOut(ApiModel):
    count: int
    reports: list[MonitorEvalOut] = Field(default_factory=list)


class MonitorSpanOut(ApiModel):
    span_id: str
    parent_span_id: str | None = None
    name: str
    span_type: str | None = None
    status: str
    latency_ms: int | None = None
    attributes: dict = Field(default_factory=dict)


class MonitorTraceOut(ApiModel):
    trace_id: str
    timestamp_ms: int | None = None
    status: str
    duration_ms: int | None = None
    root_span: str | None = None
    span_count: int = 0
    model: str | None = None
    methodology_version: str | None = None
    prompt_version: str | None = None
    run_id: str | None = None


class MonitorTraceDetailOut(ApiModel):
    trace_id: str
    timestamp_ms: int | None = None
    status: str
    duration_ms: int | None = None
    spans: list[MonitorSpanOut] = Field(default_factory=list)


class MonitorTracesOut(ApiModel):
    #: False when the tracking store is not configured or unreachable, in which
    #: case ``note`` says why and ``traces`` is empty rather than fabricated.
    available: bool
    note: str | None = None
    count: int = 0
    traces: list[MonitorTraceOut] = Field(default_factory=list)


class AgentStatusOut(ApiModel):
    available: bool
    #: ``ok`` / ``degraded`` / ``unknown``.
    level: str
    label: str
    detail: str | None = None
    runs: int = 0


# ---------------------------------------------------------------------------
# Chat (NL→SQL)
# ---------------------------------------------------------------------------


class ChatRequest(ApiModel):
    question: str = Field(min_length=1, max_length=1000)

class ChatResponse(ApiModel):
    question: str
    answer: str
    #: The SQL the model produced and the executor ran, so the answer is auditable.
    sql: str | None = None
    explanation: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list] = Field(default_factory=list)
    row_count: int = 0
    #: True when the row cap was hit, so a caller never mistakes a truncation for
    #: a complete result.
    truncated: bool = False
    #: True when the question could not be answered with a read-only query.
    refused: bool = False


class AgentChatRequest(ApiModel):
    question: str = Field(min_length=1, max_length=1000)


class AgentChatResponse(ApiModel):
    question: str
    answer: str
    #: How many traces the answer was grounded in.
    trace_count: int = 0
    #: False when the tracking store is not configured or reachable.
    available: bool = True
    note: str | None = None
