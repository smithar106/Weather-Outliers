"""The claim this file exists to back: telemetry cannot change what the app does.

Every test here is about the failure modes rather than the happy path, because
the happy path of a tracing shim is the part nobody is worried about. A span must
be transparent when MLflow is absent, when MLflow is present but broken, and when
the traced body raises — and in the third case the exception must arrive at the
caller unchanged.
"""

from __future__ import annotations

import hashlib

import pytest

from app.agent.investigator import (
    PROMPT_SHA256,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    prompt_identity,
)
from app.config import Settings
from app.observability import tracing


@pytest.fixture(autouse=True)
def _reset_tracing():
    """Tracing is process-global state, so every test starts and ends with it off."""
    tracing.disable("test setup")
    yield
    tracing.disable("test teardown")


class _FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict = {}
        self.outputs = None

    def set_attributes(self, attrs: dict) -> None:
        self.attributes.update(attrs)

    def set_outputs(self, outputs) -> None:
        self.outputs = outputs


class _FakeSpanContext:
    def __init__(self, recorder: list, name: str, *, fail_on: str | None = None) -> None:
        self.span = _FakeSpan()
        self._recorder = recorder
        self._name = name
        self._fail_on = fail_on
        self.exit_args: tuple | None = None
        if fail_on == "enter":
            raise RuntimeError("tracking server refused the connection")

    def __enter__(self):
        return self.span

    def __exit__(self, exc_type, exc, tb):
        self.exit_args = (exc_type, exc, tb)
        self._recorder.append((self._name, self.span.attributes))
        if self._fail_on == "exit":
            raise RuntimeError("flush failed")
        return False


class _FakeMlflow:
    """Enough of MLflow's surface for the shim, and nothing more."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.spans: list = []
        self.experiment: str | None = None
        self.uri: str | None = None
        self._fail_on = fail_on

    def set_tracking_uri(self, uri: str) -> None:
        self.uri = uri

    def get_tracking_uri(self) -> str:
        return self.uri or "file:./mlruns"

    def set_experiment(self, name: str) -> None:
        self.experiment = name

    def start_span(self, *, name: str, span_type: str = "UNKNOWN"):
        return _FakeSpanContext(self.spans, name, fail_on=self._fail_on)


def _settings(**overrides) -> Settings:
    base = {
        "mlflow_tracing_enabled": True,
        "mlflow_tracking_uri": "file:./mlruns-test",
        "mlflow_experiment": "unit-test",
    }
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# off by default
# ---------------------------------------------------------------------------


def test_tracing_is_off_unless_enabled():
    assert tracing.configure(Settings()) is False
    assert tracing.is_active() is False
    assert "disabled" in tracing.describe()["status"]


def test_missing_mlflow_is_a_configuration_outcome_not_an_error(monkeypatch):
    monkeypatch.setattr(tracing, "import_mlflow", lambda: None)
    assert tracing.configure(_settings()) is False
    assert tracing.is_active() is False
    assert "not installed" in tracing.describe()["status"]


def test_span_is_transparent_when_inactive():
    with tracing.span("stage", answer=42) as sp:
        sp.set(rows=7)
        value = 1 + 1
    assert value == 2
    assert sp.attributes["answer"] == 42
    assert sp.attributes["rows"] == 7
    assert sp.attributes["status"] == "OK"
    assert sp.attributes["latency_ms"] >= 0


def test_settings_default_leaves_tracing_off():
    assert Settings().mlflow_tracing_enabled is False


# ---------------------------------------------------------------------------
# on, and recording the right things
# ---------------------------------------------------------------------------


def test_active_tracing_records_attributes(monkeypatch):
    fake = _FakeMlflow()
    monkeypatch.setattr(tracing, "import_mlflow", lambda: fake)
    assert tracing.configure(_settings(), extra_defaults={"environment": "test"}) is True

    with tracing.span("pipeline.run_daily", root=True, city_count=50) as sp:
        sp.set(events_published=10)

    assert fake.experiment == "unit-test"
    assert len(fake.spans) == 1
    name, attrs = fake.spans[0]
    assert name == "pipeline.run_daily"
    assert attrs["city_count"] == 50
    assert attrs["events_published"] == 10
    # `root=True` stamps the process-wide defaults so a trace is self-describing.
    assert attrs["environment"] == "test"
    assert attrs["status"] == "OK"


def test_exception_is_reraised_and_recorded(monkeypatch):
    fake = _FakeMlflow()
    monkeypatch.setattr(tracing, "import_mlflow", lambda: fake)
    tracing.configure(_settings())

    with (
        pytest.raises(ValueError, match=r"completeness 0\.4 below floor"),
        tracing.span("require_completeness"),
    ):
        raise ValueError("completeness 0.4 below floor")

    _name, attrs = fake.spans[0]
    assert attrs["status"] == "ERROR"
    assert attrs["error_type"] == "ValueError"
    assert "completeness" in attrs["error_message"]


def test_credential_shaped_attributes_are_dropped(monkeypatch):
    fake = _FakeMlflow()
    monkeypatch.setattr(tracing, "import_mlflow", lambda: fake)
    tracing.configure(_settings())

    with tracing.span("llm_completion") as sp:
        sp.set(
            model="claude-sonnet-5",
            anthropic_api_key="sk-should-never-appear",
            database_url="postgresql://u:p@host/db",
            nested={"authorization": "Bearer x", "tokens": 12},
        )

    _name, attrs = fake.spans[0]
    assert attrs["model"] == "claude-sonnet-5"
    assert "anthropic_api_key" not in attrs
    assert "database_url" not in attrs
    # The scrub is recursive, so a nested mapping cannot smuggle one through.
    assert attrs["nested"] == {}


def test_tracking_uri_credentials_are_not_logged():
    assert (
        tracing._redact_uri("http://user:secret@mlflow.example/api")
        == "http://mlflow.example/api"
    )


# ---------------------------------------------------------------------------
# broken MLflow
# ---------------------------------------------------------------------------


def test_span_start_failure_does_not_break_the_body(monkeypatch):
    fake = _FakeMlflow(fail_on="enter")
    monkeypatch.setattr(tracing, "import_mlflow", lambda: fake)
    tracing.configure(_settings())

    results = []
    for _ in range(2):
        with tracing.span("score_anomalies") as sp:
            results.append(sp.name)

    assert results == ["score_anomalies", "score_anomalies"]


def test_repeated_faults_disable_tracing_for_the_process(monkeypatch):
    fake = _FakeMlflow(fail_on="enter")
    monkeypatch.setattr(tracing, "import_mlflow", lambda: fake)
    tracing.configure(_settings())

    for _ in range(tracing._MAX_FAILURES):
        with tracing.span("stage"):
            pass

    assert tracing.is_active() is False
    assert "auto-disabled" in tracing.describe()["status"]


def test_span_exit_failure_is_swallowed(monkeypatch):
    fake = _FakeMlflow(fail_on="exit")
    monkeypatch.setattr(tracing, "import_mlflow", lambda: fake)
    tracing.configure(_settings())

    with tracing.span("rank_events") as sp:
        sp.set(events_published=10)
    # No exception escaped, and the attributes were still handed over before the
    # failing exit.
    assert fake.spans[0][1]["events_published"] == 10


def test_mlflow_setup_failure_disables_tracing(monkeypatch):
    class _Broken:
        def set_tracking_uri(self, uri):
            raise OSError("no such directory")

    monkeypatch.setattr(tracing, "import_mlflow", lambda: _Broken())
    assert tracing.configure(_settings()) is False
    assert "MLflow setup failed" in tracing.describe()["status"]


def test_flush_is_a_noop_when_inactive():
    tracing.flush()  # must not raise


# ---------------------------------------------------------------------------
# prompt identity
# ---------------------------------------------------------------------------


def test_prompt_digest_is_derived_from_the_prompt():
    expected = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12]
    assert expected == PROMPT_SHA256
    assert len(PROMPT_SHA256) == 12


def test_prompt_identity_carries_version_and_digest():
    identity = prompt_identity()
    assert identity == {
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": PROMPT_SHA256,
    }
