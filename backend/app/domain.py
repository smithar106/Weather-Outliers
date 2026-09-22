"""Shared vocabulary: metrics, units, tiers, and the methodology version.

Kept dependency-free so every layer (models, stats, agent, API) can import it
without cycles.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

# ---------------------------------------------------------------------------
# METHODOLOGY VERSION
# ---------------------------------------------------------------------------
# Bump whenever the baseline construction, anomaly maths, or ranking rules
# change in a way that makes two runs non-comparable. Baselines and events are
# keyed by this string, so historical runs keep the methodology they were
# computed under and the archive stays honest.
#
#   1.0.0 — initial release:
#           1991-2020 reference period, +/-7 day seasonal window on a 365-day
#           no-leap calendar, empirical tail probabilities via quantile sketch
#           with explicit tail order statistics, surprisal ranking
#           (-log10 p) plus a bounded out-of-sample IQR margin term,
#           one-event-per-city top 10.
METHODOLOGY_VERSION: Final[str] = "1.0.0"


class Metric(StrEnum):
    """The five daily metrics analysed."""

    TEMP_MAX = "temp_max"
    TEMP_MIN = "temp_min"
    TEMP_MEAN = "temp_mean"
    PRECIPITATION = "precipitation"
    WIND_GUST = "wind_gust"


class Direction(StrEnum):
    ABOVE = "above"
    BELOW = "below"


class DataTier(StrEnum):
    """How settled the underlying weather data is."""

    PROVISIONAL = "provisional"  # near-real-time model analysis, may be revised
    FINAL = "final"  # ERA5 / ERA5-Land reanalysis archive


class ObservationType(StrEnum):
    """What the numbers physically are. Never conflate these in user-facing copy."""

    REANALYSIS = "reanalysis"
    MODEL_ANALYSIS = "model_analysis"
    STATION_OBSERVATION = "station_observation"


class DataQuality(StrEnum):
    OK = "ok"
    INCOMPLETE = "incomplete"
    MISSING = "missing"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RunKind(StrEnum):
    DAILY = "daily"
    BACKFILL = "backfill"
    FINALIZE = "finalize"
    BASELINES = "baselines"


class Generator(StrEnum):
    LLM = "llm"
    TEMPLATE = "template"


# ---------------------------------------------------------------------------
# Metric metadata
# ---------------------------------------------------------------------------

UNITS: Final[dict[Metric, str]] = {
    Metric.TEMP_MAX: "°C",
    Metric.TEMP_MIN: "°C",
    Metric.TEMP_MEAN: "°C",
    Metric.PRECIPITATION: "mm",
    Metric.WIND_GUST: "km/h",
}

METRIC_LABELS: Final[dict[Metric, str]] = {
    Metric.TEMP_MAX: "Daily high temperature",
    Metric.TEMP_MIN: "Daily low temperature",
    Metric.TEMP_MEAN: "Daily mean temperature",
    Metric.PRECIPITATION: "Daily precipitation",
    Metric.WIND_GUST: "Peak wind gust",
}

# Short category used for map marker colour and card badges.
METRIC_CATEGORY: Final[dict[Metric, str]] = {
    Metric.TEMP_MAX: "heat",
    Metric.TEMP_MIN: "cold",
    Metric.TEMP_MEAN: "temperature",
    Metric.PRECIPITATION: "precipitation",
    Metric.WIND_GUST: "wind",
}

# Metrics whose seasonal distribution is close enough to symmetric for a
# z-score to be a meaningful summary. For everything else a z-score is either
# withheld or explicitly flagged as not comparable.
Z_SCORE_METRICS: Final[frozenset[Metric]] = frozenset(
    {Metric.TEMP_MAX, Metric.TEMP_MIN, Metric.TEMP_MEAN}
)

# Metrics evaluated in the upper tail only. A single dry day is not a
# meaningful daily anomaly when the climatological dry-day fraction is high, and
# low peak gusts are not newsworthy, so both are one-sided.
UPPER_TAIL_ONLY_METRICS: Final[frozenset[Metric]] = frozenset(
    {Metric.PRECIPITATION, Metric.WIND_GUST}
)

# Deterministic tie-break ordering. Earlier = preferred when every numeric
# component of the score is identical. Fixed, documented, and never data
# dependent, so reruns reproduce the same board.
METRIC_TIEBREAK_ORDER: Final[tuple[Metric, ...]] = (
    Metric.TEMP_MAX,
    Metric.TEMP_MIN,
    Metric.PRECIPITATION,
    Metric.WIND_GUST,
    Metric.TEMP_MEAN,
)

ALL_METRICS: Final[tuple[Metric, ...]] = tuple(Metric)


def metric_tiebreak_index(metric: Metric | str) -> int:
    m = Metric(metric)
    try:
        return METRIC_TIEBREAK_ORDER.index(m)
    except ValueError:  # pragma: no cover - defensive
        return len(METRIC_TIEBREAK_ORDER)


def build_event_id(
    local_date: str,
    city_id: str,
    metric: Metric | str,
    methodology_version: str = METHODOLOGY_VERSION,
) -> str:
    """Deterministic, human-readable event identity.

    A pure function of (date, city, metric, methodology version), which gives us
    two properties at once:

    * Rerunning a date under the same methodology updates rows in place instead
      of creating duplicates — idempotent ingestion for free.
    * Recomputing a date under a *new* methodology creates new rows, so archived
      rankings keep pointing at the exact numbers they were published with.
    """
    return f"{local_date}_{city_id}_{Metric(metric).value}@{methodology_version}"
