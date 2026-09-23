"""Pure aggregation and shaping over :mod:`wo.models`.

The functions here are deliberately free of I/O: they take plain model objects
and return plain values, which is what makes them unit-testable without MLflow
or a database. They answer the two questions that recur across commands — "what
does one trace look like in a table row" and "how do spans nest into a tree" —
plus the composition of the ``summary`` view.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from wo.models import SpanNode, TraceRecord, TraceSummary
from wo.normalize import _as_str_none


def summarize_trace(record: TraceRecord) -> TraceSummary:
    """Distil one trace into the columns the ``traces`` listing prints.

    The interesting fields (model, prompt version, methodology version, run id)
    live on the root span's attributes, which may be absent for a trace recorded
    before those attributes existed or by a span type that never sets them. Each
    missing field becomes ``None`` rather than a guess.
    """
    root = record.root
    root_attributes = root.attributes if root is not None else {}

    def pick(attributes: Mapping, *keys: str):
        for key in keys:
            value = attributes.get(key)
            if value is not None and value != "":
                return value
        return None

    # ``model`` and ``llm_provider`` are set on the agent/LLM spans, not the root,
    # so they are searched across the whole trace rather than read from the root.
    model = pick(root_attributes, "model")
    llm_provider = pick(root_attributes, "llm_provider")
    for span in record.spans:
        if model is None:
            model = pick(span.attributes, "model")
        if llm_provider is None:
            llm_provider = pick(span.attributes, "llm_provider")
        if model is not None and llm_provider is not None:
            break

    return TraceSummary(
        trace_id=record.trace_id,
        timestamp_ms=record.timestamp_ms,
        status=record.status,
        duration_ms=record.duration_ms,
        root_span=root.name if root is not None else None,
        span_count=len(record.spans),
        model=_as_str_none(model),
        llm_provider=_as_str_none(llm_provider),
        methodology_version=_as_str_none(pick(root_attributes, "methodology_version")),
        prompt_version=_as_str_none(pick(root_attributes, "prompt_version")),
        prompt_sha256=_as_str_none(pick(root_attributes, "prompt_sha256")),
        run_id=_as_str_none(pick(root_attributes, "run_id")),
    )


def build_span_tree(spans: Sequence[SpanNode]) -> list[tuple[SpanNode, int]]:
    """Return spans in depth-first order paired with their depth.

    Root spans (``parent_span_id is None``) come first; children are ordered by
    start time so a concurrent run still reads top-to-bottom. A span whose parent
    is missing — which can happen when only part of a trace was persisted — is
    emitted as a top-level entry rather than dropped.
    """
    children: dict[str | None, list[SpanNode]] = {}
    for span in spans:
        children.setdefault(span.parent_span_id, []).append(span)

    def sort_key(span: SpanNode) -> tuple[int, str]:
        return (span.start_ms if span.start_ms is not None else 0, span.name)

    for group in children.values():
        group.sort(key=sort_key)

    ordered: list[tuple[SpanNode, int]] = []
    seen: set[str] = set()

    def walk(parent_id: str | None, depth: int) -> None:
        for span in children.get(parent_id, []):
            if span.span_id in seen:
                continue
            seen.add(span.span_id)
            ordered.append((span, depth))
            walk(span.span_id, depth + 1)

    walk(None, 0)
    for span in spans:
        if span.span_id not in seen:
            ordered.append((span, 0))
    return ordered


def generator_labels(mix: Mapping[str, int]) -> tuple[int, int]:
    """``(model_written, deterministic)`` from the stored ``generator`` counts.

    The application stores ``generator`` as ``"llm"`` or ``"template"``. Unknown
    values — which should not occur but must not crash a summary — are ignored.
    """
    return int(mix.get("llm", 0)), int(mix.get("template", 0))
