"""Monitoring data: run/event queries and trace normalisation."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.monitoring import _normalize_span, _normalize_trace, recent_evals, recent_runs


def test_recent_runs_orders_and_maps(session):
    from app.domain import METHODOLOGY_VERSION
    from app.models import PipelineRun

    session.add(
        PipelineRun(
            id="run-a",
            kind="daily",
            status="succeeded",
            started_at=datetime(2026, 9, 22, 10, 0, tzinfo=UTC),
            methodology_version=METHODOLOGY_VERSION,
            llm_calls=3,
        )
    )
    session.add(
        PipelineRun(
            id="run-b",
            kind="daily",
            status="failed",
            started_at=datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
            methodology_version=METHODOLOGY_VERSION,
            error="boom",
            error_type="ValueError",
        )
    )
    session.commit()

    runs = recent_runs(session, limit=10)
    assert [run["run_id"] for run in runs] == ["run-a", "run-b"]
    assert runs[0]["llm_calls"] == 3
    assert runs[1]["status"] == "failed"
    assert runs[1]["error"] == "boom"


def test_recent_evals_orders_and_maps(session):
    from app.models import EvaluationReport

    session.add(
        EvaluationReport(
            generated_at=datetime(2026, 9, 23, tzinfo=UTC),
            status="passed",
            methodology_version="1.0.0",
            suites_total=8,
            suites_passed=8,
            cases_total=138,
            cases_passed=138,
            duration_ms=1000,
        )
    )
    session.commit()

    reports = recent_evals(session, limit=10)
    assert len(reports) == 1
    assert reports[0]["status"] == "passed"
    assert reports[0]["cases_passed"] == 138


def test_normalize_span_is_defensive():
    span = _normalize_span(
        SimpleNamespace(
            span_id="s1",
            parent_id=None,
            name="root",
            span_type="CHAIN",
            status=SimpleNamespace(status_code=SimpleNamespace(value="OK")),
            start_time_ns=1_000_000_000,
            end_time_ns=3_000_000_000,
            attributes={"run_id": "r", "latency_ms": 42, "mlflow.spanType": "CHAIN"},
        )
    )
    assert span["name"] == "root"
    assert span["status"] == "OK"
    assert span["latency_ms"] == 42
    assert span["attributes"]["run_id"] == "r"
    assert "mlflow.spanType" not in span["attributes"]


def test_normalize_span_missing_attributes_becomes_empty():
    span = _normalize_span(SimpleNamespace(span_id="s", parent_id=None, name="n", span_type=None, status=None))
    assert span["status"] == "UNKNOWN"
    assert span["latency_ms"] is None
    assert span["attributes"] == {}


def test_normalize_trace_finds_model_on_child_span():
    raw = SimpleNamespace(
        info=SimpleNamespace(
            trace_id="tr-1",
            state=SimpleNamespace(value="OK"),
            request_time=1_700_000_000_000,
            execution_duration=10,
        ),
        data=SimpleNamespace(
            spans=[
                SimpleNamespace(
                    span_id="r",
                    parent_id=None,
                    name="pipeline.run_daily",
                    span_type="CHAIN",
                    status=SimpleNamespace(status_code=SimpleNamespace(value="OK")),
                    start_time_ns=1,
                    end_time_ns=2,
                    attributes={"llm_provider": "openai"},
                ),
                SimpleNamespace(
                    span_id="e",
                    parent_id="r",
                    name="explain_event",
                    span_type="AGENT",
                    status=SimpleNamespace(status_code=SimpleNamespace(value="OK")),
                    start_time_ns=1,
                    end_time_ns=2,
                    attributes={"model": "deepseek-chat"},
                ),
            ]
        ),
    )
    trace = _normalize_trace(raw)
    assert trace["trace_id"] == "tr-1"
    assert trace["root_span"] == "pipeline.run_daily"
    assert trace["model"] == "deepseek-chat"
    assert trace["span_count"] == 2
