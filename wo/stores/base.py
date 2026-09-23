"""Store interfaces and the one shared failure type.

Commands depend on these protocols — not on SQLAlchemy, MLflow, or any concrete
store — which is what lets MLflow's schema or the application's ORM change
without touching a command, and what lets the tests substitute in-memory fakes.

There are two stores in this phase, mirroring the architecture: an application
database store for structured aggregates, and an MLflow client store for trace
retrieval. A third, direct-to-MLflow-Postgres store is reserved for the span-level
aggregations the client API cannot provide; it is not needed by ``summary``,
``traces`` or ``trace`` and is therefore not implemented yet.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from wo.models import Coverage, RunRecord, TraceRecord


class StoreUnavailable(RuntimeError):
    """A store could not be reached. The message says why, and how to fix it."""


class AppDbStore(Protocol):
    """Read-only access to the application's own PostgreSQL database."""

    def coverage(self) -> Coverage:
        """Baseline climatology coverage."""

    def recent_runs(self, limit: int) -> list[RunRecord]:
        """The most recent pipeline runs, newest first."""

    def explanation_mix(self, run_ids: Sequence[str]) -> dict[str, int]:
        """Count of explanations by ``generator`` across the given runs."""

    def pricing_configured(self) -> bool:
        """Whether per-token prices are set, so ``$0.00`` is not shown as "free"."""


class TraceStore(Protocol):
    """Read-only access to MLflow traces via the client API."""

    def experiment_exists(self, experiment: str) -> bool:
        """Whether the named experiment exists in the tracking store."""

    def count_traces(self, experiment: str) -> int:
        """Number of traces in the experiment, without fetching span data."""

    def list_traces(self, experiment: str, limit: int | None = None) -> list[TraceRecord]:
        """Traces in the experiment, newest first, with their spans."""

    def get_trace(self, trace_id: str) -> TraceRecord | None:
        """One trace by id, or ``None`` if it does not exist."""
