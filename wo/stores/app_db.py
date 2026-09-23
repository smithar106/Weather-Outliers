"""Read-only access to the application's own database.

The heavy lifting is delegated to the application's own code where it exists —
``app.ingest.baselines.baseline_coverage`` for coverage — so the CLI reports the
same numbers as the ``status`` command and the health endpoint, rather than a
near-miss reimplementation that could drift.

Everything that imports ``app`` is deferred into the methods, because importing
this module must not itself require the backend to be installed. ``backend`` is
placed on ``sys.path`` exactly the way ``evals/environment.py`` does it.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from wo.models import Coverage, RunRecord
from wo.stores.base import StoreUnavailable


def _ensure_backend_on_path() -> None:
    import sys

    backend = Path(__file__).resolve().parents[2] / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))


def _app_modules() -> tuple[Any, Any, Any]:
    """Return ``(models, get_settings, baseline_coverage)``, importing lazily."""
    _ensure_backend_on_path()
    from app import models
    from app.config import get_settings
    from app.ingest.baselines import baseline_coverage

    return models, get_settings, baseline_coverage


class AppDbStore:
    """Queries the application database through the app's own ORM models.

    ``database_url`` overrides the URL; when omitted the store reads
    ``DATABASE_URL`` through the application's settings, which also normalises the
    ``postgres://`` scheme Railway injects. ``engine`` is a test seam: the tests
    pass an in-memory SQLite engine with a pinned pool, exactly as the backend's
    own tests do.
    """

    def __init__(self, database_url: str | None = None, *, engine: Any = None) -> None:
        self._database_url = database_url
        self._engine = engine
        self._factory: Any = None

    def _session_factory(self) -> Any:
        if self._factory is None:
            _, get_settings, _ = _app_modules()
            url = self._database_url or get_settings().database_url
            engine = self._engine or create_engine(url, future=True, pool_pre_ping=True)
            self._factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        return self._factory

    def coverage(self) -> Coverage:
        _, _, baseline_coverage = _app_modules()
        try:
            with self._session_factory()() as session:
                data = baseline_coverage(session)
        except StoreUnavailable:
            raise
        except Exception as exc:  # pragma: no cover - surfaced as a message, not a crash
            raise StoreUnavailable(f"cannot reach the application database: {exc}") from exc
        return Coverage(
            cities_with_baselines=int(data.get("cities_with_baselines", 0)),
            rows=int(data.get("rows", 0)),
            sufficient_rows=int(data.get("sufficient_rows", 0)),
            reference_period=data.get("reference_period"),
        )

    def recent_runs(self, limit: int) -> list[RunRecord]:
        models, _, _ = _app_modules()
        try:
            with self._session_factory()() as session:
                rows = (
                    session.execute(
                        select(models.PipelineRun)
                        .order_by(models.PipelineRun.started_at.desc())
                        .limit(limit)
                    )
                    .scalars()
                    .all()
                )
        except Exception as exc:  # pragma: no cover
            raise StoreUnavailable(f"cannot reach the application database: {exc}") from exc
        return [self._to_run_record(run) for run in rows]

    def explanation_mix(self, run_ids: Sequence[str]) -> dict[str, int]:
        if not run_ids:
            return {}
        models, _, _ = _app_modules()
        try:
            with self._session_factory()() as session:
                rows = session.execute(
                    select(
                        models.AgentExplanation.generator,
                        func.count(models.AgentExplanation.id),
                    )
                    .where(models.AgentExplanation.run_id.in_(run_ids))
                    .group_by(models.AgentExplanation.generator)
                ).all()
        except Exception as exc:  # pragma: no cover
            raise StoreUnavailable(f"cannot reach the application database: {exc}") from exc
        return {str(generator): int(count) for generator, count in rows if generator is not None}

    def pricing_configured(self) -> bool:
        from app.agent.llm import pricing_configured

        _, get_settings, _ = _app_modules()
        return pricing_configured(get_settings())

    @staticmethod
    def _to_run_record(run: Any) -> RunRecord:
        return RunRecord(
            run_id=run.id,
            kind=run.kind,
            analysis_date=run.analysis_date.isoformat() if run.analysis_date else None,
            status=run.status,
            published=bool(run.published),
            started_at=run.started_at.isoformat(timespec="seconds") if run.started_at else None,
            duration_ms=run.duration_ms,
            cities_with_data=run.cities_with_data,
            cities_total=run.cities_total,
            completeness=run.completeness,
            events_published=run.events_published,
            events_total=run.events_total,
            llm_calls=run.llm_calls,
            llm_estimated_usd=run.llm_estimated_usd,
            error=run.error,
            error_type=run.error_type,
        )
