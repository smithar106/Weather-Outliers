"""Read-only monitoring data: pipeline runs, evaluation reports, MLflow traces.

This backs the ``/monitor`` page. Everything here is a read. The run and
evaluation rows come from the application database; the traces come from the
MLflow client (``mlflow-skinny``, already in the image) pointed at the private
tracking server. Nothing writes, and when the tracking store is not configured
the trace endpoints report "unavailable" rather than failing the page.

The MLflow entity reading is deliberately duck-typed and defensive, mirroring
what the ``wo`` CLI does: a trace from an older client, or a span that did not
record a field, degrades to a ``None`` rather than raising.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import EvaluationReport, PipelineRun

#: MLflow reserves the ``mlflow.*`` attribute namespace for its own plumbing.
_RESERVED = "mlflow."

#: Error types that are expected, resumable stops rather than failures. The
#: baseline build stops on ProviderBudgetExhausted and resumes on the next cron
#: firing, so counting it as an error makes a healthy system look broken.
BENIGN_ERROR_TYPES = frozenset({"ProviderBudgetExhausted"})


def _is_benign_error(span: dict[str, Any]) -> bool:
    attributes = span.get("attributes", {})
    return attributes.get("error_type") in BENIGN_ERROR_TYPES


def _str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _status(value: Any) -> str:
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
    return text.rsplit(".", 1)[-1] if "." in text else (text or "UNKNOWN")


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    return {}


def _public(attributes: Mapping[str, Any]) -> dict[str, Any]:
    return {str(k): v for k, v in attributes.items() if not str(k).startswith(_RESERVED)}


def _latency_ms(attributes: Mapping[str, Any], start_ns: Any, end_ns: Any) -> int | None:
    recorded = _int(attributes.get("latency_ms"))
    if recorded is not None:
        return recorded
    start, end = _int(start_ns), _int(end_ns)
    if start is not None and end is not None and end >= start:
        return (end - start) // 1_000_000
    return None


def _normalize_span(raw: Any) -> dict[str, Any]:
    attributes = _mapping(getattr(raw, "attributes", None))
    return {
        "span_id": _str(getattr(raw, "span_id", None), "?"),
        "parent_span_id": _str(getattr(raw, "parent_id", None)) or None,
        "name": _str(getattr(raw, "name", None), "unnamed"),
        "span_type": _str(getattr(raw, "span_type", None)) or None,
        "status": _status(getattr(raw, "status", None)),
        "latency_ms": _latency_ms(
            attributes, getattr(raw, "start_time_ns", None), getattr(raw, "end_time_ns", None)
        ),
        "attributes": _public(attributes),
    }


def _pick(attributes: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = attributes.get(key)
        if value not in (None, ""):
            return value
    return None


def _normalize_trace(raw: Any) -> dict[str, Any]:
    info = getattr(raw, "info", raw)
    data = getattr(raw, "data", None)
    spans_raw = getattr(data, "spans", None) if data is not None else None
    spans = [_normalize_span(span) for span in spans_raw] if spans_raw else []

    state = getattr(info, "state", None)
    if state is None:
        state = getattr(info, "status", None)
    trace_id = getattr(info, "trace_id", None) or getattr(info, "request_id", None)

    root = next((span for span in spans if span["parent_span_id"] is None), None)
    root_attributes = root["attributes"] if root else {}

    # ``model`` is set on the agent/LLM spans, not the root, so it is searched
    # across the whole trace rather than read from the root alone.
    model = _pick(root_attributes, "model")
    for span in spans:
        if model is None:
            model = _pick(span["attributes"], "model")
        else:
            break

    request_time = getattr(info, "request_time", None)
    if request_time is None:
        request_time = getattr(info, "timestamp_ms", None)
    execution_duration = getattr(info, "execution_duration", None)
    if execution_duration is None:
        execution_duration = getattr(info, "execution_time_ms", None)

    return {
        "trace_id": _str(trace_id, "?"),
        "timestamp_ms": _int(request_time),
        "status": _status(state),
        "duration_ms": _int(execution_duration),
        "root_span": root["name"] if root else None,
        "span_count": len(spans),
        "model": _str(model) or None,
        "methodology_version": _str(_pick(root_attributes, "methodology_version")) or None,
        "prompt_version": _str(_pick(root_attributes, "prompt_version")) or None,
        "run_id": _str(_pick(root_attributes, "run_id")) or None,
        "spans": spans,
    }


def _mlflow(settings: Settings) -> tuple[Any | None, str | None]:
    if not settings.mlflow_tracking_uri:
        return None, "MLFLOW_TRACKING_URI is not set"
    try:
        import mlflow
    except ImportError:
        return None, "mlflow is not installed in this image"
    try:
        mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    except Exception as exc:  # pragma: no cover - surfaced as a note
        return None, f"cannot reach the MLflow tracking store: {exc}"
    return mlflow, None


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def recent_runs(session: Session, limit: int = 20) -> list[dict[str, Any]]:
    rows = (
        session.execute(select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(limit))
        .scalars()
        .all()
    )
    return [
        {
            "run_id": run.id,
            "kind": run.kind,
            "analysis_date": run.analysis_date.isoformat() if run.analysis_date else None,
            "status": run.status,
            "data_tier": run.data_tier,
            "published": bool(run.published),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "duration_ms": run.duration_ms,
            "cities_total": run.cities_total,
            "cities_with_data": run.cities_with_data,
            "completeness": run.completeness,
            "events_total": run.events_total,
            "events_published": run.events_published,
            "llm_calls": run.llm_calls,
            "llm_estimated_usd": run.llm_estimated_usd,
            "error": run.error,
            "error_type": run.error_type,
        }
        for run in rows
    ]


def recent_evals(session: Session, limit: int = 20) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            select(EvaluationReport).order_by(EvaluationReport.generated_at.desc()).limit(limit)
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": row.id,
            "generated_at": row.generated_at.isoformat() if row.generated_at else None,
            "status": row.status,
            "git_commit": row.git_commit,
            "git_dirty": bool(row.git_dirty),
            "suites_total": row.suites_total,
            "suites_passed": row.suites_passed,
            "cases_total": row.cases_total,
            "cases_passed": row.cases_passed,
            "duration_ms": row.duration_ms,
        }
        for row in rows
    ]


def list_traces(settings: Settings, limit: int = 50) -> dict[str, Any]:
    mlflow, note = _mlflow(settings)
    if mlflow is None:
        return {"available": False, "note": note, "count": 0, "traces": []}
    try:
        experiment = mlflow.get_experiment_by_name(settings.mlflow_experiment)
    except Exception as exc:  # pragma: no cover
        return {
            "available": False,
            "note": f"cannot reach the tracking store: {exc}",
            "count": 0,
            "traces": [],
        }
    if experiment is None:
        note = f"experiment {settings.mlflow_experiment!r} has no traces yet"
        return {"available": True, "note": note, "count": 0, "traces": []}
    try:
        traces = mlflow.search_traces(
            locations=[experiment.experiment_id],
            max_results=limit,
            include_spans=True,
            return_type="list",
        )
    except Exception as exc:  # pragma: no cover
        return {"available": False, "note": f"cannot list traces: {exc}", "count": 0, "traces": []}
    normalized = [_normalize_trace(trace) for trace in traces]
    for trace in normalized:
        trace.pop("spans", None)
    return {"available": True, "note": None, "count": len(normalized), "traces": normalized}


def get_trace(settings: Settings, trace_id: str) -> dict[str, Any] | None:
    mlflow, _ = _mlflow(settings)
    if mlflow is None:
        return None
    try:
        raw = mlflow.get_trace(trace_id, silent=True)
    except Exception:  # pragma: no cover
        return None
    if raw is None:
        return None
    return _normalize_trace(raw)


def recent_trace_data(settings: Settings, limit: int = 20) -> dict[str, Any]:
    """Recent traces *with their spans*, for the chat agent to answer trace questions."""
    mlflow, note = _mlflow(settings)
    if mlflow is None:
        return {"available": False, "note": note, "traces": []}
    try:
        experiment = mlflow.get_experiment_by_name(settings.mlflow_experiment)
    except Exception as exc:  # pragma: no cover
        return {"available": False, "note": f"cannot reach the tracking store: {exc}", "traces": []}
    if experiment is None:
        return {
            "available": True,
            "note": f"experiment {settings.mlflow_experiment!r} has no traces yet",
            "traces": [],
        }
    try:
        traces = mlflow.search_traces(
            locations=[experiment.experiment_id],
            max_results=limit,
            include_spans=True,
            return_type="list",
        )
    except Exception as exc:  # pragma: no cover
        return {"available": False, "note": f"cannot list traces: {exc}", "traces": []}
    normalized = [_normalize_trace(trace) for trace in traces]
    return {"available": True, "note": None, "traces": normalized}


def agent_status(settings: Settings, limit: int = 10) -> dict[str, Any]:
    """A deterministic one-line status of the agent, computed from recent traces."""
    data = recent_trace_data(settings, limit=limit)
    if not data["available"]:
        return {
            "available": False,
            "level": "unknown",
            "label": "Agent status unknown",
            "detail": data["note"],
            "runs": 0,
        }
    traces = data["traces"]
    if not traces:
        return {
            "available": True,
            "level": "unknown",
            "label": "No runs yet",
            "detail": "The pipeline has not produced any traces.",
            "runs": 0,
        }

    daily = [trace for trace in traces if trace.get("root_span") == "pipeline.run_daily"]
    error_spans = [span for t in traces for span in t["spans"] if span.get("status") == "ERROR"]
    hard_errors = [span for span in error_spans if not _is_benign_error(span)]
    budget_stops = [span for span in error_spans if _is_benign_error(span)]

    llm = 0
    template = 0
    for trace in traces:
        for span in trace["spans"]:
            if span.get("name") == "explain_event":
                generator = span.get("attributes", {}).get("generator")
                if generator == "llm":
                    llm += 1
                elif generator == "template":
                    template += 1

    if hard_errors:
        count = len(hard_errors)
        level = "degraded"
        label = f"Agent: {count} error" + ("s" if count != 1 else "")
    else:
        level = "ok"
        label = "Agent healthy"

    parts = []
    if daily:
        parts.append(f"{len(daily)} run" + ("s" if len(daily) != 1 else ""))
    if llm or template:
        parts.append(f"{llm} LLM · {template} template")
    if budget_stops:
        parts.append(f"{len(budget_stops)} budget stop" + ("s" if len(budget_stops) != 1 else ""))
    detail = " · ".join(parts) or None

    return {
        "available": True,
        "level": level,
        "label": label,
        "detail": detail,
        "runs": len(daily),
    }


def trace_analytics(
    settings: Settings, *, since_days: int = 30, limit: int = 300
) -> dict[str, Any]:
    """Aggregated trace analytics over a window, for the agent to answer questions.

    Aggregation is done here in Python — counts, averages and maxima — rather than
    left to the model, so the answer's numbers are exact and reproducible.
    """
    mlflow, note = _mlflow(settings)
    if mlflow is None:
        return {"available": False, "note": note}

    try:
        experiment = mlflow.get_experiment_by_name(settings.mlflow_experiment)
    except Exception as exc:  # pragma: no cover
        return {"available": False, "note": f"cannot reach the tracking store: {exc}"}
    if experiment is None:
        return {"available": True, "note": "no traces yet", "traces": 0, "runs": 0}

    since_ms = int(time.time() * 1000) - since_days * 86_400_000
    try:
        traces = mlflow.search_traces(
            locations=[experiment.experiment_id],
            filter_string=f"trace.timestamp_ms >= {since_ms}",
            max_results=limit,
            include_spans=True,
            return_type="list",
        )
    except Exception as exc:  # pragma: no cover
        return {"available": False, "note": f"cannot list traces: {exc}"}

    normalized = [_normalize_trace(trace) for trace in traces]

    span_buckets: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    budget_stops: list[dict[str, Any]] = []
    fallbacks: list[dict[str, Any]] = []
    generator = {"llm": 0, "template": 0}
    tokens = {"prompt": 0, "completion": 0}
    cost = 0.0
    runs: list[dict[str, Any]] = []

    for trace in normalized:
        root = trace["spans"][0] if trace["spans"] else {}
        root_attrs = root.get("attributes", {})
        if trace.get("root_span") == "pipeline.run_daily":
            runs.append(
                {
                    "run_id": trace.get("run_id"),
                    "timestamp_ms": trace.get("timestamp_ms"),
                    "status": trace.get("status"),
                    "duration_ms": trace.get("duration_ms"),
                    "events_published": root_attrs.get("events_published"),
                    "llm_calls": root_attrs.get("llm_calls"),
                    "completeness": root_attrs.get("completeness"),
                }
            )

        for span in trace["spans"]:
            name = span["name"]
            latency = span.get("latency_ms")
            bucket = span_buckets.setdefault(
                name, {"name": name, "count": 0, "total_ms": 0, "max_ms": 0, "errors": 0}
            )
            bucket["count"] += 1
            if latency is not None:
                bucket["total_ms"] += latency
                bucket["max_ms"] = max(bucket["max_ms"], latency)
            if span.get("status") == "ERROR":
                attrs = span.get("attributes", {})
                if _is_benign_error(span):
                    budget_stops.append(
                        {
                            "span": name,
                            "run_id": trace.get("run_id"),
                            "city": attrs.get("city_id"),
                            "note": attrs.get("error_message") or attrs.get("error_type"),
                        }
                    )
                else:
                    bucket["errors"] += 1
                    errors.append(
                        {
                            "span": name,
                            "run_id": trace.get("run_id"),
                            "city": attrs.get("city_id"),
                            "error": attrs.get("error_message") or attrs.get("error_type"),
                        }
                    )

            attrs = span.get("attributes", {})
            if span.get("name") == "explain_event":
                gen = attrs.get("generator")
                if gen == "llm":
                    generator["llm"] += 1
                elif gen == "template":
                    generator["template"] += 1
                if attrs.get("fallback_reason"):
                    fallbacks.append(
                        {
                            "city": attrs.get("city_id"),
                            "metric": attrs.get("metric"),
                            "reason": attrs.get("fallback_reason"),
                        }
                    )

            token_pairs = (("prompt", "prompt_tokens"), ("completion", "completion_tokens"))
            for token_key, attr_key in token_pairs:
                value = attrs.get(attr_key)
                if isinstance(value, (int, float)):
                    tokens[token_key] += int(value)
            usd = attrs.get("estimated_usd")
            if isinstance(usd, (int, float)):
                cost += float(usd)

    spans = []
    for bucket in span_buckets.values():
        spans.append(
            {
                "name": bucket["name"],
                "count": bucket["count"],
                "avg_ms": round(bucket["total_ms"] / bucket["count"]) if bucket["count"] else None,
                "max_ms": bucket["max_ms"],
                "errors": bucket["errors"],
            }
        )
    spans.sort(key=lambda item: -(item["avg_ms"] or 0))
    runs.sort(key=lambda item: item["timestamp_ms"] or 0, reverse=True)

    return {
        "available": True,
        "note": None,
        "window_days": since_days,
        "traces": len(normalized),
        "runs": len(runs),
        "spans": spans,
        "errors": errors[:100],
        "budget_stops": budget_stops[:100],
        "fallbacks": fallbacks[:100],
        "generator": generator,
        "tokens": tokens,
        "estimated_usd": round(cost, 4),
        "recent_runs": runs[:30],
    }
