"""Graceful degradation at the command boundary.

These tests exercise the commands with fake stores, because the behaviour under
test is "what does the user see when a store is empty, partial, or unreachable" —
not the store internals, which are covered elsewhere.
"""

from __future__ import annotations

from types import SimpleNamespace

from wo.commands.summary import run as run_summary
from wo.commands.trace import run as run_trace
from wo.commands.traces import run as run_traces
from wo.config import Config
from wo.models import Coverage, RunRecord, SpanNode, TraceRecord
from wo.stores.base import StoreUnavailable


class FakeTraceStore:
    def __init__(self, *, exists=True, count=0, traces=(), trace=None, unavailable=False):
        self._exists = exists
        self._count = count
        self._traces = list(traces)
        self._trace = trace
        self._unavailable = unavailable

    def _guard(self):
        if self._unavailable:
            raise StoreUnavailable("tracking store is down")

    def experiment_exists(self, experiment):
        self._guard()
        return self._exists

    def count_traces(self, experiment):
        self._guard()
        return self._count

    def list_traces(self, experiment, limit=None):
        self._guard()
        return self._traces

    def get_trace(self, trace_id):
        self._guard()
        return self._trace


class FakeAppDbStore:
    def __init__(self, *, coverage=None, runs=(), mix=None, pricing=False, unavailable=False):
        self._coverage = coverage
        self._runs = list(runs)
        self._mix = mix or {}
        self._pricing = pricing
        self._unavailable = unavailable

    def _guard(self):
        if self._unavailable:
            raise StoreUnavailable("application database is down")

    def coverage(self):
        self._guard()
        return self._coverage

    def recent_runs(self, limit):
        self._guard()
        return self._runs

    def explanation_mix(self, run_ids):
        self._guard()
        return self._mix

    def pricing_configured(self):
        self._guard()
        return self._pricing


def _config():
    return Config(mlflow_experiment="weather-outliers")


def _trace(*spans):
    return TraceRecord(
        trace_id="tr-1",
        status="OK",
        timestamp_ms=1_700_000_000_000,
        duration_ms=100,
        tags={},
        metadata={},
        spans=tuple(spans),
    )


def _span(name="root", parent=None):
    return SpanNode(
        span_id=name,
        parent_span_id=parent,
        name=name,
        span_type="CHAIN",
        status="OK",
        start_ms=None,
        end_ms=None,
        latency_ms=10,
        attributes={"run_id": "run-1"},
    )


def test_summary_reports_empty_gracefully(capsys):
    app = FakeAppDbStore(coverage=Coverage(0, 0, 0, None), runs=())
    code = run_summary(SimpleNamespace(runs=5), _config(), app, FakeTraceStore())
    out = capsys.readouterr().out
    assert code == 0
    assert "none yet" in out
    assert "0 cities, 0 rows" in out


def test_summary_survives_unavailable_mlflow(capsys):
    app = FakeAppDbStore(coverage=Coverage(10, 100, 90, "1991-2020"), runs=())
    trace = FakeTraceStore(unavailable=True)
    code = run_summary(SimpleNamespace(runs=5), _config(), app, trace)
    out = capsys.readouterr().out
    assert code == 0
    assert "unavailable" in out


def test_summary_survives_unavailable_app_db(capsys):
    app = FakeAppDbStore(unavailable=True)
    code = run_summary(SimpleNamespace(runs=5), _config(), app, FakeTraceStore())
    out = capsys.readouterr().out
    assert code == 0
    assert "unavailable" in out


def test_summary_renders_cost_as_not_priced(capsys):
    run = RunRecord(
        run_id="run-1",
        kind="daily",
        analysis_date="2026-09-21",
        status="succeeded",
        published=True,
        started_at="2026-09-22 09:30",
        duration_ms=1000,
        cities_with_data=50,
        cities_total=50,
        completeness=1.0,
        events_published=10,
        events_total=10,
        llm_calls=0,
        llm_estimated_usd=0.0,
        error=None,
        error_type=None,
    )
    app = FakeAppDbStore(coverage=Coverage(50, 1000, 900, "1991-2020"), runs=[run], pricing=False)
    code = run_summary(SimpleNamespace(runs=5), _config(), app, FakeTraceStore())
    out = capsys.readouterr().out
    assert code == 0
    assert "not priced" in out


def test_traces_reports_no_traces_distinctly(capsys):
    code = run_traces(SimpleNamespace(limit=20), _config(), None, FakeTraceStore(traces=()))
    out = capsys.readouterr().out
    assert code == 0
    assert "no traces recorded" in out


def test_traces_unavailable_exits_nonzero(capsys):
    code = run_traces(SimpleNamespace(limit=20), _config(), None, FakeTraceStore(unavailable=True))
    out = capsys.readouterr().out
    assert code == 1
    assert "unavailable" in out


def test_traces_renders_rows(capsys):
    store = FakeTraceStore(traces=[_trace(_span("root"))])
    code = run_traces(SimpleNamespace(limit=20), _config(), None, store)
    out = capsys.readouterr().out
    assert code == 0
    assert "tr-1" in out
    assert "root" in out


def test_trace_not_found_exits_nonzero(capsys):
    trace_store = FakeTraceStore(trace=None)
    code = run_trace(SimpleNamespace(trace_id="tr-missing"), _config(), None, trace_store)
    out = capsys.readouterr().out
    assert code == 1
    assert "not found" in out


def test_trace_renders_span_tree(capsys):
    store = FakeTraceStore(trace=_trace(_span("root"), _span("child", parent="root")))
    code = run_trace(SimpleNamespace(trace_id="tr-1"), _config(), None, store)
    out = capsys.readouterr().out
    assert code == 0
    assert "trace tr-1" in out
    assert "root" in out
    assert "child" in out
