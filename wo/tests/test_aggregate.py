"""Aggregation: trace summarisation and span-tree assembly."""

from __future__ import annotations

from wo.aggregate import build_span_tree, generator_labels, summarize_trace
from wo.models import SpanNode, TraceRecord


def _span(span_id, parent_id, name, *, start_ms=None, attrs=None):
    return SpanNode(
        span_id=span_id,
        parent_span_id=parent_id,
        name=name,
        span_type="CHAIN",
        status="OK",
        start_ms=start_ms,
        end_ms=None,
        latency_ms=None,
        attributes=attrs or {},
    )


def _trace(spans, *, trace_id="tr-1", status="OK"):
    return TraceRecord(
        trace_id=trace_id,
        status=status,
        timestamp_ms=1_700_000_000_000,
        duration_ms=100,
        tags={},
        metadata={},
        spans=tuple(spans),
    )


def test_summarize_trace_extracts_root_attributes():
    record = _trace(
        [
            _span(
                "r",
                None,
                "pipeline.run_daily",
                attrs={
                    "model": "claude-sonnet-5",
                    "llm_provider": "anthropic",
                    "methodology_version": "1.2.0",
                    "prompt_version": "1.0.0",
                    "prompt_sha256": "abc123",
                    "run_id": "run-9",
                },
            )
        ]
    )
    summary = summarize_trace(record)
    assert summary.root_span == "pipeline.run_daily"
    assert summary.span_count == 1
    assert summary.model == "claude-sonnet-5"
    assert summary.llm_provider == "anthropic"
    assert summary.methodology_version == "1.2.0"
    assert summary.prompt_version == "1.0.0"
    assert summary.prompt_sha256 == "abc123"
    assert summary.run_id == "run-9"


def test_summarize_trace_missing_root_degrades_to_none():
    record = _trace([_span("child", "missing-parent", "orphan")])
    summary = summarize_trace(record)
    assert summary.root_span is None
    assert summary.model is None
    assert summary.prompt_version is None
    assert summary.run_id is None
    assert summary.span_count == 1


def test_summarize_trace_empty_spans():
    summary = summarize_trace(_trace([]))
    assert summary.root_span is None
    assert summary.span_count == 0


def test_summarize_trace_finds_model_on_child_span():
    record = _trace(
        [
            _span("r", None, "pipeline.run_daily", attrs={"llm_provider": "anthropic"}),
            _span("e", "r", "explain_event", attrs={"model": "claude-sonnet-5"}),
        ]
    )
    summary = summarize_trace(record)
    assert summary.model == "claude-sonnet-5"
    assert summary.llm_provider == "anthropic"


def test_build_span_tree_nests_and_orders():
    spans = [
        _span("c", "r", "child", start_ms=2),
        _span("r", None, "root", start_ms=1),
        _span("gc", "c", "grandchild", start_ms=3),
    ]
    ordered = build_span_tree(spans)
    assert [(s.span_id, depth) for s, depth in ordered] == [
        ("r", 0),
        ("c", 1),
        ("gc", 2),
    ]


def test_build_span_tree_orphans_are_not_dropped():
    spans = [_span("root", None, "root"), _span("orphan", "missing", "orphan")]
    ordered = build_span_tree(spans)
    assert len(ordered) == 2
    assert ordered[1][0].span_id == "orphan"
    assert ordered[1][1] == 0


def test_generator_labels_maps_and_ignores_unknown():
    assert generator_labels({"llm": 3, "template": 7}) == (3, 7)
    assert generator_labels({}) == (0, 0)
    assert generator_labels({"llm": 2, "weird": 9}) == (2, 0)
