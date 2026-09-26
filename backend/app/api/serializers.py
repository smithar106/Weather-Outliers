"""ORM rows to API models.

Kept apart from the route handlers so that the shape of a response is defined in
one place regardless of which endpoint returns it — an event embedded in the
top-10 board is the same object as the one at ``/api/events/{id}``, which is what
lets the frontend share a single card component.
"""

from __future__ import annotations

from app.domain import METRIC_LABELS, Metric, category_for
from app.models import (
    AgentExplanation,
    AnomalyEvent,
    BaselineStatistic,
    City,
    PipelineRun,
    WeatherObservation,
)
from app.schemas import (
    BaselineOut,
    CityHistoryPointOut,
    CityOut,
    EventBaselineOut,
    EventCalculationOut,
    EventDetailOut,
    EventOut,
    ExplanationOut,
    ObservationOut,
    RunProvenanceOut,
)


def city_out(city: City) -> CityOut:
    return CityOut.model_validate(city)


def observation_out(obs: WeatherObservation) -> ObservationOut:
    return ObservationOut.model_validate(obs)


def history_point_out(obs: WeatherObservation) -> CityHistoryPointOut:
    return CityHistoryPointOut.model_validate(obs)


def baseline_out(row: BaselineStatistic) -> BaselineOut:
    return BaselineOut(
        metric=row.metric,
        day_of_year=row.day_of_year,
        reference_period=f"{row.reference_start_year}-{row.reference_end_year}",
        seasonal_window_days=row.window_days,
        n_samples=row.n_samples,
        n_years=row.n_years,
        sufficient=row.sufficient,
        mean=row.mean,
        std=row.std,
        median=row.median,
        p25=row.p25,
        p75=row.p75,
        iqr=row.iqr,
        min_value=row.min_value,
        max_value=row.max_value,
        histogram=row.histogram,
        zero_fraction=row.zero_fraction,
        nonzero_n=row.nonzero_n,
        source_dataset=row.source_dataset,
        methodology_version=row.methodology_version,
    )


def explanation_out(row: AgentExplanation | None) -> ExplanationOut | None:
    if row is None:
        return None
    return ExplanationOut(
        headline=row.headline,
        statistical_explanation=row.statistical_explanation,
        historical_context=row.historical_context,
        caveats=row.caveats,
        evidence=row.evidence or [],
        confidence=row.confidence,
        generator=row.generator,
        llm_provider=row.llm_provider,
        model=row.model,
        tool_call_count=row.tool_call_count,
        fallback_reason=row.fallback_reason,
        created_at=row.created_at,
    )


def _event_fields(event: AnomalyEvent, city: City) -> dict:
    metric = Metric(event.metric)
    return {
        "id": event.id,
        "city": city_out(city),
        "local_date": event.local_date,
        "metric": event.metric,
        "metric_label": METRIC_LABELS[metric],
        "category": category_for(metric, event.direction),
        "direction": event.direction,
        "observed_value": event.observed_value,
        "unit": event.unit,
        "baseline": EventBaselineOut(
            mean=event.baseline_mean,
            median=event.baseline_median,
            std=event.baseline_std,
            p25=event.baseline_p25,
            p75=event.baseline_p75,
            min=event.baseline_min,
            max=event.baseline_max,
            n=event.baseline_n,
            sufficient=event.baseline_sufficient,
        ),
        "calculation": EventCalculationOut(
            deviation=event.deviation,
            robust_deviation=event.robust_deviation,
            z_score=event.z_score,
            z_valid=event.z_valid,
            percentile=event.percentile,
            tail_probability=event.tail_probability,
            tail_probability_is_bounded=event.tail_probability_is_bounded,
            beyond_baseline_sample=event.beyond_baseline_sample,
            return_period_years=event.return_period_years,
            surprisal=event.surprisal,
            margin_bonus=event.margin_bonus,
            anomaly_score=event.anomaly_score,
        ),
        "data_tier": event.data_tier,
        "data_quality": event.data_quality,
        "source_dataset": event.source_dataset,
        "observation_type": event.observation_type,
        "methodology_version": event.methodology_version,
    }


def event_out(
    event: AnomalyEvent, city: City, explanation: AgentExplanation | None = None
) -> EventOut:
    return EventOut(
        **_event_fields(event, city), explanation=explanation_out(explanation)
    )


def event_detail_out(
    event: AnomalyEvent, city: City, explanation: AgentExplanation | None = None
) -> EventDetailOut:
    return EventDetailOut(
        **_event_fields(event, city),
        explanation=explanation_out(explanation),
        evidence=event.evidence,
    )


def run_out(run: PipelineRun) -> RunProvenanceOut:
    return RunProvenanceOut(
        run_id=run.id,
        analysis_date=run.analysis_date,
        data_tier=run.data_tier,
        published_at=run.published_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        cities_total=run.cities_total,
        cities_with_data=run.cities_with_data,
        completeness=run.completeness,
        events_total=run.events_total,
        events_published=run.events_published,
        methodology_version=run.methodology_version,
        registry_version=run.registry_version,
        llm_calls=run.llm_calls,
        llm_estimated_usd=run.llm_estimated_usd,
        llm_budget_exhausted=run.llm_budget_exhausted,
    )
