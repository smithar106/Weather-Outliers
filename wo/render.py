"""Plain-text rendering for the CLI.

Everything here is pure: it takes model objects and returns strings, so it can be
tested without touching MLflow or a database. The style follows the repository's
existing CLI output — short, aligned, and explicit about what was *not* measured.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from wo.aggregate import build_span_tree
from wo.models import Coverage, RunRecord, SpanNode, TraceRecord, TraceSummary
from wo.normalize import public_attributes

# ---------------------------------------------------------------------------
# formatting primitives
# ---------------------------------------------------------------------------


def format_duration_ms(ms: int | None) -> str:
    if ms is None:
        return "—"
    if ms >= 1000:
        return f"{ms / 1000:.1f}s"
    return f"{ms}ms"


def format_timestamp_ms(ms: int | None) -> str:
    if ms is None:
        return "—"
    try:
        return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return str(ms)


def format_cost(usd: float | None, *, pricing_configured: bool) -> str:
    """A cost figure that cannot be mistaken for "free".

    ``None`` means "no data". ``0.0`` with pricing unconfigured means "nobody told
    the system what tokens cost", which is different from a measured zero — so the
    two are rendered differently.
    """
    if usd is None:
        return "—"
    if not pricing_configured:
        return f"${usd:.4f} (not priced)"
    return f"${usd:.4f}"


def _abbrev(value: str | None, width: int) -> str:
    if value is None:
        return "—"
    if len(value) > width:
        return value[: width - 1] + "…"
    return value


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SummaryData:
    experiment: str
    mlflow_status: str
    trace_count: int | None
    coverage: Coverage | None
    coverage_note: str | None
    runs: tuple[RunRecord, ...]
    generator_mix: Mapping[str, int]
    pricing_configured: bool


def render_summary(data: SummaryData) -> str:
    lines: list[str] = [f"weather-outliers — summary  (experiment {data.experiment!r})", ""]

    lines.append(f"  mlflow:      {data.mlflow_status}")
    if data.trace_count is not None:
        lines.append(f"  traces:      {data.trace_count}")

    if data.coverage is not None:
        cov = data.coverage
        lines.append(
            f"  baselines:   {cov.cities_with_baselines} cities, {cov.rows} rows "
            f"({cov.sufficient_rows} sufficient)"
        )
        if cov.reference_period:
            lines.append(f"               reference period {cov.reference_period}")
    elif data.coverage_note:
        lines.append(f"  baselines:   unavailable ({data.coverage_note})")
    else:
        lines.append("  baselines:   —")

    pricing = "configured" if data.pricing_configured else "not configured"
    lines.append(f"  pricing:     {pricing}")

    model_written = int(data.generator_mix.get("llm", 0))
    deterministic = int(data.generator_mix.get("template", 0))
    if data.generator_mix:
        lines.append(
            f"  explanations: {model_written} model-written, {deterministic} deterministic "
            "(across recent runs)"
        )
    lines.append("")

    if not data.runs:
        lines.append("  runs:        none yet — run the pipeline first")
        return "\n".join(lines)

    lines.append("  recent runs:")
    for run in data.runs:
        flag = "published" if run.published else "unpublished"
        date_part = run.analysis_date or "—"
        lines.append(
            f"    {run.started_at or '—':<16} {run.kind:<9} {run.status:<9} {flag:<11} "
            f"{date_part}  {run.events_published} events  "
            f"{run.cities_with_data}/{run.cities_total} cities"
        )
        cost = format_cost(run.llm_estimated_usd, pricing_configured=data.pricing_configured)
        lines.append(f"        llm: {run.llm_calls} calls, {cost}")
        if run.error:
            lines.append(f"        error: {run.error_type or 'error'}: {run.error}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# traces listing
# ---------------------------------------------------------------------------


def render_trace_table(summaries: Sequence[TraceSummary]) -> str:
    header = (
        f"{'TRACE ID':<13} {'TIME (UTC)':<17} {'STATUS':<8} {'DURATION':<9} "
        f"{'SPANS':<6} {'ROOT SPAN':<24} {'MODEL':<16} {'RUN':<22}"
    )
    lines = [header, "-" * len(header)]
    for summary in summaries:
        lines.append(
            f"{_abbrev(summary.trace_id, 12):<13} "
            f"{format_timestamp_ms(summary.timestamp_ms):<17} "
            f"{summary.status:<8} "
            f"{format_duration_ms(summary.duration_ms):<9} "
            f"{summary.span_count:<6} "
            f"{_abbrev(summary.root_span, 23):<24} "
            f"{_abbrev(summary.model, 15):<16} "
            f"{_abbrev(summary.run_id, 21):<22}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# single trace
# ---------------------------------------------------------------------------


def render_trace(record: TraceRecord) -> str:
    lines = [
        f"trace {record.trace_id}",
        f"  status {record.status}  ·  "
        f"{format_timestamp_ms(record.timestamp_ms)}  ·  "
        f"{format_duration_ms(record.duration_ms)}  ·  {len(record.spans)} spans",
        "",
    ]
    for span, depth in build_span_tree(record.spans):
        indent = "  " + "  " * depth
        attrs = public_attributes(span.attributes)
        detail = _span_detail(span, attrs)
        lines.append(
            f"{indent}{span.name:<24} {span.status:<8} "
            f"{format_duration_ms(span.latency_ms):<9}{detail}"
        )
    return "\n".join(lines)


def _span_detail(span: SpanNode, attrs: Mapping[str, object]) -> str:
    parts: list[str] = []
    if span.span_type:
        parts.append(span.span_type)
    for key in ("model", "prompt_version", "generator", "fallback_reason"):
        value = attrs.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    return ("  " + " ".join(str(p) for p in parts)) if parts else ""
