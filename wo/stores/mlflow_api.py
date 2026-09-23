"""Read-only access to MLflow traces through the client API.

This store deliberately uses only the public, supported client surface —
``search_traces``, ``get_trace``, ``get_experiment_by_name`` — and never touches
MLflow's PostgreSQL schema. With ``mlflow-skinny`` (which ships no pandas),
``search_traces`` is called with ``return_type="list"`` so it returns entity
objects rather than a DataFrame.

The store is strictly read-only: it never calls ``set_experiment`` (which
auto-creates an experiment) and never starts a span. Every call that would reach
a store is wrapped so a dead server, a missing URI, or an uninstalled client
degrades to a :class:`StoreUnavailable` with a fix-up message.
"""

from __future__ import annotations

import os
from typing import Any

from wo.models import TraceRecord
from wo.normalize import normalize_trace
from wo.stores.base import StoreUnavailable


class MlflowApiStore:
    def __init__(
        self,
        tracking_uri: str | None = None,
        *,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._tracking_uri = tracking_uri
        self._username = username
        self._password = password

    def _mlflow(self) -> Any:
        try:
            import mlflow
        except ImportError as exc:
            raise StoreUnavailable(
                "mlflow is not installed; install the extra with "
                '`uv pip install "./backend[tracing]"`'
            ) from exc
        return mlflow

    def _configure(self, mlflow: Any) -> None:
        if not self._tracking_uri:
            raise StoreUnavailable(
                "MLFLOW_TRACKING_URI is not set; no tracking store is configured"
            )
        if self._username is not None:
            os.environ["MLFLOW_TRACKING_USERNAME"] = self._username
        if self._password is not None:
            os.environ["MLFLOW_TRACKING_PASSWORD"] = self._password
        mlflow.set_tracking_uri(self._tracking_uri)

    def _experiment_id(self, mlflow: Any, experiment: str) -> str | None:
        self._configure(mlflow)
        try:
            result = mlflow.get_experiment_by_name(experiment)
        except Exception as exc:
            raise StoreUnavailable(f"cannot reach the MLflow tracking store: {exc}") from exc
        return result.experiment_id if result is not None else None

    def experiment_exists(self, experiment: str) -> bool:
        mlflow = self._mlflow()
        return self._experiment_id(mlflow, experiment) is not None

    def count_traces(self, experiment: str) -> int:
        mlflow = self._mlflow()
        experiment_id = self._experiment_id(mlflow, experiment)
        if experiment_id is None:
            return 0
        try:
            traces = mlflow.search_traces(
                locations=[experiment_id],
                include_spans=False,
                return_type="list",
            )
        except Exception as exc:
            raise StoreUnavailable(f"cannot list traces: {exc}") from exc
        return len(traces)

    def list_traces(self, experiment: str, limit: int | None = None) -> list[TraceRecord]:
        mlflow = self._mlflow()
        experiment_id = self._experiment_id(mlflow, experiment)
        if experiment_id is None:
            return []
        try:
            traces = mlflow.search_traces(
                locations=[experiment_id],
                max_results=limit,
                include_spans=True,
                return_type="list",
            )
        except Exception as exc:
            raise StoreUnavailable(f"cannot list traces: {exc}") from exc
        return [normalize_trace(trace) for trace in traces]

    def get_trace(self, trace_id: str) -> TraceRecord | None:
        mlflow = self._mlflow()
        self._configure(mlflow)
        try:
            raw = mlflow.get_trace(trace_id, silent=True)
        except Exception as exc:
            raise StoreUnavailable(f"cannot fetch trace {trace_id!r}: {exc}") from exc
        return normalize_trace(raw) if raw is not None else None
