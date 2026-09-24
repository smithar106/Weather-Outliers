"""Live LLM evaluation and comparison.

    backend/.venv/bin/python -m evals.live --events 5 [--prompt-b-file PATH] [--model-b MODEL]

Runs the golden set of ranked events through the *real* investigator — the
tool-calling agent with a live LLM (the configured provider, DeepSeek here) — and
scores each explanation on:

* whether it survived the production grounding guard (``validation.ok``);
* whether it was actually model-generated rather than a template fallback;
* how many tools it called, its latency, and its token cost.

Then it runs a second configuration — a candidate prompt and/or model — over the
same events and prints a side-by-side comparison.

Two properties are deliberate. **This costs money**, so it is run by hand and is
never part of CI. **The golden events come from the scratch world**, not from any
weather provider: they are real ranked events with stored calculations, so the
comparison is reproducible — the same events, the same ground truth, two prompts.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from evals import environment
from evals.harness import git_state, ratio


@dataclass(frozen=True)
class LiveConfig:
    label: str
    prompt_text: str
    model: str | None = None


@dataclass(slots=True)
class EventResult:
    event_id: str
    city: str
    metric: str
    generator: str
    grounded: bool
    violations: tuple[str, ...]
    tool_call_count: int
    attempts: int
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    estimated_usd: float
    fallback_reason: str | None


@dataclass(slots=True)
class Summary:
    label: str
    events: int
    llm_generated: int
    template_generated: int
    grounded: int
    grounding_rate: float | None
    avg_tool_calls: float | None
    avg_latency_ms: float | None
    prompt_tokens: int
    completion_tokens: int
    estimated_usd: float
    violations: dict[str, int] = field(default_factory=dict)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.live",
        description="Evaluate the live LLM explanations and compare two configurations.",
    )
    parser.add_argument(
        "--events", type=int, default=5, help="Top-N events to explain (default 5)."
    )
    parser.add_argument(
        "--prompt-b-file",
        type=Path,
        default=None,
        help="Path to a candidate system prompt for configuration B.",
    )
    parser.add_argument(
        "--model-b",
        default=None,
        help="Model name for configuration B (defaults to the configured model).",
    )
    return parser.parse_args(argv)


def _settings_for(model: str | None):
    from app.config import get_settings

    settings = get_settings()
    if not model:
        return settings
    field_name = "openai_model" if settings.llm_provider == "openai" else "anthropic_model"
    return settings.model_copy(update={field_name: model})


def _client_for(settings):
    from app.agent.llm import get_llm_client

    client = get_llm_client(settings)
    if client is None:
        raise SystemExit(
            "no LLM configured — set LLM_PROVIDER, an API key, and a model in the environment"
        )
    return client


def golden_events(world, n: int) -> list[tuple[int, object, object]]:
    """The top-N ranked events and their cities, from the scratch world."""
    from sqlalchemy import select

    from app.models import AnomalyEvent, City, DailyRanking, PipelineRun

    with world.session() as session:
        run = session.scalars(
            select(PipelineRun)
            .where(PipelineRun.published.is_(True))
            .order_by(PipelineRun.analysis_date.desc())
            .limit(1)
        ).first()
        if run is None:
            return []
        return list(
            session.execute(
                select(DailyRanking.rank, AnomalyEvent, City)
                .join(AnomalyEvent, AnomalyEvent.id == DailyRanking.event_id)
                .join(City, City.id == AnomalyEvent.city_id)
                .where(DailyRanking.run_id == run.id)
                .order_by(DailyRanking.rank)
                .limit(n)
            ).all()
        )


def evaluate(config: LiveConfig, events, world) -> list[EventResult]:
    from app.agent.investigator import investigate_events

    settings = _settings_for(config.model)
    client = _client_for(settings)
    meta = {event.id: (city.name, event.metric) for _, event, city in events}

    try:
        with world.session() as session:
            batch = investigate_events(
                session, events, settings, client, system_prompt=config.prompt_text
            )
            results = []
            for outcome in batch.outcomes:
                city, metric = meta[outcome.event_id]
                validation = outcome.validation or {}
                results.append(
                    EventResult(
                        event_id=outcome.event_id,
                        city=city,
                        metric=metric,
                        generator=outcome.generator,
                        grounded=bool(validation.get("ok")),
                        violations=tuple(validation.get("violations") or ()),
                        tool_call_count=outcome.tool_call_count,
                        attempts=outcome.attempts,
                        latency_ms=outcome.latency_ms,
                        prompt_tokens=outcome.prompt_tokens,
                        completion_tokens=outcome.completion_tokens,
                        estimated_usd=outcome.estimated_usd,
                        fallback_reason=outcome.fallback_reason,
                    )
                )
    finally:
        client.close()
    return results


def aggregate(label: str, results: list[EventResult]) -> Summary:
    n = len(results)
    llm = sum(1 for result in results if result.generator == "llm")
    grounded = sum(1 for result in results if result.grounded)
    violations: Counter[str] = Counter()
    for result in results:
        violations.update(result.violations)
    return Summary(
        label=label,
        events=n,
        llm_generated=llm,
        template_generated=n - llm,
        grounded=grounded,
        grounding_rate=ratio(grounded, n),
        avg_tool_calls=ratio(sum(r.tool_call_count for r in results), n),
        avg_latency_ms=ratio(sum(r.latency_ms for r in results), n),
        prompt_tokens=sum(r.prompt_tokens for r in results),
        completion_tokens=sum(r.completion_tokens for r in results),
        estimated_usd=round(sum(r.estimated_usd for r in results), 4),
        violations=dict(violations),
    )


def render_comparison(a: Summary, b: Summary) -> str:
    def pct(value: float | None) -> str:
        return "—" if value is None else f"{value * 100:.0f}%"

    def num(value: float | None) -> str:
        return "—" if value is None else f"{value:.1f}"

    rows = [
        ("Events", str(a.events), str(b.events)),
        ("LLM-generated", str(a.llm_generated), str(b.llm_generated)),
        ("Template fallback", str(a.template_generated), str(b.template_generated)),
        ("Grounded (passed guards)", f"{a.grounded}/{a.events}", f"{b.grounded}/{b.events}"),
        ("Grounding rate", pct(a.grounding_rate), pct(b.grounding_rate)),
        ("Avg tool calls", num(a.avg_tool_calls), num(b.avg_tool_calls)),
        ("Avg latency", f"{num(a.avg_latency_ms)} ms", f"{num(b.avg_latency_ms)} ms"),
        ("Prompt tokens", str(a.prompt_tokens), str(b.prompt_tokens)),
        ("Completion tokens", str(a.completion_tokens), str(b.completion_tokens)),
        ("Estimated cost", f"${a.estimated_usd:.4f}", f"${b.estimated_usd:.4f}"),
    ]

    width = max(len(row[0]) for row in rows)
    lines = [f"{'metric':<{width}}  {a.label:<16}  {b.label}"]
    lines.append("-" * (width + 2 + 16 + 2 + 16))
    for label, av, bv in rows:
        lines.append(f"{label:<{width}}  {av:<16}  {bv}")
    return "\n".join(lines)


def _prompt_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _slug(label: str) -> str:
    import re

    return re.sub(r"[^0-9a-z]+", "_", label.lower()).strip("_") or "config"


def _metrics_for(prefix: str, summary: Summary) -> dict[str, float]:
    metrics: dict[str, float] = {
        f"{prefix}.events": summary.events,
        f"{prefix}.llm_generated": summary.llm_generated,
        f"{prefix}.template_generated": summary.template_generated,
        f"{prefix}.grounded": summary.grounded,
        f"{prefix}.prompt_tokens": summary.prompt_tokens,
        f"{prefix}.completion_tokens": summary.completion_tokens,
        f"{prefix}.estimated_usd": summary.estimated_usd,
    }
    if summary.grounding_rate is not None:
        metrics[f"{prefix}.grounding_rate"] = summary.grounding_rate
    if summary.avg_tool_calls is not None:
        metrics[f"{prefix}.avg_tool_calls"] = summary.avg_tool_calls
    if summary.avg_latency_ms is not None:
        metrics[f"{prefix}.avg_latency_ms"] = summary.avg_latency_ms
    return metrics


def log_comparison(
    config_a: LiveConfig,
    config_b: LiveConfig,
    summary_a: Summary,
    summary_b: Summary,
    results_a: list[EventResult],
    results_b: list[EventResult],
) -> str | None:
    """Log the comparison to MLflow as one run. Best-effort; never raises."""
    from app.observability import tracing

    mlflow = tracing.import_mlflow()
    if mlflow is None:
        print("  (mlflow not installed — comparison not logged)", file=sys.stderr)
        return None

    uri = os.environ.get("MLFLOW_TRACKING_URI") or (
        f"sqlite:///{environment.REPO_ROOT / 'mlflow.db'}"
    )
    base = os.environ.get("MLFLOW_EXPERIMENT", "weather-outliers")
    experiment = os.environ.get("MLFLOW_LIVE_EXPERIMENT") or f"{base}-live"

    commit, dirty = git_state(environment.REPO_ROOT)
    from app.agent.investigator import prompt_identity
    from app.config import get_settings

    identity = prompt_identity()
    settings = get_settings()
    model_a = config_a.model or settings.openai_model or settings.anthropic_model
    model_b = config_b.model or settings.openai_model or settings.anthropic_model

    params = {
        "events": summary_a.events,
        "llm_provider": settings.llm_provider,
        "a.prompt_sha256": identity["prompt_sha256"],
        "b.prompt_sha256": _prompt_sha(config_b.prompt_text),
        "a.model": model_a,
        "b.model": model_b,
        "git_commit": commit or "unknown",
        "git_dirty": dirty,
    }

    try:
        mlflow.set_tracking_uri(uri)
        mlflow.set_experiment(experiment)
        run_name = f"live-{datetime.now(UTC):%Y%m%dT%H%M%S}"
        with mlflow.start_run(run_name=run_name) as run:
            mlflow.set_tags(
                {
                    "evaluation.kind": "live_llm",
                    "evaluation.generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
            )
            mlflow.log_params({k: str(v) for k, v in params.items()})
            mlflow.log_metrics(_metrics_for(_slug(config_a.label), summary_a))
            mlflow.log_metrics(_metrics_for(_slug(config_b.label), summary_b))
            mlflow.log_dict(
                {
                    "a": [asdict(result) for result in results_a],
                    "b": [asdict(result) for result in results_b],
                },
                "results.json",
            )
            return f"{experiment} · {run.info.run_id}"
    except Exception as exc:  # pragma: no cover - telemetry is best-effort
        print(f"  (MLflow logging failed: {exc})", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    environment.configure(live_llm=True)

    from app.agent.investigator import SYSTEM_PROMPT
    from evals.world import build_world

    world = build_world(skip_explanations=True)
    events = golden_events(world, args.events)
    if not events:
        print("no ranked events in the scratch world", flush=True)
        world.dispose()
        return 2

    prompt_b = SYSTEM_PROMPT
    if args.prompt_b_file:
        prompt_b = args.prompt_b_file.read_text(encoding="utf-8")

    config_a = LiveConfig(label="current", prompt_text=SYSTEM_PROMPT)
    config_b = LiveConfig(label="candidate", prompt_text=prompt_b, model=args.model_b)

    print(f"evaluating {len(events)} events against the live LLM…", flush=True)
    try:
        results_a = evaluate(config_a, events, world)
        results_b = evaluate(config_b, events, world)
    finally:
        world.dispose()

    summary_a = aggregate(config_a.label, results_a)
    summary_b = aggregate(config_b.label, results_b)

    print()
    print(render_comparison(summary_a, summary_b))
    print()

    location = log_comparison(config_a, config_b, summary_a, summary_b, results_a, results_b)
    print(f"  logged to {location}" if location else "  not logged to MLflow")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
