"""Read-only public endpoints.

Every handler here reads precomputed rows. Nothing in this module calls a weather
provider, runs a statistical calculation, or invokes a model — that all happened
once, in the scheduled pipeline, before publication. A thousand simultaneous
visitors produce a thousand indexed SELECTs and zero external requests.

One behaviour is worth stating plainly because it is a product decision, not an
implementation detail: when the requested date has no published run, the API
serves the most recent published run and sets ``is_latest_available=false`` with
``requested_date`` filled in. A failed pipeline therefore shows yesterday's
analysis, correctly labelled, rather than an empty page.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import monitoring
from app.agent.investigator import month_to_date_budget
from app.agent.llm import LLMConfigError, get_llm_client
from app.api.deps import Page, history_range, pagination, parse_iso_date
from app.api.serializers import (
    baseline_out,
    city_out,
    event_detail_out,
    event_out,
    history_point_out,
    observation_out,
    run_out,
)
from app.chat import CHAT_PATH, answer_agent_question, answer_question
from app.config import Settings, get_settings
from app.db import get_db
from app.domain import METHODOLOGY_VERSION, RunStatus
from app.models import (
    AgentExplanation,
    AnomalyEvent,
    BaselineStatistic,
    City,
    DailyRanking,
    PipelineRun,
    WeatherObservation,
)
from app.provenance import LIMITATIONS, RANKING_BASIS, data_sources, methodology
from app.schemas import (
    AgentChatRequest,
    AgentChatResponse,
    ArchiveEntryOut,
    ArchiveOut,
    ChatRequest,
    ChatResponse,
    CityDetailOut,
    CityHistoryOut,
    CityListOut,
    EventDetailOut,
    HealthOut,
    MethodologyOut,
    MonitorEvalsOut,
    MonitorRunsOut,
    MonitorTraceDetailOut,
    MonitorTracesOut,
    RankedEventOut,
    RankingsOut,
)
from app.stats.seasonal import noleap_day_of_year

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Shared queries
# ---------------------------------------------------------------------------


def _latest_published_run(session: Session, on_date: date | None = None) -> PipelineRun | None:
    """The newest published, successful run, optionally pinned to one date."""
    stmt = (
        select(PipelineRun)
        .where(
            PipelineRun.published.is_(True),
            PipelineRun.status == RunStatus.SUCCEEDED.value,
        )
        .order_by(PipelineRun.analysis_date.desc(), PipelineRun.published_at.desc())
        .limit(1)
    )
    if on_date is not None:
        stmt = stmt.where(PipelineRun.analysis_date == on_date)
    return session.execute(stmt).scalars().first()


def _explanations_for(session: Session, run_id: str, event_ids: list[str]) -> dict:
    """Explanations keyed by event id, fetched in one query rather than per card."""
    if not event_ids:
        return {}
    rows = (
        session.execute(
            select(AgentExplanation).where(
                AgentExplanation.run_id == run_id,
                AgentExplanation.event_id.in_(event_ids),
            )
        )
        .scalars()
        .all()
    )
    return {row.event_id: row for row in rows}


def _set_cache(response: Response, settings: Settings, *, public: bool = True) -> None:
    """Published results are immutable until the next run, so they cache well."""
    if settings.http_cache_seconds <= 0:
        response.headers["Cache-Control"] = "no-store"
        return
    scope = "public" if public else "private"
    response.headers["Cache-Control"] = (
        f"{scope}, max-age={settings.http_cache_seconds}, "
        f"stale-while-revalidate={settings.http_cache_seconds * 2}"
    )


def _build_rankings(
    session: Session,
    run: PipelineRun,
    settings: Settings,
    *,
    requested_date: date | None = None,
) -> RankingsOut:
    rows = (
        session.execute(
            select(DailyRanking, AnomalyEvent, City)
            .join(AnomalyEvent, DailyRanking.event_id == AnomalyEvent.id)
            .join(City, AnomalyEvent.city_id == City.id)
            .where(DailyRanking.run_id == run.id)
            .order_by(DailyRanking.rank)
        )
        .tuples()
        .all()
    )
    explanations = _explanations_for(session, run.id, [e.id for _, e, _ in rows])

    events = [
        RankedEventOut(
            rank=ranking.rank,
            score=ranking.score,
            event=event_out(event, city, explanations.get(event.id)),
        )
        for ranking, event, city in rows
    ]

    is_latest = requested_date is None or requested_date == run.analysis_date
    return RankingsOut(
        analysis_date=run.analysis_date,
        published_at=run.published_at,
        methodology_version=run.methodology_version,
        ranking_basis=RANKING_BASIS,
        one_event_per_city=settings.ranking_one_event_per_city,
        count=len(events),
        events=events,
        run=run_out(run),
        data_sources=data_sources(settings),
        is_latest_available=is_latest,
        requested_date=requested_date if not is_latest else None,
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@router.get("/health", response_model=HealthOut, tags=["meta"])
def health(
    response: Response,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HealthOut:
    """Liveness and freshness in one payload.

    ``status`` is ``degraded`` rather than a 5xx when the database is reachable
    but stale, because a stale site is still a working site and Railway should not
    restart a healthy container over it. Alerting reads
    ``hours_since_publish``.
    """
    response.headers["Cache-Control"] = "no-store"

    try:
        city_count = session.execute(select(func.count(City.id))).scalar_one()
        db_state = "ok"
    except Exception as exc:  # pragma: no cover - exercised by killing the DB
        logger.error("health check database failure: %s", exc)
        return HealthOut(
            status="degraded",
            environment=settings.environment,
            methodology_version=METHODOLOGY_VERSION,
            database="unreachable",
            llm_enabled=settings.llm_enabled,
        )

    run = _latest_published_run(session)
    hours = None
    if run is not None and run.published_at is not None:
        published = run.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        hours = round((datetime.now(UTC) - published).total_seconds() / 3600.0, 2)

    return HealthOut(
        status="ok" if run is not None else "degraded",
        environment=settings.environment,
        methodology_version=METHODOLOGY_VERSION,
        database=db_state,
        cities=city_count,
        latest_published_date=run.analysis_date if run else None,
        latest_published_at=run.published_at if run else None,
        hours_since_publish=hours,
        llm_enabled=settings.llm_enabled,
    )


# ---------------------------------------------------------------------------
# Rankings
# ---------------------------------------------------------------------------


@router.get("/api/rankings/latest", response_model=RankingsOut, tags=["rankings"])
def rankings_latest(
    response: Response,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RankingsOut:
    """The most recent published board."""
    run = _latest_published_run(session)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No analysis has been published yet. Run the pipeline first.",
        )
    _set_cache(response, settings)
    return _build_rankings(session, run, settings)


@router.get("/api/rankings/{analysis_date}", response_model=RankingsOut, tags=["rankings"])
def rankings_for_date(
    response: Response,
    analysis_date: str = Path(description="ISO date, YYYY-MM-DD."),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RankingsOut:
    """The board for one date, falling back to the latest published run.

    The fallback is what keeps the site useful when a daily run fails: the
    response is clearly marked with ``is_latest_available=false`` and the date the
    caller actually asked for.
    """
    requested = parse_iso_date(analysis_date, "analysis_date")
    run = _latest_published_run(session, requested) or _latest_published_run(session)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No analysis has been published yet.",
        )
    _set_cache(response, settings)
    return _build_rankings(session, run, settings, requested_date=requested)


@router.get("/api/rankings", response_model=ArchiveOut, tags=["rankings"])
def rankings_archive(
    response: Response,
    page: Page = Depends(pagination),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ArchiveOut:
    """Published analysis dates, newest first, for the historical archive."""
    published = (
        select(PipelineRun.id, PipelineRun.analysis_date, PipelineRun.published_at,
               PipelineRun.methodology_version)
        .where(
            PipelineRun.published.is_(True),
            PipelineRun.status == RunStatus.SUCCEEDED.value,
        )
        .order_by(PipelineRun.analysis_date.desc())
    )
    total = session.execute(
        select(func.count()).select_from(published.subquery())
    ).scalar_one()

    runs = session.execute(published.limit(page.limit).offset(page.offset)).tuples().all()

    entries: list[ArchiveEntryOut] = []
    for run_id, analysis_date, published_at, version in runs:
        count = session.execute(
            select(func.count(DailyRanking.id)).where(DailyRanking.run_id == run_id)
        ).scalar_one()
        top = (
            session.execute(
                select(City.name, AnomalyEvent.metric, DailyRanking.score)
                .join(AnomalyEvent, DailyRanking.event_id == AnomalyEvent.id)
                .join(City, AnomalyEvent.city_id == City.id)
                .where(DailyRanking.run_id == run_id, DailyRanking.rank == 1)
            )
            .tuples()
            .first()
        )
        entries.append(
            ArchiveEntryOut(
                analysis_date=analysis_date,
                published_at=published_at,
                methodology_version=version,
                event_count=count,
                top_city=top[0] if top else None,
                top_metric=top[1] if top else None,
                top_score=top[2] if top else None,
            )
        )

    _set_cache(response, settings)
    return ArchiveOut(
        count=len(entries),
        total=total,
        limit=page.limit,
        offset=page.offset,
        entries=entries,
    )


# ---------------------------------------------------------------------------
# Cities
# ---------------------------------------------------------------------------


@router.get("/api/cities", response_model=CityListOut, tags=["cities"])
def list_cities(
    response: Response,
    country: str | None = Query(None, min_length=2, max_length=2, description="ISO code."),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CityListOut:
    """The curated registry. Small and fixed, so it is returned whole."""
    stmt = select(City).where(City.is_active.is_(True)).order_by(City.country, City.name)
    if country:
        stmt = stmt.where(City.country == country.upper())
    cities = session.execute(stmt).scalars().all()

    _set_cache(response, settings)
    return CityListOut(
        registry_version=cities[0].registry_version if cities else "unknown",
        count=len(cities),
        cities=[city_out(c) for c in cities],
    )


@router.get("/api/cities/{city_id}", response_model=CityDetailOut, tags=["cities"])
def city_detail(
    response: Response,
    city_id: str = Path(max_length=64),
    analysis_date: str | None = Query(None, description="ISO date. Defaults to latest."),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CityDetailOut:
    """One city on one date: the reading, its seasonal baselines, its events."""
    city = session.get(City, city_id)
    if city is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown city '{city_id}'"
        )

    if analysis_date:
        target = parse_iso_date(analysis_date, "analysis_date")
        run = _latest_published_run(session, target)
    else:
        run = _latest_published_run(session)
        target = run.analysis_date if run else None

    observation = None
    events = []
    baselines = []

    if target is not None:
        observation = (
            session.execute(
                select(WeatherObservation)
                .where(
                    WeatherObservation.city_id == city_id,
                    WeatherObservation.local_date == target,
                )
                .order_by(WeatherObservation.retrieved_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )

        event_rows = (
            session.execute(
                select(AnomalyEvent)
                .where(
                    AnomalyEvent.city_id == city_id,
                    AnomalyEvent.local_date == target,
                    AnomalyEvent.methodology_version == METHODOLOGY_VERSION,
                )
                .order_by(AnomalyEvent.anomaly_score.desc())
            )
            .scalars()
            .all()
        )
        explanations = _explanations_for(
            session, run.id if run else "", [e.id for e in event_rows]
        )
        events = [event_out(e, city, explanations.get(e.id)) for e in event_rows]

        doy = noleap_day_of_year(target)
        baselines = [
            baseline_out(row)
            for row in session.execute(
                select(BaselineStatistic)
                .where(
                    BaselineStatistic.city_id == city_id,
                    BaselineStatistic.day_of_year == doy,
                    BaselineStatistic.methodology_version == METHODOLOGY_VERSION,
                )
                .order_by(BaselineStatistic.metric)
            )
            .scalars()
            .all()
        ]

    _set_cache(response, settings)
    return CityDetailOut(
        city=city_out(city),
        analysis_date=target,
        latest_observation=observation_out(observation) if observation else None,
        baselines=baselines,
        events=events,
        limitations=list(LIMITATIONS[:3]),
    )


@router.get("/api/cities/{city_id}/history", response_model=CityHistoryOut, tags=["cities"])
def city_history(
    response: Response,
    city_id: str = Path(max_length=64),
    window: tuple[date, date] = Depends(history_range),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CityHistoryOut:
    """A bounded daily time series for the trend chart.

    The range is clamped by ``history_range`` before a row is touched, so there is
    no request shape that scans a city's full archive.
    """
    city = session.get(City, city_id)
    if city is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown city '{city_id}'"
        )

    start, end = window
    rows = (
        session.execute(
            select(WeatherObservation)
            .where(
                WeatherObservation.city_id == city_id,
                WeatherObservation.local_date >= start,
                WeatherObservation.local_date <= end,
            )
            .order_by(WeatherObservation.local_date)
        )
        .scalars()
        .all()
    )

    # One row per date: prefer the settled reanalysis over a provisional estimate.
    by_date: dict[date, WeatherObservation] = {}
    for row in rows:
        existing = by_date.get(row.local_date)
        if existing is None or (
            existing.data_tier != "final" and row.data_tier == "final"
        ):
            by_date[row.local_date] = row

    ordered = [by_date[d] for d in sorted(by_date)]
    _set_cache(response, settings)
    return CityHistoryOut(
        city=city_out(city),
        start_date=start,
        end_date=end,
        count=len(ordered),
        points=[history_point_out(r) for r in ordered],
        source_datasets=sorted({r.source_dataset for r in ordered}),
    )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@router.get("/api/events/{event_id}", response_model=EventDetailOut, tags=["events"])
def event_detail(
    response: Response,
    event_id: str = Path(max_length=160),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> EventDetailOut:
    """One event with its complete calculation trace.

    This is the endpoint that makes the numbers on the site auditable: everything
    the ranking and the explanation were derived from is here.
    """
    event = session.get(AnomalyEvent, event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown event '{event_id}'"
        )
    city = session.get(City, event.city_id)
    if city is None:  # pragma: no cover - FK prevents this
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Event has no city"
        )

    explanation = (
        session.execute(
            select(AgentExplanation)
            .where(AgentExplanation.event_id == event_id)
            .order_by(AgentExplanation.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )

    _set_cache(response, settings)
    return event_detail_out(event, city, explanation)


# ---------------------------------------------------------------------------
# Methodology
# ---------------------------------------------------------------------------


@router.get("/api/methodology", response_model=MethodologyOut, tags=["meta"])
def get_methodology(
    response: Response,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MethodologyOut:
    """Formulas, bounds, sources, and limitations, as served by this deployment.

    Generated from live configuration rather than written prose, so the
    methodology page cannot drift from the code that produced the numbers.
    """
    registry_version = session.execute(
        select(City.registry_version).limit(1)
    ).scalar_one_or_none()
    _set_cache(response, settings)
    return MethodologyOut.model_validate(methodology(settings, registry_version))


# ---------------------------------------------------------------------------
# Chat (NL→SQL)
# ---------------------------------------------------------------------------


@router.post(CHAT_PATH, response_model=ChatResponse, tags=["chat"])
def chat(
    request: Request,
    payload: ChatRequest,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ChatResponse:
    """Turn a natural-language question into a read-only SQL query and an answer.

    This is the one endpoint that calls a language model and therefore costs
    money per request, so it is off unless ``CHAT_ENABLED`` is set and, when
    ``CHAT_API_KEY`` is configured, requires the matching ``X-API-Key`` header.
    """
    if not settings.chat_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat is not enabled (set CHAT_ENABLED=true).",
        )
    if settings.chat_api_key and request.headers.get("x-api-key") != settings.chat_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key.",
        )
    if not settings.llm_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No LLM provider is configured; chat is unavailable.",
        )
    try:
        client = get_llm_client(settings)
    except LLMConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    if client is None:  # pragma: no cover - llm_enabled guards this
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No LLM provider is configured.",
        )

    budget = month_to_date_budget(session, settings)
    if budget.exhausted:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=budget.reason or "The monthly AI budget has been reached.",
        )

    try:
        result = answer_question(
            payload.question, client=client, session=session, max_rows=settings.chat_max_rows
        )
    finally:
        client.close()

    return ChatResponse(
        question=result.question,
        answer=result.answer,
        sql=result.sql,
        explanation=result.explanation,
        columns=list(result.columns),
        rows=[list(row) for row in result.rows],
        row_count=len(result.rows),
        truncated=result.truncated,
        refused=result.refused,
    )


@router.post("/api/agent", response_model=AgentChatResponse, tags=["chat"])
def agent_chat(
    request: Request,
    payload: AgentChatRequest,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AgentChatResponse:
    """Answer a question about the agentic flow from recent MLflow traces.

    Same gating and budget as the data chat, but grounded in the tracing store
    rather than the application database.
    """
    if not settings.chat_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat is not enabled (set CHAT_ENABLED=true).",
        )
    if settings.chat_api_key and request.headers.get("x-api-key") != settings.chat_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key.",
        )
    if not settings.llm_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No LLM provider is configured; chat is unavailable.",
        )
    try:
        client = get_llm_client(settings)
    except LLMConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    if client is None:  # pragma: no cover - llm_enabled guards this
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No LLM provider is configured.",
        )

    budget = month_to_date_budget(session, settings)
    if budget.exhausted:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=budget.reason or "The monthly AI budget has been reached.",
        )

    try:
        result = answer_agent_question(payload.question, client=client, settings=settings)
    finally:
        client.close()

    return AgentChatResponse(
        question=payload.question,
        answer=result["answer"],
        trace_count=result["trace_count"],
        available=result["available"],
        note=result["note"],
    )


# ---------------------------------------------------------------------------
# Monitoring (read-only pipeline runs, evaluations, and MLflow traces)
# ---------------------------------------------------------------------------


@router.get("/api/monitor/runs", response_model=MonitorRunsOut, tags=["monitor"])
def monitor_runs(
    response: Response,
    limit: int = Query(20, ge=1, le=200, description="Runs to return, newest first."),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MonitorRunsOut:
    """Recent pipeline runs: status, completeness, and LLM usage, newest first."""
    _set_cache(response, settings)
    runs = monitoring.recent_runs(session, limit=limit)
    return MonitorRunsOut(count=len(runs), runs=runs)


@router.get("/api/monitor/evals", response_model=MonitorEvalsOut, tags=["monitor"])
def monitor_evals(
    response: Response,
    limit: int = Query(20, ge=1, le=200, description="Reports to return, newest first."),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MonitorEvalsOut:
    """Recent evaluation reports, newest first."""
    _set_cache(response, settings)
    reports = monitoring.recent_evals(session, limit=limit)
    return MonitorEvalsOut(count=len(reports), reports=reports)


@router.get("/api/monitor/traces", response_model=MonitorTracesOut, tags=["monitor"])
def monitor_traces(
    response: Response,
    limit: int = Query(50, ge=1, le=200, description="Traces to return, newest first."),
    settings: Settings = Depends(get_settings),
) -> MonitorTracesOut:
    """MLflow traces for the pipeline experiment.

    When the tracking store is not configured or reachable, ``available`` is
    false and ``note`` says why — the list is empty rather than invented.
    """
    response.headers["Cache-Control"] = "no-store"
    payload = monitoring.list_traces(settings, limit=limit)
    return MonitorTracesOut(**payload)


@router.get(
    "/api/monitor/traces/{trace_id}", response_model=MonitorTraceDetailOut, tags=["monitor"]
)
def monitor_trace_detail(
    response: Response,
    trace_id: str = Path(description="MLflow trace id (e.g. tr-…)."),
    settings: Settings = Depends(get_settings),
) -> MonitorTraceDetailOut:
    """One trace's span tree, with each span's status, latency and attributes."""
    response.headers["Cache-Control"] = "no-store"
    payload = monitoring.get_trace(settings, trace_id)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trace {trace_id!r} not found (or the tracking store is unreachable).",
        )
    spans = payload.pop("spans", [])
    return MonitorTraceDetailOut(**payload, spans=spans)
