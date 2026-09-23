"""Plain data types shared by the stores, the commands, and the renderer.

These are deliberately framework-free. The stores translate whatever MLflow or
the application database hands them into these types, so command and renderer
code never sees a SQLAlchemy row or an MLflow entity. If MLflow changes its
internal span representation, or the application schema moves a column, only the
corresponding store adapter changes — never a command.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SpanNode:
    """One span, flattened from MLflow's representation.

    ``attributes`` is the raw span attribute map, including MLflow's own
    ``mlflow.*`` reserved keys; :func:`wo.normalize.public_attributes` strips
    those for display. ``latency_ms`` prefers the instrumented ``latency_ms``
    attribute and falls back to start/end timing.
    """

    span_id: str
    parent_span_id: str | None
    name: str
    span_type: str | None
    status: str
    start_ms: int | None
    end_ms: int | None
    latency_ms: int | None
    attributes: Mapping[str, Any]
    inputs: Any = None
    outputs: Any = None


@dataclass(frozen=True)
class TraceRecord:
    """One trace: its metadata plus its spans, flattened."""

    trace_id: str
    status: str
    timestamp_ms: int | None
    duration_ms: int | None
    tags: Mapping[str, str]
    metadata: Mapping[str, str]
    spans: tuple[SpanNode, ...]

    @property
    def root(self) -> SpanNode | None:
        for span in self.spans:
            if span.parent_span_id is None:
                return span
        return None


@dataclass(frozen=True)
class TraceSummary:
    """The columns the ``traces`` listing shows, one per trace.

    Every field but ``trace_id`` may be ``None``: a trace produced before some
    attribute was added, or by a span that did not record it, degrades to a
    missing column rather than a fabricated value.
    """

    trace_id: str
    timestamp_ms: int | None
    status: str
    duration_ms: int | None
    root_span: str | None
    span_count: int
    model: str | None
    llm_provider: str | None
    methodology_version: str | None
    prompt_version: str | None
    prompt_sha256: str | None
    run_id: str | None


@dataclass(frozen=True)
class Coverage:
    """Baseline climatology coverage, as reported by the application's own query."""

    cities_with_baselines: int
    rows: int
    sufficient_rows: int
    reference_period: str | None


@dataclass(frozen=True)
class RunRecord:
    """One pipeline run, from the application's ``pipeline_runs`` table."""

    run_id: str
    kind: str
    analysis_date: str | None
    status: str
    published: bool
    started_at: str | None
    duration_ms: int | None
    cities_with_data: int
    cities_total: int
    completeness: float | None
    events_published: int
    events_total: int
    llm_calls: int
    llm_estimated_usd: float
    error: str | None
    error_type: str | None
