"""Central configuration.

Every knob in the statistical methodology, the agent's cost bounds, and the
provider limits is settable here so that a run is fully described by its
config plus its methodology version. Nothing important is hard-coded deeper in
the stack.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- database
    database_url: str = "postgresql+psycopg://weather:weather@localhost:5432/weather_outliers"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_recycle_seconds: int = 1800
    db_echo: bool = False

    # ------------------------------------------------------------- application
    environment: Literal["development", "production", "test"] = "development"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    cors_allow_origins: str = "http://localhost:3000"
    public_api_base_url: str = "http://localhost:8000"

    # ---------------------------------------------------------------- provider
    weather_provider: Literal["open_meteo", "fixture"] = "open_meteo"
    open_meteo_api_key: str = ""
    open_meteo_archive_url: str = "https://archive-api.open-meteo.com/v1/archive"
    open_meteo_forecast_url: str = "https://api.open-meteo.com/v1/forecast"
    open_meteo_customer_archive_url: str = (
        "https://customer-archive-api.open-meteo.com/v1/archive"
    )
    open_meteo_customer_forecast_url: str = "https://customer-api.open-meteo.com/v1/forecast"

    # Budgets are in Open-Meteo's *weighted* API calls, not HTTP requests: one
    # 10-year chunk of five daily variables is one request but ~130 calls. The
    # published free-tier allowances are 600/minute, 5,000/hour and 10,000/day;
    # these defaults keep roughly 15-20% headroom because the weight is our
    # estimate of the provider's accounting, not a reading of it. See
    # docs/data-sources.md.
    provider_max_call_weight_per_minute: float = 500.0
    provider_max_call_weight_per_hour: float = 4_200.0
    provider_max_call_weight_per_day: float = 8_500.0
    #: Longest the limiter will block for a window to reopen before raising
    #: ProviderBudgetExhausted. Minutely waits fit inside; hourly ones do not, and
    #: should stop the run so it can be resumed rather than hang.
    provider_max_budget_wait_seconds: float = 180.0
    provider_request_timeout_seconds: float = 60.0
    provider_max_retries: int = 3
    provider_backoff_base_seconds: float = 2.0
    provider_baseline_batch_size: int = 1

    # ---------------------------------------------------------------- baseline
    baseline_start_year: int = 1991
    baseline_end_year: int = 2020
    baseline_seasonal_window_days: int = 7
    baseline_min_samples: int = 120
    baseline_min_years: int = 20
    baseline_min_wet_days: int = 15
    baseline_histogram_bins: int = 24
    baseline_chunk_years: int = 10

    # ----------------------------------------------------------------- ranking
    ranking_top_n: int = 10
    ranking_one_event_per_city: bool = True
    allow_provisional_in_rankings: bool = True
    ranking_min_score: float = 0.5

    # ------------------------------------------------------------------- agent
    llm_provider: Literal["none", "anthropic", "openai"] = "none"
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    anthropic_model: str = "claude-sonnet-5"
    anthropic_api_version: str = "2023-06-01"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = ""
    #: Which field carries the output cap on this endpoint. OpenAI renamed
    #: ``max_tokens`` to ``max_completion_tokens`` and its reasoning models reject
    #: the old name outright; DeepSeek and most other OpenAI-compatible gateways
    #: still take ``max_tokens`` only. There is no field that works everywhere,
    #: and sending both is not an option because the strict endpoints reject the
    #: name they do not know. Getting this wrong on a lenient endpoint is worse
    #: than a crash: the parameter is ignored, the output cap silently stops
    #: applying, and the cost control it exists to provide is gone.
    openai_max_tokens_param: Literal["max_completion_tokens", "max_tokens"] = (
        "max_completion_tokens"
    )

    agent_max_tool_calls: int = 6
    agent_max_iterations: int = 5
    agent_timeout_seconds: float = 45.0
    agent_max_retries: int = 2
    agent_max_output_tokens: int = 1200
    agent_investigate_top_n: int = 10
    agent_monthly_usd_budget: float = 5.00

    # Token prices are deliberately 0.0 by default. This project does not ship a
    # price list, because published rates change and a stale number baked into a
    # repo is worse than no number. The operator copies the current figures from
    # their provider's pricing page into these two variables; until they do,
    # ``estimated_usd`` stays 0.0 and the USD budget cannot bind — which is why
    # the hard call caps below exist as the real spend ceiling.
    agent_usd_per_mtok_input: float = 0.0
    agent_usd_per_mtok_output: float = 0.0
    #: Absolute cap on LLM calls per calendar month, enforced whether or not
    #: prices are configured. This is the bound that always holds.
    agent_monthly_max_llm_calls: int = 2000

    # ------------------------------------------------------------ rate limiting
    rate_limit_enabled: bool = True
    rate_limit_requests_per_minute: int = 120
    rate_limit_burst: int = 30
    rate_limit_trust_forwarded_for: bool = True
    http_cache_seconds: int = 300
    max_page_size: int = 200
    max_history_days: int = 400

    # ---------------------------------------------------------------- pipeline
    pipeline_source_lag_hours: int = 6
    pipeline_finalize_lag_days: int = 6
    pipeline_min_city_completeness: float = 0.80

    # ----------------------------------------------------------- observability
    # Tracing is opt-in and defaults to off, so the deployed image attempts no
    # MLflow import and contacts no tracking server unless an operator asks for
    # it. MLflow itself is an optional extra, not a dependency. See EVALUATION.md.
    mlflow_tracing_enabled: bool = False
    #: Empty means MLflow's default, which is a local ``./mlruns`` directory. A
    #: ``file:`` or ``databricks:`` URI needs no server at all; an ``http://`` URI
    #: is a server whose authentication is the operator's responsibility, because
    #: MLflow's tracking server ships without any.
    mlflow_tracking_uri: str = ""
    mlflow_experiment: str = "weather-outliers"

    cities_file: str = Field(
        default="",
        description="Override path to cities.json. Empty means auto-discover ../data/cities.json.",
    )

    # ------------------------------------------------------------- validators
    @field_validator("database_url")
    @classmethod
    def _normalise_database_url(cls, v: str) -> str:
        """Accept the URL shape Railway/Heroku inject and upgrade the driver.

        Railway exposes ``postgresql://...``; SQLAlchemy would then reach for
        psycopg2, which we do not install. Rewriting here means the operator can
        paste ``${{Postgres.DATABASE_URL}}`` verbatim and it just works.
        """
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://") :]
        if v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://") :]
        return v

    @field_validator("baseline_end_year")
    @classmethod
    def _check_reference_period(cls, v: int, info) -> int:
        start = info.data.get("baseline_start_year")
        if start is not None and v < start:
            raise ValueError("baseline_end_year must be >= baseline_start_year")
        return v

    # ---------------------------------------------------------------- helpers
    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @property
    def archive_url(self) -> str:
        return (
            self.open_meteo_customer_archive_url
            if self.open_meteo_api_key
            else self.open_meteo_archive_url
        )

    @property
    def forecast_url(self) -> str:
        return (
            self.open_meteo_customer_forecast_url
            if self.open_meteo_api_key
            else self.open_meteo_forecast_url
        )

    @property
    def reference_period_label(self) -> str:
        return f"{self.baseline_start_year}-{self.baseline_end_year}"

    @property
    def llm_enabled(self) -> bool:
        """True only when a provider is selected AND its key is present AND budget > 0."""
        if self.agent_monthly_usd_budget <= 0:
            return False
        if self.llm_provider == "anthropic":
            return bool(self.anthropic_api_key)
        if self.llm_provider == "openai":
            return bool(self.openai_api_key)
        return False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test hook: forget the memoised Settings so env changes take effect."""
    get_settings.cache_clear()
