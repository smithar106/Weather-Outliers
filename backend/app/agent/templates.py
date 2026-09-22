"""Deterministic explanations, used whenever no LLM is available or trusted.

This is not a degraded mode. With ``LLM_PROVIDER=none`` — the default, and what
anyone cloning the repo gets — every explanation on the site comes from here, so
the templates have to read like something a person would write. They are also
what the pipeline falls back to when a generated explanation fails the grounding
guards, which means the site degrades toward *more* conservative prose rather
than toward an error page.

Everything below is a pure function of one stored event. Same event, same words,
forever — which is also what makes the ranking reproducible end to end.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.agent.schemas import EventExplanation, EvidenceItem
from app.domain import (
    METRIC_LABELS,
    DataTier,
    Direction,
    Metric,
    ObservationType,
)

TEMPLATE_SOURCE = "get_anomaly_evidence"


@dataclass(slots=True)
class TemplateInput:
    """Everything the templates need, decoupled from the ORM for testability."""

    city_name: str
    city_admin: str
    city_country: str
    metric: Metric
    direction: Direction
    local_date: date
    observed_value: float
    unit: str
    reference_period: str
    window_days: int
    baseline_n: int
    baseline_sufficient: bool
    baseline_mean: float | None = None
    baseline_median: float | None = None
    baseline_p25: float | None = None
    baseline_p75: float | None = None
    baseline_min: float | None = None
    baseline_max: float | None = None
    deviation: float | None = None
    percentile: float | None = None
    tail_probability: float | None = None
    tail_probability_is_bounded: bool = False
    beyond_baseline_sample: bool = False
    return_period_years: float | None = None
    surprisal: float = 0.0
    anomaly_score: float = 0.0
    observation_type: str = ObservationType.REANALYSIS.value
    data_tier: str = DataTier.FINAL.value
    data_quality: str = "ok"
    dry_day_fraction: float | None = None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _num(value: float | None, digits: int = 1) -> str:
    return "unavailable" if value is None else f"{value:.{digits}f}"


def _prob(p: float | None) -> str:
    if p is None:
        return "unavailable"
    if p < 0.001:
        return f"{p:.5f}"
    if p < 0.01:
        return f"{p:.4f}"
    return f"{p:.3f}"


def _season_adjective(d: date) -> str:
    part = "early" if d.day <= 10 else "mid" if d.day <= 20 else "late"
    return f"{part}-{d.strftime('%B')}"


def _metric_noun(metric: Metric) -> str:
    return METRIC_LABELS[metric].lower()


def _observation_clause(inp: TemplateInput) -> str:
    """How the reading itself is described, in the voice of the metric."""
    value = _num(inp.observed_value)
    city = inp.city_name
    above = inp.direction == Direction.ABOVE
    match inp.metric:
        case Metric.TEMP_MAX:
            return f"{city} climbed to {value} °C" if above else (
                f"{city} peaked at only {value} °C"
            )
        case Metric.TEMP_MIN:
            return f"{city} never fell below {value} °C" if above else (
                f"{city} dropped to {value} °C"
            )
        case Metric.TEMP_MEAN:
            return f"{city} averaged {value} °C over the day"
        case Metric.PRECIPITATION:
            return f"{city} collected {value} mm of precipitation"
        case Metric.WIND_GUST:
            return f"{city} saw gusts reach {value} km/h"
    return f"{city} registered {value} {inp.unit}"  # pragma: no cover - exhaustive above


def _observation_type_phrase(observation_type: str) -> str:
    return {
        ObservationType.REANALYSIS.value: "an ERA5 reanalysis estimate",
        ObservationType.MODEL_ANALYSIS.value: "an operational model-analysis estimate",
        ObservationType.STATION_OBSERVATION.value: "a station observation",
    }.get(observation_type, "a modelled estimate")


# ---------------------------------------------------------------------------
# The four fields
# ---------------------------------------------------------------------------


def _headline(inp: TemplateInput) -> str:
    clause = _observation_clause(inp)
    season = _season_adjective(inp.local_date)

    if inp.metric in (Metric.TEMP_MAX, Metric.TEMP_MIN, Metric.TEMP_MEAN):
        if inp.deviation is not None:
            word = "above" if inp.deviation >= 0 else "below"
            return (
                f"{clause} — {_num(abs(inp.deviation))} °C {word} its "
                f"{inp.reference_period} {season} average."
            )
        return f"{clause}, far from its {inp.reference_period} {season} average."

    if inp.beyond_baseline_sample:
        return f"{clause} — beyond anything in its {inp.reference_period} {season} sample."
    if inp.percentile is not None:
        return (
            f"{clause} — the {_num(inp.percentile)} percentile of its "
            f"{inp.reference_period} {season} distribution."
        )
    return f"{clause}, an outlier against its {inp.reference_period} {season} distribution."


def _statistical_explanation(inp: TemplateInput) -> str:
    season = _season_adjective(inp.local_date)
    window_span = 2 * inp.window_days + 1
    noun = _metric_noun(inp.metric)

    opening = (
        f"In the {inp.reference_period} reference period, the {window_span} calendar days "
        f"around this date give {inp.baseline_n} {noun} values for {inp.city_name}: "
        f"median {_num(inp.baseline_median)} {inp.unit}"
    )
    if inp.baseline_p25 is not None and inp.baseline_p75 is not None:
        opening += (
            f", middle half {_num(inp.baseline_p25)} to {_num(inp.baseline_p75)} {inp.unit}"
        )
    parts = [opening + "."]

    if inp.metric == Metric.PRECIPITATION and inp.dry_day_fraction is not None:
        parts.append(
            f"{_num(inp.dry_day_fraction * 100.0)}% of those {season} days were completely dry, "
            f"so the reading is measured against the wet-day distribution."
        )

    if inp.tail_probability is not None:
        if inp.beyond_baseline_sample:
            # Quoting a percentile of 100 here would be arithmetically true and
            # rhetorically misleading, so the sentence says what actually happened.
            parts.append(
                f"At {_num(inp.observed_value)} {inp.unit} the reading sits outside every value "
                f"in that sample, so the empirical probability of a day at least this extreme "
                f"is at most {_prob(inp.tail_probability)} — a bound set by the sample size, "
                f"not a measured frequency."
            )
        elif inp.percentile is not None:
            parts.append(
                f"At {_num(inp.observed_value)} {inp.unit} it falls on the "
                f"{_num(inp.percentile)} percentile of that distribution, an empirical "
                f"probability of {_prob(inp.tail_probability)} for a day at least this extreme "
                f"at this point in the calendar."
            )

    if inp.return_period_years is not None and inp.return_period_years >= 1.0:
        parts.append(
            f"That is roughly one occurrence every "
            f"{_num(inp.return_period_years, 0)} years in this seasonal window."
        )

    return " ".join(parts)


def _historical_context(inp: TemplateInput) -> str:
    above = inp.direction == Direction.ABOVE
    extreme = inp.baseline_max if above else inp.baseline_min
    word = "highest" if above else "lowest"

    if inp.beyond_baseline_sample and extreme is not None:
        verb = "reached" if above else "fell as low as"
        return (
            f"No day in this seasonal window between {inp.reference_period.replace('-', ' and ')} "
            f"{verb} {_num(inp.observed_value)} {inp.unit}: the {word} value among the "
            f"{inp.baseline_n} samples is {_num(extreme)} {inp.unit}. That makes this the far "
            f"edge of what the reference sample contains, which is also why the probability "
            f"above is quoted as a bound."
        )

    if inp.baseline_min is not None and inp.baseline_max is not None:
        tail = "upper" if above else "lower"
        return (
            f"The same seasonal window across {inp.reference_period} ranges from "
            f"{_num(inp.baseline_min)} to {_num(inp.baseline_max)} {inp.unit}, so this "
            f"reading stays inside the historical range while sitting well out in its "
            f"{tail} tail."
        )

    return (
        f"The {inp.reference_period} reference sample for this seasonal window holds "
        f"{inp.baseline_n} values, and this reading sits in its "
        f"{'upper' if above else 'lower'} tail."
    )


def _caveats(inp: TemplateInput) -> str:
    parts = [
        f"The value is {_observation_type_phrase(inp.observation_type)} for the model grid cell "
        f"nearest {inp.city_name}, not a reading from an instrument inside the city."
    ]
    if inp.data_tier == DataTier.PROVISIONAL.value:
        parts.append(
            "It comes from the provisional operational analysis rather than the settled "
            "reanalysis, so it may shift slightly when the archive catches up."
        )
    if not inp.baseline_sufficient:
        parts.append(
            f"The reference sample for this window is thin at {inp.baseline_n} values, so the "
            "percentile should be read as indicative."
        )
    parts.append(
        f"This is a statistical comparison against the {inp.reference_period} seasonal "
        "distribution: it is not an official record, and it does not attempt to say which "
        "atmospheric conditions produced the reading."
    )
    return " ".join(parts)


def _confidence(inp: TemplateInput) -> str:
    if not inp.baseline_sufficient or inp.data_quality != "ok":
        return "low"
    if inp.tail_probability_is_bounded:
        return "medium"
    return "high"


def _evidence(inp: TemplateInput) -> list[EvidenceItem]:
    above = inp.direction == Direction.ABOVE
    rows: list[tuple[str, float | int | None, str | None]] = [
        ("Observed value", inp.observed_value, inp.unit),
        ("Seasonal median", inp.baseline_median, inp.unit),
        ("Deviation from baseline", inp.deviation, inp.unit),
        ("Percentile in reference distribution", inp.percentile, "%"),
        ("Empirical tail probability", inp.tail_probability, None),
        (
            f"Reference sample {'maximum' if above else 'minimum'}",
            inp.baseline_max if above else inp.baseline_min,
            inp.unit,
        ),
        ("Reference sample size", inp.baseline_n, "days"),
        ("Anomaly score", inp.anomaly_score, None),
    ]
    return [
        EvidenceItem(
            label=label,
            value=round(value, 4) if isinstance(value, float) else value,
            unit=unit,
            source_tool=TEMPLATE_SOURCE,
        )
        for label, value, unit in rows
        if value is not None
    ][:8]


def render_template(inp: TemplateInput) -> EventExplanation:
    """Build a complete, grounded explanation with no model involved."""
    return EventExplanation(
        headline=_headline(inp),
        statistical_explanation=_statistical_explanation(inp),
        historical_context=_historical_context(inp),
        caveats=_caveats(inp),
        evidence=_evidence(inp),
        confidence=_confidence(inp),
    )


# ---------------------------------------------------------------------------
# ORM adapter
# ---------------------------------------------------------------------------


def template_input_from_event(event, city) -> TemplateInput:
    """Build a :class:`TemplateInput` from an :class:`~app.models.AnomalyEvent`."""
    evidence = event.evidence or {}
    # Only the baseline block is read out of the evidence JSON. Every calculated
    # figure is taken from its own column on the event, so the templates and the
    # API cannot disagree about a number.
    baseline = evidence.get("baseline", {}) or {}
    reference_period = baseline.get("reference_period") or "reference period"
    window_days = baseline.get("seasonal_window_days")
    if window_days is None:
        window_days = 7

    return TemplateInput(
        city_name=city.name,
        city_admin=city.admin,
        city_country=city.country,
        metric=Metric(event.metric),
        direction=Direction(event.direction),
        local_date=event.local_date,
        observed_value=event.observed_value,
        unit=event.unit,
        reference_period=reference_period,
        window_days=int(window_days),
        baseline_n=event.baseline_n,
        baseline_sufficient=event.baseline_sufficient,
        baseline_mean=event.baseline_mean,
        baseline_median=event.baseline_median,
        baseline_p25=event.baseline_p25,
        baseline_p75=event.baseline_p75,
        baseline_min=event.baseline_min,
        baseline_max=event.baseline_max,
        deviation=event.deviation,
        percentile=event.percentile,
        tail_probability=event.tail_probability,
        tail_probability_is_bounded=event.tail_probability_is_bounded,
        beyond_baseline_sample=event.beyond_baseline_sample,
        return_period_years=event.return_period_years,
        surprisal=event.surprisal,
        anomaly_score=event.anomaly_score,
        observation_type=event.observation_type,
        data_tier=event.data_tier,
        data_quality=event.data_quality,
        dry_day_fraction=baseline.get("zero_fraction"),
    )
