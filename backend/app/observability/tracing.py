"""MLflow tracing, made optional and made incapable of breaking a run.

Three properties matter more here than any span this module produces.

**1. MLflow is not a dependency of the application.** It is an extra:
``pip install -e "./backend[tracing]"``. The import is attempted once, lazily, and
an ``ImportError`` is a configuration outcome rather than a crash. This matters
because the deployed image installs the base dependency set only — the backend
is deliberately free of numpy/pandas/scipy (see ``pyproject.toml``) and even
``mlflow-skinny`` is weight the API container has no use for. A pipeline that
could only run where MLflow was installed would be a pipeline whose telemetry had
become load-bearing.

**2. Tracing is off unless it is turned on.** ``MLFLOW_TRACING_ENABLED`` defaults
to false, so an operator who has never heard of MLflow gets exactly the behaviour
they had before it existed, and no network call is made to a tracking server they
did not configure.

**3. Telemetry cannot fail a run.** Every call into MLflow is wrapped. A dead
tracking server, a permissions error, a version skew in the tracing API — all of
them degrade to "no trace was recorded" and are logged once. After
``_MAX_FAILURES`` faults tracing disables itself for the life of the process,
because a tracking server that is down at the first span is down at the
five-hundredth, and retrying it that many times converts a telemetry outage into
a latency problem.

**What is and is not recorded.** This application handles no personal data of any
kind: its inputs are public weather figures and its outputs are published on a
public website, so span inputs and outputs carry the real content — the prompt,
the model's reply, the explanation being validated — because a span holding only
``{"documents": 20}`` cannot be used to debug anything. What is excluded is
configuration: API keys, ``DATABASE_URL``, and provider request URLs, which carry
query strings and can carry keys. Attribute keys are filtered through
:data:`_SENSITIVE_KEY` as a backstop against a future caller passing one of those
by accident — a backstop, not a licence to rely on it in place of not passing
them.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

logger = logging.getLogger(__name__)

# MLflow's own span-type vocabulary. Spelled as literals so that importing this
# module never requires MLflow to be present.
SPAN_CHAIN = "CHAIN"
SPAN_AGENT = "AGENT"
SPAN_LLM = "LLM"
SPAN_TOOL = "TOOL"
SPAN_RETRIEVER = "RETRIEVER"
SPAN_PARSER = "PARSER"
SPAN_UNKNOWN = "UNKNOWN"

#: Attribute keys that are dropped rather than recorded, whatever their value.
_SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|secret|password|passwd|token|credential|authorization|cookie"
    r"|database_url|dsn|connection_string)",
    re.IGNORECASE,
)

#: Consecutive MLflow faults tolerated before tracing switches itself off.
_MAX_FAILURES = 3


@dataclass
class _State:
    active: bool = False
    #: Human-readable explanation of why tracing is or is not running. Surfaced by
    #: :func:`describe` so a report can state its own telemetry status instead of
    #: leaving a reader to infer it from an absence of spans.
    reason: str = "not configured"
    experiment: str = ""
    tracking_uri: str = ""
    mlflow: Any = None
    failures: int = 0
    #: Static attributes stamped onto every root span: methodology version,
    #: prompt version, environment. Set by :func:`configure`.
    defaults: dict[str, Any] = field(default_factory=dict)


_state = _State()


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def import_mlflow() -> Any | None:
    """Return the ``mlflow`` module, or ``None`` if it is not installed.

    Shared with the evaluation harness, which logs runs and metrics rather than
    traces but needs the same "absent is not an error" semantics.
    """
    try:
        import mlflow
    except ImportError:
        return None
    return mlflow


def configure(
    settings: Any | None = None, *, extra_defaults: Mapping[str, Any] | None = None
) -> bool:
    """Turn tracing on if it is enabled and MLflow is importable. Never raises.

    Returns whether tracing ended up active. Safe to call more than once; the
    last call wins, which is what lets a test enable and disable it.
    """
    if settings is None:  # pragma: no cover - convenience path
        from app.config import get_settings

        settings = get_settings()

    _state.failures = 0
    _state.defaults = dict(extra_defaults or {})

    if not getattr(settings, "mlflow_tracing_enabled", False):
        _state.active = False
        _state.mlflow = None
        _state.reason = "disabled (MLFLOW_TRACING_ENABLED is false)"
        return False

    mlflow = import_mlflow()
    if mlflow is None:
        _state.active = False
        _state.reason = (
            "requested but mlflow is not installed; install the extra with "
            "pip install -e \"./backend[tracing]\""
        )
        logger.warning("MLflow tracing %s", _state.reason)
        return False

    uri = (getattr(settings, "mlflow_tracking_uri", "") or "").strip()
    experiment = (getattr(settings, "mlflow_experiment", "") or "weather-outliers").strip()
    try:
        if uri:
            mlflow.set_tracking_uri(uri)
        mlflow.set_experiment(experiment)
    except Exception as exc:
        _state.active = False
        _state.reason = f"MLflow setup failed: {type(exc).__name__}: {exc}"
        logger.warning("MLflow tracing disabled — %s", _state.reason)
        return False

    _state.mlflow = mlflow
    _state.active = True
    _state.tracking_uri = uri or str(_safe(mlflow.get_tracking_uri) or "")
    _state.experiment = experiment
    _state.reason = f"active (experiment={experiment!r})"

    _warn_if_unauthenticated(_state.tracking_uri)
    logger.info(
        "MLflow tracing active: experiment=%s tracking_uri=%s",
        experiment,
        _redact_uri(_state.tracking_uri),
    )
    return True


def disable(reason: str = "disabled") -> None:
    """Stop tracing for the rest of the process."""
    _state.active = False
    _state.mlflow = None
    _state.reason = reason


def is_active() -> bool:
    return _state.active


def describe() -> dict[str, Any]:
    """Telemetry status, for a report that would rather state it than imply it."""
    return {
        "enabled": _state.active,
        "status": _state.reason,
        "experiment": _state.experiment or None,
        "tracking_uri": _redact_uri(_state.tracking_uri) or None,
        "mlflow_installed": import_mlflow() is not None,
    }


def flush() -> None:
    """Best-effort flush of asynchronously logged traces before the process exits."""
    if not _state.active:
        return
    for name in ("flush_trace_async_logging", "flush_async_logging"):
        fn = getattr(_state.mlflow, name, None)
        if callable(fn):
            _safe(fn)
            return


# ---------------------------------------------------------------------------
# spans
# ---------------------------------------------------------------------------


class SpanHandle:
    """Collects one span's attributes and I/O, whether or not anything is listening.

    Everything accumulates here and is written to MLflow once, at span exit. Two
    reasons: a caller can set a value it only learns at the end (a row count, a
    fallback reason, the model that actually answered) without a second code path,
    and there is exactly one place where a telemetry failure has to be caught.
    """

    __slots__ = ("attributes", "inputs", "name", "outputs")

    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, Any] = {}
        self.inputs: Any = None
        self.outputs: Any = None

    def set(self, **attrs: Any) -> SpanHandle:
        for key, value in attrs.items():
            if value is None:
                continue
            if _SENSITIVE_KEY.search(key):
                continue
            self.attributes[key] = value
        return self

    def set_inputs(self, inputs: Any) -> SpanHandle:
        self.inputs = _scrub(inputs)
        return self

    def set_outputs(self, outputs: Any) -> SpanHandle:
        self.outputs = _scrub(outputs)
        return self


@contextmanager
def span(
    name: str,
    *,
    span_type: str = SPAN_UNKNOWN,
    root: bool = False,
    **attributes: Any,
) -> Iterator[SpanHandle]:
    """Record one span. The body runs identically whether tracing is on or off.

    ``root=True`` additionally stamps the process-wide defaults (methodology
    version, prompt version, environment) so that every trace is self-describing
    without each call site repeating them.
    """
    handle = SpanHandle(name)
    handle.set(**attributes)
    if root:
        handle.set(**_state.defaults)

    manager = None
    mlspan = None
    started = perf_counter()
    if _state.active:
        try:
            manager = _state.mlflow.start_span(name=name, span_type=span_type)
            # The value ``__enter__`` yields is the span; the manager itself is
            # not. Keeping only the manager produced spans with a name and a
            # duration and nothing else, which in the trace UI is indistinguishable
            # from a span that legitimately has no I/O.
            mlspan = manager.__enter__()
        except Exception as exc:
            manager = None
            mlspan = None
            _note_failure("start_span", exc)

    try:
        yield handle
    except BaseException as exc:
        handle.set(
            status="ERROR", error_type=type(exc).__name__, error_message=str(exc)[:500]
        )
        _close(manager, mlspan, handle, started, exc)
        raise
    handle.attributes.setdefault("status", "OK")
    _close(manager, mlspan, handle, started, None)


def _close(
    manager: Any,
    mlspan: Any,
    handle: SpanHandle,
    started: float,
    exc: BaseException | None,
) -> None:
    """Write the accumulated attributes and I/O, then end the span. Swallows everything."""
    handle.attributes["latency_ms"] = int((perf_counter() - started) * 1000)
    if manager is None:
        return
    try:
        target = mlspan if mlspan is not None else manager
        for attr, value in (
            ("set_inputs", handle.inputs),
            ("set_outputs", handle.outputs),
        ):
            if value is None:
                continue
            fn = getattr(target, attr, None)
            if callable(fn):
                fn(value)
        setter = getattr(target, "set_attributes", None)
        if callable(setter):
            setter(_scrub(handle.attributes))
    except Exception as attr_exc:
        _note_failure("set_attributes", attr_exc)
    try:
        if exc is None:
            manager.__exit__(None, None, None)
        else:
            manager.__exit__(type(exc), exc, exc.__traceback__)
    except Exception as exit_exc:
        _note_failure("end_span", exit_exc)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _note_failure(where: str, exc: BaseException) -> None:
    _state.failures += 1
    logger.warning(
        "MLflow tracing fault in %s (%d/%d): %s: %s",
        where,
        _state.failures,
        _MAX_FAILURES,
        type(exc).__name__,
        exc,
    )
    if _state.failures >= _MAX_FAILURES:
        disable(f"auto-disabled after {_MAX_FAILURES} faults; last was {type(exc).__name__}")
        logger.warning(
            "MLflow tracing disabled for this process: %s. The run continues untraced.",
            _state.reason,
        )


def _scrub(value: Any) -> Any:
    """Drop mapping entries whose key looks like a credential, recursively."""
    if isinstance(value, Mapping):
        return {
            k: _scrub(v) for k, v in value.items() if not _SENSITIVE_KEY.search(str(k))
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def _redact_uri(uri: str) -> str:
    """Strip any ``user:password@`` from a tracking URI before it is logged."""
    return re.sub(r"//[^/@]*@", "//", uri or "")


def _warn_if_unauthenticated(uri: str) -> None:
    """Say something when the tracking server looks like it is open to the world.

    MLflow's own server ships without authentication. Pointing this at a plain
    ``http://`` host that is not loopback is how a trace store becomes a public
    one, so it gets a warning rather than a silent success. See EVALUATION.md.
    """
    if not uri.startswith("http://"):
        return
    host = uri.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0]
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return
    logger.warning(
        "MLFLOW_TRACKING_URI is plain http:// to a non-local host (%s). MLflow's "
        "tracking server has no authentication of its own: put it behind TLS and "
        "an authenticating proxy, or use a file/databricks URI instead.",
        host,
    )


def _safe(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None
