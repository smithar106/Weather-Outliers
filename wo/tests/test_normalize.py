"""Normalisation is the part that must never raise on unfamiliar input.

The CLI reads traces that may have been produced by an older client, a different
span type, or a partially-flushed run, so these tests deliberately feed it
malformed, missing and mis-typed values and assert it degrades instead of
crashing.
"""

from __future__ import annotations

from types import SimpleNamespace

from wo.normalize import (
    _status_str,
    normalize_span,
    normalize_trace,
    public_attributes,
)


def _span(**overrides):
    base = {
        "span_id": "s1",
        "parent_id": None,
        "name": "root",
        "span_type": "CHAIN",
        "status": SimpleNamespace(status_code=SimpleNamespace(value="OK")),
        "start_time_ns": 1_000_000_000,
        "end_time_ns": 3_000_000_000,
        "attributes": {"run_id": "run-1", "latency_ms": 42, "mlflow.spanType": "CHAIN"},
        "inputs": {"a": 1},
        "outputs": {"b": 2},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_status_string_variants():
    assert _status_str(None) == "UNKNOWN"
    assert _status_str("ERROR") == "ERROR"
    assert _status_str(SimpleNamespace(value="OK")) == "OK"
    assert _status_str(SimpleNamespace(status_code=SimpleNamespace(value="ERROR"))) == "ERROR"
    assert _status_str(SimpleNamespace(value="IN_PROGRESS")) == "IN_PROGRESS"


def test_span_latency_prefers_recorded_attribute():
    span = normalize_span(_span())
    assert span.latency_ms == 42


def test_span_latency_falls_back_to_timing():
    span = normalize_span(_span(attributes={"mlflow.spanType": "LLM"}))
    assert span.latency_ms == 2000  # 3s - 1s


def test_span_latency_none_when_absent_and_untimed():
    span = normalize_span(_span(attributes={}, start_time_ns=None, end_time_ns=None))
    assert span.latency_ms is None


def test_span_latency_ignores_malformed_value():
    span = normalize_span(_span(attributes={"latency_ms": "not-a-number"}, end_time_ns=None))
    assert span.latency_ms is None


def test_span_missing_attributes_becomes_empty_mapping():
    span = normalize_span(_span(attributes=None))
    assert dict(span.attributes) == {}


def test_span_attributes_non_mapping_becomes_empty():
    span = normalize_span(_span(attributes=42))
    assert dict(span.attributes) == {}


def test_span_keeps_inputs_and_outputs_verbatim():
    span = normalize_span(_span())
    assert span.inputs == {"a": 1}
    assert span.outputs == {"b": 2}


def test_trace_normalisation_reads_info_and_spans():
    raw = SimpleNamespace(
        info=SimpleNamespace(
            trace_id="tr-1",
            state=SimpleNamespace(value="OK"),
            request_time=1_700_000_000_000,
            execution_duration=123,
            tags={"mlflow.traceName": "root"},
            trace_metadata={"mlflow.source.git.commit": "abc123"},
        ),
        data=SimpleNamespace(spans=[_span()]),
    )
    trace = normalize_trace(raw)
    assert trace.trace_id == "tr-1"
    assert trace.status == "OK"
    assert trace.timestamp_ms == 1_700_000_000_000
    assert trace.duration_ms == 123
    assert trace.tags["mlflow.traceName"] == "root"
    assert trace.metadata["mlflow.source.git.commit"] == "abc123"
    assert len(trace.spans) == 1
    assert trace.root is not None and trace.root.name == "root"


def test_trace_normalisation_handles_legacy_field_names():
    raw = SimpleNamespace(
        info=SimpleNamespace(
            request_id="tr-legacy",
            status="ERROR",
            timestamp_ms=99,
            execution_time_ms=7,
            tags=None,
            request_metadata=None,
        ),
        data=None,
    )
    trace = normalize_trace(raw)
    assert trace.trace_id == "tr-legacy"
    assert trace.status == "ERROR"
    assert trace.timestamp_ms == 99
    assert trace.duration_ms == 7
    assert trace.spans == ()
    assert trace.root is None


def test_trace_normalisation_without_data_field():
    raw = SimpleNamespace(trace_id="tr-2", state=SimpleNamespace(value="OK"), request_time=None)
    trace = normalize_trace(raw)
    assert trace.trace_id == "tr-2"
    assert trace.timestamp_ms is None
    assert trace.spans == ()


def test_public_attributes_strips_mlflow_namespace():
    attrs = {"run_id": "r", "mlflow.spanType": "LLM", "mlflow.traceRequestId": "tr", "model": "m"}
    assert public_attributes(attrs) == {"run_id": "r", "model": "m"}
