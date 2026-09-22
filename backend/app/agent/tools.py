"""The five tools the investigation agent is allowed to call.

Design rules, all of them deliberate:

1. **Read-only.** Nothing here writes. The agent cannot change a number it is
   supposed to be explaining.
2. **Bounded.** Every query is keyed on a primary key or a narrow index and
   returns a fixed-size payload. There is no tool that can ask for "all events".
3. **Self-describing.** Each result carries a ``_meta`` block stating what the
   numbers are and — more importantly — what they are not. The
   ``get_historical_extremes`` result says in so many words that its figures are
   reference-period sample extremes and not records of any kind, because that is
   the single most likely place for a model to overclaim.
4. **Grounding surface.** Every numeric leaf of every tool result is collected
   into :attr:`AgentToolkit.observed_numbers`. The guards later refuse to publish
   a sentence containing a number the tools never returned, so this collection is
   the whitelist.
5. **Errors are data.** A bad argument returns an ``error`` payload instead of
   raising, so a model that guesses a city id can recover inside its remaining
   budget rather than failing the whole event.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.domain import (
    METHODOLOGY_VERSION,
    METRIC_CATEGORY,
    METRIC_LABELS,
    UNITS,
    Metric,
)
from app.ingest.baselines import get_baseline_row
from app.ingest.observations import get_observation
from app.models import AnomalyEvent, BaselineStatistic, City, DailyRanking, PipelineRun
from app.stats.seasonal import noleap_day_of_year, window_label

logger = logging.getLogger(__name__)

MAX_RANKING_ROWS = 10


class ToolError(Exception):
    """Raised internally; always converted to an ``error`` payload."""


def _parse_date(value: Any, field: str = "date") -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ToolError(f"{field} must be a string in YYYY-MM-DD form")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ToolError(f"{field} '{value}' is not a valid YYYY-MM-DD date") from exc


def _parse_metric(value: Any) -> Metric:
    try:
        return Metric(str(value).strip().lower())
    except ValueError as exc:
        allowed = ", ".join(m.value for m in Metric)
        raise ToolError(f"metric '{value}' is not recognised. Allowed values: {allowed}") from exc


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


class AgentToolkit:
    """Session-scoped tool implementations plus the grounding whitelist."""

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        #: Every number any tool has returned during this investigation.
        self.observed_numbers: set[float] = set()
        #: Every string any tool has returned, lower-cased, for source-claim checks.
        self.observed_text: list[str] = []
        self.call_log: list[dict] = []

    # -- dispatch ----------------------------------------------------------

    def call(self, name: str, arguments: dict | None) -> dict:
        """Execute a tool by name. Never raises; returns an error payload instead."""
        arguments = arguments or {}
        handler = {
            "get_daily_rankings": self.get_daily_rankings,
            "get_city_weather": self.get_city_weather,
            "get_city_baseline": self.get_city_baseline,
            "get_historical_extremes": self.get_historical_extremes,
            "get_anomaly_evidence": self.get_anomaly_evidence,
        }.get(name)

        if handler is None:
            result = {
                "error": f"unknown tool '{name}'",
                "available_tools": sorted(t["name"] for t in TOOL_SPECS),
            }
        else:
            try:
                result = handler(**arguments)
            except ToolError as exc:
                result = {"error": str(exc)}
            except TypeError as exc:
                # Wrong or missing argument names.
                result = {"error": f"invalid arguments for {name}: {exc}"}
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("tool %s failed", name)
                result = {"error": f"{name} failed: {type(exc).__name__}"}

        self._harvest(result)
        # The log is persisted with the explanation, so it records what was asked
        # and whether it worked rather than the full payload — a ranking of ten
        # events would otherwise carry megabytes of duplicated tool output into
        # the database and out through the API.
        error = result.get("error") if isinstance(result, dict) else None
        self.call_log.append(
            {
                "tool": name,
                "arguments": arguments,
                "ok": error is None,
                "error": error,
                "result_keys": sorted(k for k in result if k != "_meta")
                if isinstance(result, dict)
                else None,
            }
        )
        return result

    def register_context(self, payload: Any) -> None:
        """Whitelist facts stated in the prompt itself.

        The opening message tells the model the city, date, metric and observed
        value so it has somewhere to start. Those figures are legitimate
        citations, so they belong in the grounding whitelist alongside the tool
        results — otherwise the guards would reject the model for repeating what
        we told it.
        """
        self._harvest(payload)

    def _harvest(self, payload: Any) -> None:
        """Record every numeric and textual leaf so the guards can verify citations."""
        if isinstance(payload, bool):
            return
        if isinstance(payload, (int, float)):
            self.observed_numbers.add(float(payload))
            return
        if isinstance(payload, str):
            self.observed_text.append(payload.lower())
            return
        if isinstance(payload, dict):
            for key, value in payload.items():
                self.observed_text.append(str(key).lower())
                self._harvest(value)
            return
        if isinstance(payload, Iterable):
            for item in payload:
                self._harvest(item)

    # -- helpers -----------------------------------------------------------

    def _city(self, city_id: Any) -> City:
        if not isinstance(city_id, str) or not city_id.strip():
            raise ToolError("city_id must be a non-empty string such as 'us-denver-co'")
        city = self.session.get(City, city_id.strip())
        if city is None:
            raise ToolError(
                f"no city with id '{city_id}'. Use the city_id values returned by "
                f"get_daily_rankings."
            )
        return city

    @staticmethod
    def _city_block(city: City) -> dict:
        return {
            "city_id": city.id,
            "name": city.name,
            "admin": city.admin,
            "country": city.country,
            "timezone": city.timezone,
        }

    # -- tools -------------------------------------------------------------

    def get_daily_rankings(self, date: Any = None, **_: Any) -> dict:
        """Published top-N board for one analysis date."""
        analysis_date = _parse_date(date)

        run = self.session.scalars(
            select(PipelineRun)
            .where(
                PipelineRun.analysis_date == analysis_date,
                PipelineRun.published.is_(True),
            )
            .order_by(PipelineRun.published_at.desc())
            .limit(1)
        ).first()
        if run is None:
            return {
                "error": f"no published ranking for {analysis_date.isoformat()}",
                "_meta": {"hint": "Rankings exist only for dates the pipeline has published."},
            }

        rows = self.session.scalars(
            select(DailyRanking)
            .where(DailyRanking.run_id == run.id)
            .order_by(DailyRanking.rank)
            .limit(MAX_RANKING_ROWS)
        ).all()

        rankings = []
        for row in rows:
            event = row.event
            city = event.city
            rankings.append(
                {
                    "rank": row.rank,
                    "event_id": event.id,
                    "city_id": city.id,
                    "city": city.name,
                    "admin": city.admin,
                    "country": city.country,
                    "metric": event.metric,
                    "metric_label": METRIC_LABELS[Metric(event.metric)],
                    "category": METRIC_CATEGORY[Metric(event.metric)],
                    "direction": event.direction,
                    "observed_value": _round(event.observed_value),
                    "unit": event.unit,
                    "baseline_median": _round(event.baseline_median),
                    "deviation": _round(event.deviation),
                    "percentile": _round(event.percentile, 3),
                    "tail_probability": event.tail_probability,
                    "anomaly_score": _round(event.anomaly_score, 3),
                }
            )

        return {
            "analysis_date": analysis_date.isoformat(),
            "data_tier": run.data_tier,
            "methodology_version": run.methodology_version,
            "published_at": run.published_at.isoformat() if run.published_at else None,
            "count": len(rankings),
            "rankings": rankings,
            "_meta": {
                "ranking_basis": (
                    "Events are ordered by an empirical-tail-probability surprisal score, "
                    "not by the raw size of the measurement."
                ),
                "not_records": (
                    "These are statistical outliers relative to a 30-year seasonal "
                    "distribution. They are not official weather records."
                ),
            },
        }

    def get_city_weather(self, city_id: Any = None, date: Any = None, **_: Any) -> dict:
        """The stored daily values for one city-day, with full provenance."""
        city = self._city(city_id)
        local_date = _parse_date(date)

        observation = get_observation(self.session, city.id, local_date)
        if observation is None:
            return {
                "error": (
                    f"no stored observation for {city.id} on {local_date.isoformat()}"
                ),
                "_meta": {"hint": "The pipeline only stores dates it has analysed."},
            }

        return {
            "city": self._city_block(city),
            "local_date": observation.local_date.isoformat(),
            "values": {
                "temp_max_c": _round(observation.temp_max_c),
                "temp_min_c": _round(observation.temp_min_c),
                "temp_mean_c": _round(observation.temp_mean_c),
                "precipitation_mm": _round(observation.precipitation_mm),
                "wind_gust_max_kmh": _round(observation.wind_gust_max_kmh),
            },
            "units": observation.units,
            "provenance": {
                "source_provider": observation.source_provider,
                "source_dataset": observation.source_dataset,
                "observation_type": observation.observation_type,
                "data_tier": observation.data_tier,
                "data_quality": observation.data_quality,
                "missing_fields": observation.missing_fields or [],
                "utc_offset_seconds": observation.utc_offset_seconds,
                "grid_latitude": observation.grid_latitude,
                "grid_longitude": observation.grid_longitude,
                "grid_elevation_m": observation.grid_elevation_m,
                "retrieved_at": observation.retrieved_at.isoformat()
                if observation.retrieved_at
                else None,
            },
            "_meta": {
                "observation_type_note": (
                    "These values come from a gridded reanalysis or operational model "
                    "analysis at the nearest grid cell. They are model estimates, NOT "
                    "direct readings from a weather station in this city."
                )
            },
        }

    def get_city_baseline(
        self, city_id: Any = None, metric: Any = None, date: Any = None, **_: Any
    ) -> dict:
        """Seasonal reference distribution for one city, metric and calendar position."""
        city = self._city(city_id)
        metric_enum = _parse_metric(metric)
        local_date = _parse_date(date)

        row = get_baseline_row(self.session, city.id, metric_enum, local_date, self.settings)
        if row is None:
            return {
                "error": (
                    f"no cached baseline for {city.id} / {metric_enum.value} near "
                    f"{local_date.isoformat()}"
                ),
                "_meta": {"hint": "Baselines are built per city by the pipeline."},
            }

        payload: dict = {
            "city": self._city_block(city),
            "metric": metric_enum.value,
            "unit": UNITS[metric_enum],
            "reference_period": f"{row.reference_start_year}-{row.reference_end_year}",
            "seasonal_window": window_label(row.day_of_year, row.window_days),
            "window_days": row.window_days,
            "day_of_year": row.day_of_year,
            "n_samples": row.n_samples,
            "n_years": row.n_years,
            "sufficient": row.sufficient,
            "mean": _round(row.mean),
            "std": _round(row.std),
            "median": _round(row.median),
            "p25": _round(row.p25),
            "p75": _round(row.p75),
            "iqr": _round(row.iqr),
            "sample_minimum": _round(row.min_value),
            "sample_maximum": _round(row.max_value),
            "source_dataset": row.source_dataset,
            "methodology_version": row.methodology_version,
            "_meta": {
                "definition": (
                    "Distribution of all daily values observed within +/- "
                    f"{row.window_days} calendar days of this date across the "
                    f"{row.reference_start_year}-{row.reference_end_year} reference period."
                ),
                "not_normals": (
                    "This is a reanalysis-derived reference distribution computed by this "
                    "project. It is not an official climatological normal published by a "
                    "national meteorological service."
                ),
                "sample_extremes_note": (
                    "sample_minimum and sample_maximum are the lowest and highest values "
                    "in this reference sample. They are NOT records."
                ),
            },
        }
        if metric_enum == Metric.PRECIPITATION:
            payload["dry_day_fraction"] = _round(row.zero_fraction, 3)
            payload["wet_day_count"] = row.nonzero_n
            payload["_meta"]["mixture_note"] = (
                "Precipitation is modelled as a point mass at zero plus a wet-day "
                "distribution, so percentiles are computed on wet days only and then "
                "rescaled by the wet-day frequency."
            )
        return payload

    def get_historical_extremes(self, city_id: Any = None, metric: Any = None, **_: Any) -> dict:
        """Highest and lowest values anywhere in this city's reference-period sample.

        This is the tool most likely to tempt a model into writing "record", so
        the payload repeats the disclaimer three ways.
        """
        city = self._city(city_id)
        metric_enum = _parse_metric(metric)

        identity = (
            BaselineStatistic.city_id == city.id,
            BaselineStatistic.metric == metric_enum.value,
            BaselineStatistic.reference_start_year == self.settings.baseline_start_year,
            BaselineStatistic.reference_end_year == self.settings.baseline_end_year,
            BaselineStatistic.window_days == self.settings.baseline_seasonal_window_days,
            BaselineStatistic.methodology_version == METHODOLOGY_VERSION,
        )
        result = self.session.execute(
            select(
                func.min(BaselineStatistic.min_value),
                func.max(BaselineStatistic.max_value),
                func.min(BaselineStatistic.n_samples),
                func.count(),
            ).where(*identity)
        ).one_or_none()

        if result is None or result[3] == 0:
            return {
                "error": f"no cached baseline for {city.id} / {metric_enum.value}",
                "_meta": {"hint": "Baselines are built per city by the pipeline."},
            }

        sample_min, sample_max, _min_n, rows = result
        return {
            "city": self._city_block(city),
            "metric": metric_enum.value,
            "unit": UNITS[metric_enum],
            "reference_period": (
                f"{self.settings.baseline_start_year}-{self.settings.baseline_end_year}"
            ),
            "reference_sample_minimum": _round(sample_min),
            "reference_sample_maximum": _round(sample_max),
            "calendar_days_covered": int(rows),
            "source_dataset": "reanalysis reference period cached by this project",
            "_meta": {
                "these_are_not_records": (
                    "These are the smallest and largest daily values present in this "
                    "project's cached reanalysis sample for "
                    f"{self.settings.baseline_start_year}-"
                    f"{self.settings.baseline_end_year}. They are NOT official records, "
                    "NOT all-time extremes, and NOT verified against any authoritative "
                    "records archive. Never describe them as records."
                ),
                "period_bound": (
                    "Any value before "
                    f"{self.settings.baseline_start_year} or after "
                    f"{self.settings.baseline_end_year} is outside this sample entirely."
                ),
                "grid_bound": (
                    "Values are gridded model estimates for the nearest cell, not station "
                    "measurements."
                ),
            },
        }

    def get_anomaly_evidence(self, event_id: Any = None, **_: Any) -> dict:
        """The stored calculation trace for one event — the primary grounding source."""
        if not isinstance(event_id, str) or not event_id.strip():
            raise ToolError("event_id must be a non-empty string from get_daily_rankings")

        event = self.session.get(AnomalyEvent, event_id.strip())
        if event is None:
            return {
                "error": f"no anomaly event with id '{event_id}'",
                "_meta": {"hint": "Use the event_id values returned by get_daily_rankings."},
            }

        evidence = dict(event.evidence or {})
        evidence.setdefault("event_id", event.id)
        evidence["city"] = self._city_block(event.city)
        evidence["_meta"] = {
            "score_formula": "anomaly_score = -log10(p_tail) + 0.5 * log10(1 + margin_iqr)",
            "tail_probability_meaning": (
                "Empirical probability of a value at least this extreme on this calendar "
                "date, estimated from the cached reference distribution."
            ),
            "bounded_note": (
                "When tail_probability_is_bounded is true the observed value sits beyond "
                "every value in the reference sample, so the probability shown is an upper "
                "bound (1/(n+1)) rather than a measured frequency. Say 'at least this rare', "
                "not 'exactly this rare'."
            ),
            "z_score_note": (
                "z_score is reported for context only and is meaningful as a probability "
                "just when z_valid is true. Never compare z-scores across metrics."
            ),
            "causation_note": (
                "This trace contains no information about atmospheric conditions, weather "
                "systems, or causes. Do not assert why the weather happened."
            ),
        }
        return evidence


# ---------------------------------------------------------------------------
# Tool schemas handed to the model
# ---------------------------------------------------------------------------

TOOL_SPECS: list[dict] = [
    {
        "name": "get_daily_rankings",
        "description": (
            "Return the published top-10 statistical outlier board for one analysis date, "
            "including each event's id, city, metric, observed value, baseline median, "
            "percentile, tail probability and anomaly score. Use this to see how the event "
            "you are investigating compares with the rest of the day."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["date"],
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Analysis date in YYYY-MM-DD form.",
                }
            },
        },
    },
    {
        "name": "get_city_weather",
        "description": (
            "Return the stored daily weather values for one city on one local date "
            "(max/min/mean temperature in Celsius, precipitation in mm, maximum wind gust "
            "in km/h), together with the source dataset, observation type and data quality "
            "flags. Use this to see the other metrics for the same day."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["city_id", "date"],
            "properties": {
                "city_id": {
                    "type": "string",
                    "description": "City id such as 'us-denver-co'.",
                },
                "date": {"type": "string", "description": "Local date in YYYY-MM-DD form."},
            },
        },
    },
    {
        "name": "get_city_baseline",
        "description": (
            "Return the seasonal reference distribution for one city, metric and calendar "
            "date: sample size, mean, standard deviation, median, quartiles, and the "
            "minimum and maximum values in the reference sample. Use this to state what is "
            "normal for this city at this time of year."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["city_id", "metric", "date"],
            "properties": {
                "city_id": {"type": "string", "description": "City id such as 'us-denver-co'."},
                "metric": {
                    "type": "string",
                    "enum": [m.value for m in Metric],
                    "description": "Which metric's baseline to return.",
                },
                "date": {
                    "type": "string",
                    "description": "Local date in YYYY-MM-DD form; selects the seasonal window.",
                },
            },
        },
    },
    {
        "name": "get_historical_extremes",
        "description": (
            "Return the lowest and highest values for one city and metric anywhere in this "
            "project's cached 1991-2020 reanalysis reference sample. These are sample "
            "extremes for context ONLY and must never be described as records, all-time "
            "extremes, or verified measurements."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["city_id", "metric"],
            "properties": {
                "city_id": {"type": "string", "description": "City id such as 'us-denver-co'."},
                "metric": {
                    "type": "string",
                    "enum": [m.value for m in Metric],
                    "description": "Which metric's sample extremes to return.",
                },
            },
        },
    },
    {
        "name": "get_anomaly_evidence",
        "description": (
            "Return the full stored calculation trace for one anomaly event: observed value, "
            "baseline statistics, deviation, robust deviation, percentile, tail probability, "
            "whether that probability is bounded by sample size, the score components, and "
            "the data provenance. This is the primary source for your explanation."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["event_id"],
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "Event id, as returned by get_daily_rankings.",
                }
            },
        },
    },
]

TOOL_NAMES: tuple[str, ...] = tuple(spec["name"] for spec in TOOL_SPECS)


def seasonal_day_of_year(local_date: date) -> int:
    """Re-exported for callers that want the same calendar rule as the baselines."""
    return noleap_day_of_year(local_date)
