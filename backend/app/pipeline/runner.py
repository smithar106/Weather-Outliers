"""The scheduled pipeline.

This is where the project's central operational guarantee lives: **a failed run
never replaces a good one.** Every run writes into the database as it goes, but
nothing becomes visible to the public API until the very last statement sets
``published = true`` on the run row. The API only ever reads published,
successful runs, so a crash at step four leaves yesterday's board serving
unchanged, with its own timestamps intact.

The shape of a run:

1. Resolve which local calendar date to analyse, per city, in its own IANA zone.
2. Fetch that day from the provider.
3. Validate completeness and record what was missing rather than dropping it.
4. Load cached baselines. Decades of history are never re-downloaded here.
5. Compute anomalies for every city-metric pair.
6. Rank them into a board with deterministic tie-breaking.
7. Investigate the top events, within bounded LLM cost.
8. Publish atomically.

Reruns are idempotent because event identity is a pure function of date, city,
metric, and methodology version — step 5 updates rows rather than inserting
duplicates, and the previous run's ranking rows for the same date are replaced
wholesale inside the same transaction that publishes the new ones.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.agent.investigator import investigate_events
from app.agent.schemas import InvestigationOutcome
from app.cities.registry import active_cities, sync_registry
from app.config import Settings, get_settings
from app.domain import (
    ALL_METRICS,
    METHODOLOGY_VERSION,
    DataQuality,
    DataTier,
    Metric,
    RunKind,
    RunStatus,
    build_event_id,
)
from app.ingest.baselines import (
    baseline_coverage,
    build_city_baselines,
    load_baselines_for_date,
)
from app.ingest.observations import observations_for_date, upsert_observations
from app.models import (
    AgentExplanation,
    AnomalyEvent,
    City,
    DailyRanking,
    PipelineRun,
    WeatherObservation,
)
from app.providers import (
    ProviderBudgetExhausted,
    ProviderError,
    WeatherProvider,
    get_provider,
)
from app.stats.anomaly import AnomalyCandidate, compute_anomaly
from app.stats.ranking import rank_candidates
from app.stats.seasonal import is_local_date_complete, latest_eligible_local_date

logger = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    """A run could not complete. The previous published board is untouched."""


@dataclass(slots=True)
class CityOutcome:
    """What happened to one city on one date. Kept for the run's diagnostics."""

    city_id: str
    local_date: date | None = None
    fetched: bool = False
    quality: str = DataQuality.MISSING.value
    missing_fields: list[str] = field(default_factory=list)
    baselines_found: int = 0
    candidates: int = 0
    error: str | None = None


@dataclass(slots=True)
class RunReport:
    """The result of a run, in the form the CLI prints and the tests assert on."""

    run_id: str
    kind: str
    analysis_date: date | None
    status: str
    published: bool
    duration_ms: int
    cities_total: int = 0
    cities_with_data: int = 0
    completeness: float = 0.0
    events_total: int = 0
    events_published: int = 0
    events_excluded: int = 0
    provider_requests: int = 0
    provider_errors: int = 0
    llm_calls: int = 0
    llm_generated: int = 0
    template_generated: int = 0
    llm_estimated_usd: float = 0.0
    llm_budget_exhausted: bool = False
    data_tier: str | None = None
    error: str | None = None
    city_outcomes: list[CityOutcome] = field(default_factory=list)

    def summary(self) -> str:
        head = (
            f"run {self.run_id} ({self.kind}) {self.status} "
            f"date={self.analysis_date} published={self.published} "
            f"in {self.duration_ms}ms"
        )
        if self.status != RunStatus.SUCCEEDED.value:
            return f"{head}\n  error: {self.error}"
        if self.kind == RunKind.BASELINES.value:
            rows = sum(o.baselines_found for o in self.city_outcomes)
            return (
                f"{head}\n"
                f"  cities:       {self.cities_with_data} built, "
                f"{self.cities_total - self.cities_with_data} already cached\n"
                f"  baselines:    {rows} rows written\n"
                f"  provider:     {self.provider_requests} requests, "
                f"{self.provider_errors} errors"
            )
        return (
            f"{head}\n"
            f"  cities:       {self.cities_with_data}/{self.cities_total} "
            f"({self.completeness:.1%} complete)\n"
            f"  events:       {self.events_published} published, "
            f"{self.events_total} computed, {self.events_excluded} ineligible\n"
            f"  provider:     {self.provider_requests} requests, "
            f"{self.provider_errors} errors\n"
            f"  explanations: {self.llm_generated} model-written, "
            f"{self.template_generated} deterministic "
            f"({self.llm_calls} llm calls, ~${self.llm_estimated_usd:.4f})"
        )


# ---------------------------------------------------------------------------
# Date resolution
# ---------------------------------------------------------------------------


def resolve_analysis_date(
    cities: list[City],
    settings: Settings,
    *,
    now_utc: datetime | None = None,
) -> date:
    """The most recent local date that is complete *everywhere* in the registry.

    Cities span roughly seven hours of longitude, so "yesterday" is not one date.
    Taking the minimum across the registry is the only choice that keeps a single
    board internally comparable: every city on it is being measured over the same
    calendar day, and none of them contributes a partial one.
    """
    now_utc = now_utc or datetime.now(UTC)
    if not cities:
        raise PipelineError("no active cities in the registry; run seed-cities first")

    eligible = [
        latest_eligible_local_date(
            ZoneInfo(city.timezone), now_utc, settings.pipeline_source_lag_hours
        )
        for city in cities
    ]
    return min(eligible)


def _tier_for(analysis_date: date, settings: Settings, *, today: date) -> DataTier:
    """Reanalysis if the archive has caught up, otherwise the provisional analysis."""
    if (today - analysis_date).days >= settings.pipeline_finalize_lag_days:
        return DataTier.FINAL
    return DataTier.PROVISIONAL


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


class Pipeline:
    """One invocation. Construct, call :meth:`run`, then :meth:`close`."""

    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        provider: WeatherProvider | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self._provider = provider
        self._owns_provider = provider is None

    @property
    def provider(self) -> WeatherProvider:
        if self._provider is None:
            self._provider = get_provider(self.settings)
        return self._provider

    def close(self) -> None:
        if self._owns_provider and self._provider is not None:
            self._provider.close()
            self._provider = None

    # -- public entry points -------------------------------------------------

    def run_daily(
        self,
        analysis_date: date | None = None,
        *,
        kind: RunKind = RunKind.DAILY,
        force_tier: DataTier | None = None,
        skip_explanations: bool = False,
        now_utc: datetime | None = None,
    ) -> RunReport:
        """Analyse one date and publish it, or fail without publishing anything."""
        started = time.perf_counter()
        now_utc = now_utc or datetime.now(UTC)
        run = self._open_run(kind)
        report = RunReport(
            run_id=run.id,
            kind=kind.value,
            analysis_date=analysis_date,
            status=RunStatus.RUNNING.value,
            published=False,
            duration_ms=0,
        )

        try:
            cities = active_cities(self.session)
            if analysis_date is None:
                analysis_date = resolve_analysis_date(
                    cities, self.settings, now_utc=now_utc
                )
            report.analysis_date = analysis_date
            run.analysis_date = analysis_date

            tier = force_tier or _tier_for(
                analysis_date, self.settings, today=now_utc.date()
            )
            run.data_tier = tier.value
            report.data_tier = tier.value
            self.session.commit()

            logger.info(
                "run %s analysing %s across %d cities at tier=%s",
                run.id,
                analysis_date,
                len(cities),
                tier.value,
            )

            outcomes = self._fetch(cities, analysis_date, tier, report, now_utc=now_utc)
            self._require_completeness(report, len(cities))
            candidates = self._compute(cities, analysis_date, outcomes, report)
            ranked = self._rank(candidates, report)
            self._persist_events(run, candidates, ranked, analysis_date, report)

            if not skip_explanations:
                self._explain(run, ranked, report)

            self._publish(run, analysis_date, ranked, report, started)
            return report

        except Exception as exc:
            self.session.rollback()
            report.status = RunStatus.FAILED.value
            report.published = False
            report.error = f"{type(exc).__name__}: {exc}"
            report.duration_ms = int((time.perf_counter() - started) * 1000)
            self._fail_run(run.id, report, exc)
            logger.error("run %s failed: %s", run.id, report.error)
            return report

    def build_baselines(
        self, *, city_ids: list[str] | None = None, force: bool = False
    ) -> RunReport:
        """Populate the climatology cache. Slow, network-heavy, and run rarely.

        Separated from the daily run on purpose: a daily job that could
        accidentally re-download thirty years of history for fifty cities is a
        daily job that will eventually do it.
        """
        started = time.perf_counter()
        run = self._open_run(RunKind.BASELINES)
        report = RunReport(
            run_id=run.id,
            kind=RunKind.BASELINES.value,
            analysis_date=None,
            status=RunStatus.RUNNING.value,
            published=False,
            duration_ms=0,
        )
        try:
            cities = active_cities(self.session)
            if city_ids:
                wanted = set(city_ids)
                cities = [c for c in cities if c.id in wanted]
                missing = wanted - {c.id for c in cities}
                if missing:
                    raise PipelineError(f"unknown city ids: {sorted(missing)}")

            report.cities_total = len(cities)
            for city in cities:
                try:
                    stats = build_city_baselines(
                        self.session, city, self.provider, self.settings, force=force
                    )
                except ProviderBudgetExhausted as exc:
                    # Not this city's failure — the whole free-tier window is
                    # spent, so every remaining city would fail identically.
                    # Stop, keep what is already committed, and report the
                    # resume command rather than logging fifty copies of the
                    # same error.
                    report.provider_errors += 1
                    report.city_outcomes.append(
                        CityOutcome(city_id=city.id, error=f"{type(exc).__name__}: {exc}")
                    )
                    remaining = len(cities) - len(report.city_outcomes)
                    logger.warning(
                        "provider budget exhausted at %s with %d cities not yet attempted: %s "
                        "Baselines are cached per city, so rerunning `pipeline build-baselines` "
                        "once the window reopens continues where this stopped.",
                        city.id,
                        remaining,
                        exc,
                    )
                    break
                except ProviderError as exc:
                    report.provider_errors += 1
                    report.city_outcomes.append(
                        CityOutcome(city_id=city.id, error=f"{type(exc).__name__}: {exc}")
                    )
                    logger.error("baselines failed for %s: %s", city.id, exc)
                    continue

                report.provider_requests += int(stats.get("provider_requests", 0))
                if stats.get("skipped"):
                    logger.info("baselines already present for %s", city.id)
                else:
                    report.cities_with_data += 1
                    logger.info(
                        "baselines built for %s: %d rows from %d reference days",
                        city.id,
                        int(stats.get("rows", 0)),
                        int(stats.get("days_fetched", 0)),
                    )
                report.city_outcomes.append(
                    CityOutcome(
                        city_id=city.id,
                        fetched=not stats.get("skipped", False),
                        baselines_found=int(stats.get("rows", 0)),
                    )
                )
                # Commit per city, not once at the end. Cities are independent
                # here — unlike a daily board, a half-built climatology is not
                # inconsistent, just incomplete, and the next run skips what is
                # already there. Holding all fifty in one transaction meant an
                # interrupted build discarded every city it had already fetched,
                # along with the free-tier quota those requests cost.
                self.session.commit()

            # Reported separately from the request count because the two differ by
            # two orders of magnitude on this endpoint, and only the weighted
            # figure can be compared to a published free-tier allowance.
            spent = getattr(self.provider, "call_weight_spent", None)
            if spent:
                logger.info(
                    "provider cost: %d requests ≈ %.0f weighted API calls "
                    "(free tier allows 10,000/day)",
                    report.provider_requests,
                    spent,
                )

            # A baseline build is infrastructure, not a publication: it has
            # nothing for the site to show, so it never sets published.
            report.status = RunStatus.SUCCEEDED.value
            report.duration_ms = int((time.perf_counter() - started) * 1000)
            self._close_run(
                run.id,
                report,
                status=RunStatus.SUCCEEDED,
                published=False,
            )
            return report

        except Exception as exc:
            self.session.rollback()
            report.status = RunStatus.FAILED.value
            report.error = f"{type(exc).__name__}: {exc}"
            report.duration_ms = int((time.perf_counter() - started) * 1000)
            self._fail_run(run.id, report, exc)
            return report

    # -- steps ---------------------------------------------------------------

    def _open_run(self, kind: RunKind) -> PipelineRun:
        run = PipelineRun(
            id=f"{kind.value}-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}",
            kind=kind.value,
            status=RunStatus.RUNNING.value,
            published=False,
            started_at=datetime.now(UTC),
            methodology_version=METHODOLOGY_VERSION,
            config_snapshot=self._config_snapshot(),
        )
        self.session.add(run)
        self.session.commit()
        return run

    def _config_snapshot(self) -> dict:
        """The settings that affect results, stored with the run.

        Reproducibility means being able to say what the numbers were computed
        under. Deliberately excludes every credential.
        """
        s = self.settings
        return {
            "methodology_version": METHODOLOGY_VERSION,
            "reference_period": s.reference_period_label,
            "seasonal_window_days": s.baseline_seasonal_window_days,
            "baseline_min_samples": s.baseline_min_samples,
            "baseline_min_years": s.baseline_min_years,
            "baseline_min_wet_days": s.baseline_min_wet_days,
            "ranking_top_n": s.ranking_top_n,
            "ranking_one_event_per_city": s.ranking_one_event_per_city,
            "ranking_min_score": s.ranking_min_score,
            "allow_provisional_in_rankings": s.allow_provisional_in_rankings,
            "pipeline_source_lag_hours": s.pipeline_source_lag_hours,
            "pipeline_finalize_lag_days": s.pipeline_finalize_lag_days,
            "pipeline_min_city_completeness": s.pipeline_min_city_completeness,
            "weather_provider": s.weather_provider,
            "llm_provider": s.llm_provider if s.llm_enabled else "none",
            "agent_investigate_top_n": s.agent_investigate_top_n,
        }

    def _fetch(
        self,
        cities: list[City],
        analysis_date: date,
        tier: DataTier,
        report: RunReport,
        *,
        now_utc: datetime,
    ) -> dict[str, CityOutcome]:
        """Fetch and store the day for every city, tolerating per-city failures.

        One city's provider error must not lose the other forty-nine. Failures are
        counted, logged, and reflected in the completeness figure that the run
        threshold checks — which is where a bad day gets rejected, rather than
        here.
        """
        outcomes: dict[str, CityOutcome] = {}
        report.cities_total = len(cities)

        for city in cities:
            outcome = CityOutcome(city_id=city.id, local_date=analysis_date)
            outcomes[city.id] = outcome

            tz = ZoneInfo(city.timezone)
            if not is_local_date_complete(
                analysis_date, tz, now_utc, self.settings.pipeline_source_lag_hours
            ):
                outcome.error = "local day not complete"
                logger.warning(
                    "skipping %s: %s is not a completed local day", city.id, analysis_date
                )
                continue

            try:
                records = self.provider.fetch_daily(
                    city_id=city.id,
                    latitude=city.latitude,
                    longitude=city.longitude,
                    timezone=city.timezone,
                    start_date=analysis_date,
                    end_date=analysis_date,
                    tier=tier,
                )
                report.provider_requests += 1
            except ProviderError as exc:
                report.provider_errors += 1
                outcome.error = f"{type(exc).__name__}: {exc}"
                logger.error("fetch failed for %s: %s", city.id, exc)
                continue

            if not records:
                outcome.error = "provider returned no records"
                continue

            upsert_observations(self.session, records)
            record = records[0]
            outcome.fetched = True
            outcome.quality = record.classify_quality().value
            outcome.missing_fields = list(record.missing_fields)
            if record.has_any_value():
                report.cities_with_data += 1

        report.completeness = (
            report.cities_with_data / report.cities_total if report.cities_total else 0.0
        )
        self.session.commit()
        return outcomes

    def _require_completeness(self, report: RunReport, city_count: int) -> None:
        """Refuse to publish a board built from a fraction of the registry.

        A day where two thirds of the fetches failed would still produce a
        plausible-looking top 10. Publishing it would quietly change the question
        the board answers, which is worse than publishing nothing and leaving
        yesterday up.
        """
        floor = self.settings.pipeline_min_city_completeness
        if report.completeness < floor:
            raise PipelineError(
                f"data completeness {report.completeness:.1%} is below the required "
                f"{floor:.1%} ({report.cities_with_data}/{city_count} cities). "
                f"Refusing to publish; the previous ranking is unchanged."
            )

    def _compute(
        self,
        cities: list[City],
        analysis_date: date,
        outcomes: dict[str, CityOutcome],
        report: RunReport,
    ) -> list[AnomalyCandidate]:
        """Score every city-metric pair against its cached seasonal baseline."""
        observations = observations_for_date(self.session, analysis_date)
        baselines = load_baselines_for_date(
            self.session, analysis_date, [c.id for c in cities], self.settings
        )

        candidates: list[AnomalyCandidate] = []
        for city in cities:
            observation = observations.get(city.id)
            if observation is None:
                continue

            outcome = outcomes.setdefault(city.id, CityOutcome(city_id=city.id))
            for metric in ALL_METRICS:
                baseline = baselines.get((city.id, metric))
                if baseline is None:
                    continue
                outcome.baselines_found += 1

                candidate = compute_anomaly(
                    city_id=city.id,
                    local_date=analysis_date.isoformat(),
                    metric=metric,
                    observed_value=_observed_value(observation, metric),
                    baseline=baseline,
                    min_wet_days=self.settings.baseline_min_wet_days,
                    min_score=self.settings.ranking_min_score,
                )
                if candidate is None:
                    continue
                candidates.append(candidate)
                outcome.candidates += 1

        report.city_outcomes = list(outcomes.values())
        report.events_total = len(candidates)
        report.events_excluded = sum(1 for c in candidates if not c.eligible)

        if not candidates:
            raise PipelineError(
                f"no anomaly candidates for {analysis_date}. Baselines are probably "
                f"missing — run 'build-baselines' first."
            )
        return candidates

    def _rank(self, candidates: list[AnomalyCandidate], report: RunReport):
        eligible = [c for c in candidates if c.eligible]
        result = rank_candidates(
            eligible,
            top_n=self.settings.ranking_top_n,
            one_event_per_city=self.settings.ranking_one_event_per_city,
        )
        report.events_published = len(result.ranked)
        logger.info(
            "ranked %d of %d eligible candidates (%s)",
            len(result.ranked),
            len(eligible),
            json.dumps(result.diagnostics, default=str),
        )
        if not result.ranked:
            raise PipelineError(
                "ranking produced no events; every candidate scored below "
                f"RANKING_MIN_SCORE={self.settings.ranking_min_score}"
            )
        return result

    def _persist_events(
        self,
        run: PipelineRun,
        candidates: list[AnomalyCandidate],
        ranked,
        analysis_date: date,
        report: RunReport,
    ) -> None:
        """Upsert every candidate, including the ones that missed the top 10.

        The board shows ten; the table keeps all of them. That is what makes a
        city page able to say "this was the 34th most unusual day here" and what
        lets the ranking be re-derived later without refetching anything.
        """
        observations = observations_for_date(self.session, analysis_date)
        existing = {
            row.id: row
            for row in self.session.execute(
                select(AnomalyEvent).where(AnomalyEvent.local_date == analysis_date)
            )
            .scalars()
            .all()
        }

        for candidate in candidates:
            event_id = build_event_id(
                analysis_date.isoformat(), candidate.city_id, candidate.metric
            )
            observation = observations.get(candidate.city_id)
            values = _event_values(candidate, observation)
            row = existing.get(event_id)
            if row is None:
                row = AnomalyEvent(id=event_id, **values)
                self.session.add(row)
                existing[event_id] = row
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            row.run_id = run.id

        self.session.flush()

        # Replace this date's board rather than appending to it. Without this a
        # rerun would leave two rank-1 rows for the same day.
        self.session.execute(
            delete(DailyRanking).where(DailyRanking.analysis_date == analysis_date)
        )
        for entry in ranked.ranked:
            self.session.add(
                DailyRanking(
                    run_id=run.id,
                    analysis_date=analysis_date,
                    rank=entry.rank,
                    event_id=build_event_id(
                        analysis_date.isoformat(),
                        entry.candidate.city_id,
                        entry.candidate.metric,
                    ),
                    score=entry.candidate.anomaly_score,
                    methodology_version=METHODOLOGY_VERSION,
                )
            )
        self.session.commit()
        logger.info(
            "persisted %d events and a %d-event board for %s",
            len(candidates),
            len(ranked.ranked),
            analysis_date,
        )

    def _explain(self, run: PipelineRun, ranked, report: RunReport) -> None:
        """Generate one explanation per ranked event, then store them.

        This is the only step that can cost money, and it is the only step whose
        failure is tolerated: if every investigation fell back to a template the
        board is still complete and still correct, so there is nothing here worth
        failing a run over.
        """
        cities = {
            c.id: c
            for c in self.session.execute(select(City)).scalars().all()
        }
        targets = [
            (
                entry.rank,
                self.session.get(
                    AnomalyEvent,
                    build_event_id(
                        entry.candidate.local_date,
                        entry.candidate.city_id,
                        entry.candidate.metric,
                    ),
                ),
                cities[entry.candidate.city_id],
            )
            for entry in ranked.ranked
        ]
        targets = [(rank, event, city) for rank, event, city in targets if event]

        batch = investigate_events(self.session, targets, self.settings)

        self.session.execute(
            delete(AgentExplanation).where(
                AgentExplanation.event_id.in_([e.id for _, e, _ in targets])
            )
        )
        for outcome in batch.outcomes:
            self.session.add(_explanation_row(run.id, outcome))
        self.session.commit()

        report.llm_calls = batch.llm_calls
        report.llm_generated = batch.llm_generated
        report.template_generated = batch.template_generated
        report.llm_estimated_usd = batch.estimated_usd
        report.llm_budget_exhausted = batch.budget_exhausted

        run.llm_calls = batch.llm_calls
        run.llm_prompt_tokens = batch.prompt_tokens
        run.llm_completion_tokens = batch.completion_tokens
        run.llm_estimated_usd = batch.estimated_usd
        run.llm_budget_exhausted = batch.budget_exhausted
        self.session.commit()

        logger.info(
            "explanations: %d model-written, %d deterministic, %d llm calls, ~$%.4f",
            batch.llm_generated,
            batch.template_generated,
            batch.llm_calls,
            batch.estimated_usd,
        )

    def _publish(
        self,
        run: PipelineRun,
        analysis_date: date,
        ranked,
        report: RunReport,
        started: float,
    ) -> None:
        """The atomic step. Before this commit the run is invisible; after it, live.

        Unpublishing the previous run for the same date happens in the same
        transaction, so there is no instant at which a reader sees two boards for
        one day or none.
        """
        registry_version = self.session.execute(
            select(City.registry_version).limit(1)
        ).scalar_one_or_none()

        for superseded in (
            self.session.execute(
                select(PipelineRun).where(
                    PipelineRun.analysis_date == analysis_date,
                    PipelineRun.id != run.id,
                    PipelineRun.published.is_(True),
                )
            )
            .scalars()
            .all()
        ):
            superseded.published = False

        now = datetime.now(UTC)
        report.duration_ms = int((time.perf_counter() - started) * 1000)
        report.status = RunStatus.SUCCEEDED.value
        report.published = True

        run.status = RunStatus.SUCCEEDED.value
        run.finished_at = now
        run.duration_ms = report.duration_ms
        run.cities_total = report.cities_total
        run.cities_with_data = report.cities_with_data
        run.cities_missing = report.cities_total - report.cities_with_data
        run.completeness = report.completeness
        run.events_total = report.events_total
        run.events_published = report.events_published
        run.events_excluded = report.events_excluded
        run.provider_requests = report.provider_requests
        run.provider_errors = report.provider_errors
        run.registry_version = registry_version
        run.published = True
        run.published_at = now
        self.session.commit()

        logger.info(
            "published run %s for %s with %d events",
            run.id,
            analysis_date,
            len(ranked.ranked),
        )

    def _close_run(
        self,
        run_id: str,
        report: RunReport,
        *,
        status: RunStatus,
        published: bool,
    ) -> None:
        run = self.session.get(PipelineRun, run_id)
        if run is None:  # pragma: no cover - we created it
            return
        run.status = status.value
        run.finished_at = datetime.now(UTC)
        run.duration_ms = report.duration_ms
        run.cities_total = report.cities_total
        run.cities_with_data = report.cities_with_data
        run.provider_requests = report.provider_requests
        run.provider_errors = report.provider_errors
        run.published = published
        self.session.commit()

    def _fail_run(self, run_id: str, report: RunReport, exc: BaseException) -> None:
        """Record the failure on the run row, in its own transaction.

        Done with a fresh read of the row because the rollback that preceded this
        may have expired the instance. The one invariant that matters: this never
        touches ``published`` on any other run.
        """
        try:
            run = self.session.get(PipelineRun, run_id)
            if run is None:  # pragma: no cover
                return
            run.status = RunStatus.FAILED.value
            run.finished_at = datetime.now(UTC)
            run.duration_ms = report.duration_ms
            run.cities_total = report.cities_total
            run.cities_with_data = report.cities_with_data
            run.completeness = report.completeness
            run.events_total = report.events_total
            run.provider_requests = report.provider_requests
            run.provider_errors = report.provider_errors
            run.published = False
            run.error = str(exc)[:4000]
            run.error_type = type(exc).__name__
            self.session.commit()
        except Exception:  # pragma: no cover - never mask the original failure
            logger.exception("could not record failure for run %s", run_id)
            self.session.rollback()


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def _observed_value(observation: WeatherObservation, metric: Metric) -> float | None:
    return {
        Metric.TEMP_MAX: observation.temp_max_c,
        Metric.TEMP_MIN: observation.temp_min_c,
        Metric.TEMP_MEAN: observation.temp_mean_c,
        Metric.PRECIPITATION: observation.precipitation_mm,
        Metric.WIND_GUST: observation.wind_gust_max_kmh,
    }[metric]


def _event_values(
    candidate: AnomalyCandidate, observation: WeatherObservation | None
) -> dict:
    """Map a computed candidate onto event columns, carrying source provenance."""
    return {
        "city_id": candidate.city_id,
        "local_date": date.fromisoformat(candidate.local_date),
        "metric": Metric(candidate.metric).value,
        "direction": candidate.direction.value,
        "observed_value": candidate.observed_value,
        "unit": candidate.unit,
        "baseline_mean": candidate.baseline_mean,
        "baseline_median": candidate.baseline_median,
        "baseline_std": candidate.baseline_std,
        "baseline_p25": candidate.baseline_p25,
        "baseline_p75": candidate.baseline_p75,
        "baseline_min": candidate.baseline_min,
        "baseline_max": candidate.baseline_max,
        "baseline_n": candidate.baseline_n,
        "baseline_sufficient": candidate.baseline_sufficient,
        "deviation": candidate.deviation,
        "robust_deviation": candidate.robust_deviation,
        "z_score": candidate.z_score,
        "z_valid": candidate.z_valid,
        "percentile": candidate.percentile,
        "tail_probability": candidate.tail_probability,
        "return_period_years": candidate.return_period_years,
        "tail_probability_is_bounded": candidate.tail_probability_is_bounded,
        "beyond_baseline_sample": candidate.beyond_baseline_sample,
        "surprisal": candidate.surprisal,
        "margin_bonus": candidate.margin_bonus,
        "anomaly_score": candidate.anomaly_score,
        "data_tier": observation.data_tier if observation else DataTier.FINAL.value,
        "data_quality": observation.data_quality if observation else DataQuality.OK.value,
        "source_dataset": observation.source_dataset if observation else "unknown",
        "observation_type": observation.observation_type if observation else "reanalysis",
        "eligible": candidate.eligible,
        "excluded_reason": candidate.excluded_reason,
        "evidence": candidate.evidence,
        "methodology_version": candidate.methodology_version,
    }


def _explanation_row(run_id: str, outcome: InvestigationOutcome) -> AgentExplanation:
    e = outcome.explanation
    return AgentExplanation(
        event_id=outcome.event_id,
        run_id=run_id,
        headline=e.headline,
        statistical_explanation=e.statistical_explanation,
        historical_context=e.historical_context,
        caveats=e.caveats,
        evidence=[item.model_dump() for item in e.evidence],
        confidence=e.confidence,
        generator=outcome.generator,
        llm_provider=outcome.llm_provider,
        model=outcome.model,
        tool_calls=outcome.tool_calls,
        tool_call_count=outcome.tool_call_count,
        attempts=outcome.attempts,
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
        estimated_usd=outcome.estimated_usd,
        latency_ms=outcome.latency_ms,
        validation=outcome.validation,
        fallback_reason=outcome.fallback_reason,
    )


# ---------------------------------------------------------------------------
# Convenience wrappers used by the CLI
# ---------------------------------------------------------------------------


def seed_cities(session: Session) -> dict:
    """Load the version-controlled registry into the database."""
    return sync_registry(session)


def coverage(session: Session, settings: Settings | None = None) -> dict:
    return baseline_coverage(session, settings)


def backfill(
    session: Session,
    start: date,
    end: date,
    settings: Settings | None = None,
    provider: WeatherProvider | None = None,
    *,
    skip_explanations: bool = False,
) -> list[RunReport]:
    """Re-run a closed date range, oldest first.

    Each date is an independent run: one bad day does not abort the rest, and the
    reports come back in order so a failure is obvious in the output.
    """
    if end < start:
        raise PipelineError("end date must not be before start date")

    pipeline = Pipeline(session, settings, provider)
    reports: list[RunReport] = []
    try:
        day = start
        while day <= end:
            reports.append(
                pipeline.run_daily(
                    day,
                    kind=RunKind.BACKFILL,
                    skip_explanations=skip_explanations,
                )
            )
            day += timedelta(days=1)
    finally:
        pipeline.close()
    return reports


def finalize(
    session: Session,
    settings: Settings | None = None,
    provider: WeatherProvider | None = None,
    *,
    lookback_days: int = 14,
    now_utc: datetime | None = None,
) -> list[RunReport]:
    """Recompute published dates that were analysed from provisional data.

    The near-real-time analysis a daily run uses can be revised when the
    reanalysis lands. This walks recent published runs, finds the ones still
    marked provisional whose settled archive should now exist, and re-runs them at
    the final tier. The archive therefore converges on the better numbers instead
    of preserving a first guess forever — and because the methodology version is
    unchanged, the same event rows are updated rather than duplicated.
    """
    settings = settings or get_settings()
    now_utc = now_utc or datetime.now(UTC)
    cutoff = now_utc.date() - timedelta(days=lookback_days)
    ready_before = now_utc.date() - timedelta(days=settings.pipeline_finalize_lag_days)

    pending = (
        session.execute(
            select(PipelineRun.analysis_date)
            .where(
                PipelineRun.published.is_(True),
                PipelineRun.status == RunStatus.SUCCEEDED.value,
                PipelineRun.data_tier == DataTier.PROVISIONAL.value,
                PipelineRun.analysis_date >= cutoff,
                PipelineRun.analysis_date <= ready_before,
            )
            .order_by(PipelineRun.analysis_date)
        )
        .scalars()
        .all()
    )

    pipeline = Pipeline(session, settings, provider)
    reports: list[RunReport] = []
    try:
        for day in pending:
            logger.info("finalising %s against the settled reanalysis", day)
            reports.append(
                pipeline.run_daily(
                    day, kind=RunKind.FINALIZE, force_tier=DataTier.FINAL, now_utc=now_utc
                )
            )
    finally:
        pipeline.close()
    return reports
