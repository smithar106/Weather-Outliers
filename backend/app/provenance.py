"""Single source of truth for data-source and methodology metadata.

Every layer that has to tell a reader where a number came from — the API, the
methodology page, the agent's tool ``_meta`` blocks, the footer of the site —
reads from here. Duplicating this text would guarantee that one copy eventually
says something the project no longer does.

Licensing facts below were read from the providers' own published terms on the
date in :data:`SOURCES_VERIFIED_ON` and are restated, not interpreted. They are
not legal advice, and an operator deploying this commercially needs to check the
current terms themselves — which is why :func:`licensing_notice` exists and is
surfaced in the API rather than buried in a comment.
"""

from __future__ import annotations

from datetime import date

from app.config import Settings
from app.domain import (
    METHODOLOGY_VERSION,
    METRIC_CATEGORY,
    METRIC_LABELS,
    UNITS,
    UPPER_TAIL_ONLY_METRICS,
    Z_SCORE_METRICS,
    Metric,
)
from app.stats.ranking import TIEBREAK_CHAIN

#: When the provider terms quoted below were last read. Re-check before any
#: commercial deployment; published terms change without notice.
SOURCES_VERIFIED_ON = date(2026, 9, 25)

RANKING_BASIS = (
    "Events are ranked by surprisal, -log10 of the empirical probability of a day "
    "at least this extreme in the same seasonal window, plus a bounded margin term "
    "for readings that fall outside the reference sample entirely. Raw temperature "
    "and rainfall magnitudes are never used as the ranking, because a hot day in "
    "Phoenix and a hot day in Iqaluit are not comparable in degrees."
)

SCORE_FORMULA = (
    "score = -log10(p_tail) + 0.5 * log10(1 + margin / IQR), where p_tail is the "
    "empirical one-sided tail probability against the city's seasonal reference "
    "distribution and margin is the distance beyond the nearest reference-sample "
    "extreme (0 when the reading falls inside the sample)."
)

LEAP_DAY_HANDLING = (
    "Baselines are keyed to a 365-day no-leap calendar. February 29 folds onto the "
    "same index as February 28, so a leap day is analysed against the neighbouring "
    "seasonal window rather than against a sample of only seven historical "
    "observations."
)

CALENDAR_NOTE = (
    "Day-of-year indices run 1-365 on a no-leap calendar, and the seasonal window "
    "wraps circularly across the new year, so late December and early January share "
    "reference days as they should."
)

#: Stated on the methodology page and on every ranking payload. Each of these is
#: a real limit of this project, not a disclaimer template.
LIMITATIONS: tuple[str, ...] = (
    "Values are gridded reanalysis or operational model-analysis estimates for the "
    "grid cell nearest each city, not readings from a weather station inside it. "
    "They can differ from an official station report, particularly for precipitation "
    "and wind gusts, and for cities with strong local terrain effects.",
    "Results are statistical outliers against a 1991-2020 seasonal distribution. No "
    "authoritative records archive is consulted, so nothing here is verified as a "
    "city, state, national, or all-time record, and the site never claims one.",
    "The baseline distributions are computed by this project from a reanalysis "
    "archive. They are not official climatological normals published by a national "
    "meteorological service, and they will not match those normals exactly.",
    "Coverage is a curated sample of major North American cities, not a systematic "
    "survey. An unusual day in a city outside the registry will not appear, so the "
    "board answers 'which of these 50 cities was most unusual', not 'where was the "
    "most unusual weather in North America'.",
    "Tail probabilities are bounded below by 1/(n+1) for a reference sample of n "
    "days. A reading beyond every value in its sample is reported as 'at least this "
    "rare' rather than with an exact frequency, and the payload flags this with "
    "tail_probability_is_bounded.",
    "Z-scores are published only for temperature metrics, where the seasonal "
    "distribution is close enough to symmetric for them to summarise anything. They "
    "are never used for ranking, and z_valid is false wherever they should not be "
    "read as probabilities.",
    "Precipitation is modelled as a zero-inflated mixture, with a wet day defined as "
    "any amount strictly greater than zero rather than the 0.2 mm or 1.0 mm "
    "conventions, because the reanalysis reports continuous trace amounts and any "
    "threshold imposed here would be an undocumented editorial choice.",
    "Same-day rankings use a provisional operational analysis that can be revised. "
    "Those runs are labelled provisional and are recomputed against the settled "
    "reanalysis once it is available, which can change a board after publication.",
)


def data_sources(settings: Settings) -> list[dict]:
    """Provider metadata, including whether this deployment is on a paid plan."""
    commercial = bool(settings.open_meteo_api_key)
    return [
        {
            "name": "Open-Meteo Historical Weather API (ERA5 / ERA5-Land reanalysis)",
            "url": "https://open-meteo.com/en/docs/historical-weather-api",
            "dataset": "era5_seamless",
            "licence": (
                "Commercial subscription (customer endpoints)"
                if commercial
                else "CC BY 4.0, free tier for non-commercial use"
            ),
            "attribution": "Weather data by Open-Meteo.com, based on ERA5 reanalysis "
            "from the Copernicus Climate Change Service (C3S) / ECMWF.",
            "observation_type": "reanalysis",
            "note": "Settled archive, available after roughly a five-day lag. Used for "
            "all baselines and for finalised daily rankings.",
        },
        {
            "name": "Open-Meteo Forecast API (operational best-match analysis)",
            "url": "https://open-meteo.com/en/docs",
            "dataset": "best_match",
            "licence": (
                "Commercial subscription (customer endpoints)"
                if commercial
                else "CC BY 4.0, free tier for non-commercial use"
            ),
            "attribution": "Weather data by Open-Meteo.com.",
            "observation_type": "model_analysis",
            "note": "Near-real-time model analysis for the previous local day. Marked "
            "provisional, and superseded by the reanalysis when it lands.",
        },
        {
            "name": "Mapbox vector tiles (OpenStreetMap data)",
            "url": "https://www.mapbox.com/about/maps/",
            "dataset": "light-v11",
            "licence": "Map data ODbL 1.0 (OpenStreetMap); tiles served under the Mapbox terms",
            "attribution": "© Mapbox, © OpenStreetMap contributors.",
            "observation_type": "basemap",
            "note": "Basemap only. No weather data comes from this source.",
        },
    ]


def licensing_notice(settings: Settings) -> str:
    if settings.open_meteo_api_key:
        return (
            "This deployment uses an Open-Meteo commercial subscription key and the "
            "customer API endpoints."
        )
    return (
        "This deployment uses the Open-Meteo free tier, whose published terms cover "
        f"non-commercial use, with data under CC BY 4.0 (terms read {SOURCES_VERIFIED_ON}). "
        "Attribution is displayed on every page. A commercial deployment requires a "
        "paid subscription; setting OPEN_METEO_API_KEY switches the client to the "
        "customer endpoints with no code change."
    )


def metric_descriptors() -> list[dict]:
    """Per-metric methodology, including which tails and statistics apply."""
    return [
        {
            "metric": metric.value,
            "label": METRIC_LABELS[metric],
            "unit": UNITS[metric],
            "category": METRIC_CATEGORY[metric],
            "tails": "upper only" if metric in UPPER_TAIL_ONLY_METRICS else "both",
            "z_score_published": metric in Z_SCORE_METRICS,
            "deviation_reference": (
                "mean" if metric in Z_SCORE_METRICS else "median"
            ),
            "distribution_model": (
                "zero-inflated mixture: P(X >= x) = P(X > 0) * P(X >= x | X > 0)"
                if metric == Metric.PRECIPITATION
                else "empirical distribution from the seasonal reference sample"
            ),
        }
        for metric in Metric
    ]


def ai_descriptor(settings: Settings) -> dict:
    """How explanations are produced in this deployment."""
    enabled = settings.llm_enabled
    return {
        "explanations_enabled": True,
        "llm_enabled": enabled,
        "generator_default": "llm" if enabled else "template",
        "provider": settings.llm_provider if enabled else "none",
        "model": (
            settings.anthropic_model
            if enabled and settings.llm_provider == "anthropic"
            else settings.openai_model
            if enabled and settings.llm_provider == "openai"
            else None
        ),
        "precomputed": True,
        "precompute_note": (
            "Explanations are generated once, server-side, during the scheduled "
            "pipeline and stored with the ranking. Page views read stored rows; a "
            "visitor never triggers a model call."
        ),
        "tools": [
            "get_daily_rankings",
            "get_city_weather",
            "get_city_baseline",
            "get_historical_extremes",
            "get_anomaly_evidence",
        ],
        "bounds": {
            "max_tool_calls_per_event": settings.agent_max_tool_calls,
            "max_iterations_per_event": settings.agent_max_iterations,
            "max_retries_per_event": settings.agent_max_retries,
            "timeout_seconds_per_event": settings.agent_timeout_seconds,
            "events_investigated": settings.agent_investigate_top_n,
            "monthly_max_llm_calls": settings.agent_monthly_max_llm_calls,
            "monthly_usd_budget": settings.agent_monthly_usd_budget,
        },
        "grounding": (
            "Every number in a generated explanation is checked against the exact "
            "values the tools returned. Explanations that cite an unsupported figure, "
            "claim a record, assert a meteorological cause, or reference an outside "
            "source are rejected and replaced with deterministic prose."
        ),
        "fallback": (
            "With no LLM configured, every explanation is deterministic and generated "
            "from the stored calculation. This is the default, and it is the same "
            "prose that replaces any generated explanation which fails verification."
        ),
    }


def methodology(settings: Settings, registry_version: str | None = None) -> dict:
    window = settings.baseline_seasonal_window_days
    return {
        "methodology_version": METHODOLOGY_VERSION,
        "registry_version": registry_version,
        "reference_period": settings.reference_period_label,
        "seasonal_window_days": window,
        "seasonal_window_day_count": 2 * window + 1,
        "calendar": CALENDAR_NOTE,
        "leap_day_handling": LEAP_DAY_HANDLING,
        "min_samples": settings.baseline_min_samples,
        "min_years": settings.baseline_min_years,
        "min_wet_days": settings.baseline_min_wet_days,
        "ranking_top_n": settings.ranking_top_n,
        "one_event_per_city": settings.ranking_one_event_per_city,
        "ranking_basis": RANKING_BASIS,
        "tiebreak_chain": list(TIEBREAK_CHAIN),
        "score_formula": SCORE_FORMULA,
        "metrics": metric_descriptors(),
        "data_sources": data_sources(settings),
        "limitations": [*LIMITATIONS, licensing_notice(settings)],
        "ai": ai_descriptor(settings),
    }
