"""Translate MLflow traces and spans into the plain :mod:`wo.models` types.

Everything here is duck-typed on purpose: the functions read the attributes the
MLflow entities expose rather than importing the entities, so the tests can pass
``types.SimpleNamespace`` fakes and the code stays honest about exactly what it
depends on. It is also deliberately defensive — a missing attribute, a
non-numeric latency, a ``None`` status, or a malformed inputs blob must degrade
to ``None`` or a placeholder rather than raise, because a trace produced by an
older client, a different span type, or a partially-flushed run is not an error
in the CLI that reads it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from wo.models import SpanNode, TraceRecord

#: MLflow reserves the ``mlflow.*`` attribute namespace for its own span plumbing
#: (span type, request id, log level, serialised inputs/outputs). The application's
#: attributes — ``run_id``, ``latency_ms``, ``model``, ``prompt_version`` … — are the
#: unprefixed ones.
_RESERVED_ATTR_PREFIX = "mlflow."


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_str_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and not value:
        return None
    return str(value)


def _as_int_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _status_str(value: Any) -> str:
    """Normalise MLflow's several status representations to a plain string.

    A span's ``status`` is a ``SpanStatus`` (which carries a ``.status_code``), a
    trace's ``state`` is a ``TraceState`` enum, and either may be absent or a bare
    string. Return ``"OK"`` / ``"ERROR"`` / ``"IN_PROGRESS"`` / ``"UNKNOWN"`` and
    never raise.
    """
    if value is None:
        return "UNKNOWN"
    raw = getattr(value, "value", None)
    if isinstance(raw, str):
        return raw
    code = getattr(value, "status_code", None)
    code_value = getattr(code, "value", None)
    if isinstance(code_value, str):
        return code_value
    text = str(value)
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text or "UNKNOWN"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    return {}


def _as_str_mapping(value: Any) -> Mapping[str, str]:
    out: dict[str, str] = {}
    for key, val in _as_mapping(value).items():
        out[_as_str(key)] = _as_str(val)
    return out


def _latency_ms(attributes: Mapping[str, Any], start_ns: Any, end_ns: Any) -> int | None:
    """Prefer the instrumented ``latency_ms`` attribute; fall back to span timing."""
    recorded = _as_int_none(attributes.get("latency_ms"))
    if recorded is not None:
        return recorded
    start = _as_int_none(start_ns)
    end = _as_int_none(end_ns)
    if start is not None and end is not None and end >= start:
        return (end - start) // 1_000_000
    return None


def public_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """The application's own span attributes, with MLflow's plumbing stripped out."""
    return {
        key: value
        for key, value in attributes.items()
        if not _as_str(key).startswith(_RESERVED_ATTR_PREFIX)
    }


def normalize_span(raw: Any) -> SpanNode:
    attributes = _as_mapping(getattr(raw, "attributes", None))
    start_ns = _as_int_none(getattr(raw, "start_time_ns", None))
    end_ns = _as_int_none(getattr(raw, "end_time_ns", None))
    return SpanNode(
        span_id=_as_str(getattr(raw, "span_id", None), "?"),
        parent_span_id=_as_str_none(getattr(raw, "parent_id", None)),
        name=_as_str(getattr(raw, "name", None), "unnamed"),
        span_type=_as_str_none(getattr(raw, "span_type", None)),
        status=_status_str(getattr(raw, "status", None)),
        start_ms=start_ns // 1_000_000 if start_ns is not None else None,
        end_ms=end_ns // 1_000_000 if end_ns is not None else None,
        latency_ms=_latency_ms(attributes, start_ns, end_ns),
        attributes=attributes,
        inputs=getattr(raw, "inputs", None),
        outputs=getattr(raw, "outputs", None),
    )


def normalize_trace(raw: Any) -> TraceRecord:
    info = getattr(raw, "info", raw)
    data = getattr(raw, "data", None)
    spans_raw = getattr(data, "spans", None) if data is not None else None

    state = getattr(info, "state", None)
    if state is None:
        state = getattr(info, "status", None)

    trace_id = getattr(info, "trace_id", None)
    if not trace_id:
        trace_id = getattr(info, "request_id", None)

    request_time = getattr(info, "request_time", None)
    if request_time is None:
        request_time = getattr(info, "timestamp_ms", None)

    execution_duration = getattr(info, "execution_duration", None)
    if execution_duration is None:
        execution_duration = getattr(info, "execution_time_ms", None)

    metadata = getattr(info, "trace_metadata", None)
    if metadata is None:
        metadata = getattr(info, "request_metadata", None)

    return TraceRecord(
        trace_id=_as_str(trace_id, "?"),
        status=_status_str(state),
        timestamp_ms=_as_int_none(request_time),
        duration_ms=_as_int_none(execution_duration),
        tags=_as_str_mapping(getattr(info, "tags", None)),
        metadata=_as_str_mapping(metadata),
        spans=tuple(normalize_span(span) for span in spans_raw) if spans_raw else (),
    )
