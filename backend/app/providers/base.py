"""Weather provider interface.

Everything downstream of this module — baselines, anomalies, ranking, the agent —
consumes :class:`DailyRecord` and knows nothing about Open-Meteo. Adding NOAA
GHCN-Daily, Environment Canada, or a commercial feed means writing one class
that emits ``DailyRecord``s, registering it in ``providers/__init__.py``, and
changing one environment variable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Protocol, runtime_checkable

from app.domain import DataQuality, DataTier, Metric, ObservationType

#: The daily fields every provider is expected to attempt. A provider that
#: cannot supply one must leave it ``None`` and list it in ``missing_fields``
#: rather than substituting a guess.
METRIC_FIELDS: dict[Metric, str] = {
    Metric.TEMP_MAX: "temp_max_c",
    Metric.TEMP_MIN: "temp_min_c",
    Metric.TEMP_MEAN: "temp_mean_c",
    Metric.PRECIPITATION: "precipitation_mm",
    Metric.WIND_GUST: "wind_gust_max_kmh",
}


@dataclass(slots=True)
class DailyRecord:
    """One city-day of daily aggregates, with its provenance attached."""

    city_id: str
    local_date: date

    temp_max_c: float | None = None
    temp_min_c: float | None = None
    temp_mean_c: float | None = None
    precipitation_mm: float | None = None
    wind_gust_max_kmh: float | None = None

    source_provider: str = "unknown"
    source_dataset: str = "unknown"
    source_endpoint: str | None = None
    observation_type: ObservationType = ObservationType.REANALYSIS
    data_tier: DataTier = DataTier.FINAL
    data_quality: DataQuality = DataQuality.OK
    missing_fields: list[str] = field(default_factory=list)
    units: dict[str, str] = field(default_factory=dict)

    utc_offset_seconds: int | None = None
    grid_latitude: float | None = None
    grid_longitude: float | None = None
    grid_elevation_m: float | None = None
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def value_for(self, metric: Metric) -> float | None:
        return getattr(self, METRIC_FIELDS[metric])

    def has_any_value(self) -> bool:
        return any(self.value_for(m) is not None for m in Metric)

    def classify_quality(self) -> DataQuality:
        """Derive a quality flag from which fields actually arrived.

        ``MISSING`` when nothing usable came back, ``INCOMPLETE`` when at least
        one requested metric is absent, otherwise ``OK``. Incomplete records are
        still stored and still used for the metrics they do carry; the flag
        travels with the row so the API can disclose it.
        """
        missing = [f for m, f in METRIC_FIELDS.items() if getattr(self, f) is None]
        self.missing_fields = missing
        if len(missing) == len(METRIC_FIELDS):
            return DataQuality.MISSING
        if missing:
            return DataQuality.INCOMPLETE
        return DataQuality.OK


class ProviderError(RuntimeError):
    """Base class for provider failures."""


class ProviderRateLimited(ProviderError):
    """The provider asked us to slow down (HTTP 429)."""


class ProviderUnavailable(ProviderError):
    """Transient upstream failure (5xx, timeout, connection reset)."""


class ProviderBadRequest(ProviderError):
    """We asked for something invalid (4xx other than 429). Not retryable."""


class ProviderBudgetExhausted(ProviderError):
    """Our own quota accounting says the next request cannot be afforded yet.

    Distinct from :class:`ProviderRateLimited`, which is the provider telling us
    after the fact. This is raised *before* a request is sent, when continuing
    would mean sleeping for hours rather than seconds — an hourly or daily
    window. Callers should stop and resume later rather than block; baseline
    building is idempotent and skips cities it has already completed.
    """


@runtime_checkable
class WeatherProvider(Protocol):
    """Contract every weather source must satisfy."""

    name: str

    def fetch_daily(
        self,
        *,
        city_id: str,
        latitude: float,
        longitude: float,
        timezone: str,
        start_date: date,
        end_date: date,
        tier: DataTier,
    ) -> list[DailyRecord]:
        """Return one :class:`DailyRecord` per local calendar day in range.

        Implementations must return records for the local calendar dates of
        ``timezone`` — not UTC days — and must not silently substitute a
        different date range than the one requested.
        """
        ...

    def close(self) -> None:
        """Release sockets/connections. The cron worker must exit cleanly."""
        ...
