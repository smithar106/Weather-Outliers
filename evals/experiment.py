"""Run the evaluation suites and log the result to MLflow as one experiment run.

    backend/.venv/bin/python -m evals.experiment

This is the repeatable command. It runs exactly the same suites as
``python -m evals.runner`` — it calls :func:`evals.runner.execute`, it does not
re-implement or shell out to it — and then records what produced the numbers:
the commit, the methodology version, the prompt version and hash, the dataset
version, the model identity, every numeric metric each suite measured, latency,
and cost where a suite was able to price it.

Three properties are deliberate.

**MLflow is optional.** If ``mlflow`` is not installed the evaluation still runs,
the report is still written, and the exit code is still the evaluation's own
verdict. Missing telemetry is reported as missing telemetry, not as a failed
evaluation. The same holds if the tracking store rejects the connection halfway
through: logging is best effort and its failures are warnings.

**Configuration comes from the environment.** ``MLFLOW_TRACKING_URI`` and
``MLFLOW_EVAL_EXPERIMENT`` are read from the environment, never hardcoded to a
server and never written to a config file. With no tracking URI set, runs go to a
local SQLite file inside the repository, which is private by construction.
Nothing here starts or expects a tracking server.

**Only an allowlist is logged.** Parameters and tags are built one at a time
below; nothing iterates over the environment or over settings. A last-line guard
(:func:`_redact`) additionally drops any value that happens to contain the text of
a credential-shaped environment variable, so a mistake upstream cannot leak one.

With ``--trace`` the pipeline that the suites exercise also emits MLflow spans
into the same experiment, so a run and the traces it produced sit together. That
is off by default because a traced run is not a like-for-like latency comparison
with an untraced one.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from evals import environment, runner
from evals.harness import STATUS_PASSED, Report, ratio

#: Where runs go when ``MLFLOW_TRACKING_URI`` is unset. A file in the repository,
#: reachable by nobody but this machine. Note the scheme: MLflow 3 rejects bare
#: ``file:`` tracking URIs, so the local default has to be SQLite.
DEFAULT_TRACKING_URI = f"sqlite:///{environment.REPO_ROOT / 'mlflow.db'}"

#: Evaluation runs are kept out of the experiment that production tracing would
#: use, so an evaluation of the machinery is never mistaken for a real pipeline
#: run over real weather.
DEFAULT_EXPERIMENT_SUFFIX = "-evals"

#: Environment variables whose *values* must never appear in a logged string.
_CREDENTIAL_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|DSN|DATABASE_URL)", re.I)

#: Shortest credential value worth guarding against. Below this a match is far
#: more likely to be a coincidence than a leak.
_MIN_SECRET_LEN = 12

#: ``"5/5"``, ``"96/96"`` — how several suites report a count of their own cases.
_FRACTION = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.experiment",
        description=(
            "Run the evaluation suites and log the run to MLflow. "
            "Tracking configuration is read from the environment."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=runner.DEFAULT_OUTPUT,
        help=f"Where to write the JSON report (default: {runner.DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--run-name",
        default=None,
        help="MLflow run name. Defaults to the commit and a UTC timestamp.",
    )
    parser.add_argument(
        "--judge",
        action="store_true",
        help=(
            "Also run the optional model-based explanation-quality evaluator. "
            "Needs a judge API key in the environment. Costs money."
        ),
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help=(
            "Enable MLflow tracing inside the pipeline the suites exercise, so the "
            "run's spans land in the same experiment. Adds tracing overhead to the "
            "reported latencies."
        ),
    )
    parser.add_argument(
        "--skip-unit-tests",
        action="store_true",
        help="Skip the pytest suite. Useful while iterating.",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="SUITE",
        help="Run only the named suite. Repeatable.",
    )
    return parser.parse_args(argv)


def tracking_uri() -> str:
    return (os.environ.get("MLFLOW_TRACKING_URI") or "").strip() or DEFAULT_TRACKING_URI


def experiment_name() -> str:
    explicit = (os.environ.get("MLFLOW_EVAL_EXPERIMENT") or "").strip()
    if explicit:
        return explicit
    base = (os.environ.get("MLFLOW_EXPERIMENT") or "weather-outliers").strip()
    return f"{base}{DEFAULT_EXPERIMENT_SUFFIX}"


def _secrets() -> list[str]:
    """Credential-shaped environment values, longest first."""
    found = {
        value
        for name, value in os.environ.items()
        if _CREDENTIAL_NAME.search(name) and len(value or "") >= _MIN_SECRET_LEN
    }
    return sorted(found, key=len, reverse=True)


def _redact(text: str, secrets: list[str]) -> str:
    """Last line of defence. Nothing upstream is supposed to reach this."""
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, "[redacted]")
    return text


def _safe_uri(uri: str) -> str:
    """A tracking URI fit to print: scheme and host, never userinfo or a query."""
    parsed = urlsplit(uri)
    if parsed.scheme in ("sqlite", "file", ""):
        return uri
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


def _slug(label: str) -> str:
    """A metric key MLflow will accept, derived from a human-written label."""
    return re.sub(r"[^0-9a-z]+", "_", label.lower()).strip("_") or "unnamed"


def _params(report: Report, args: argparse.Namespace) -> dict[str, str]:
    """The explicit allowlist. Every entry is named here, by hand."""
    from app.agent.investigator import prompt_identity
    from app.agent.llm import pricing_configured
    from app.config import get_settings
    from evals.dataset import load_dataset
    from evals.suites import explanation_quality

    settings = get_settings()
    dataset = load_dataset()
    identity = prompt_identity()
    env = report.environment

    params: dict[str, Any] = {
        "git_commit": report.git_commit or "unknown",
        "git_dirty": report.git_dirty,
        "methodology_version": report.methodology_version,
        "prompt_version": identity.get("prompt_version"),
        "prompt_sha256": identity.get("prompt_sha256"),
        "dataset_version": dataset.dataset_version,
        "dataset_schema_version": dataset.schema_version,
        "dataset_cases": len(dataset.cases),
        "weather_provider": env.get("weather_provider"),
        "llm_mode": env.get("llm_mode"),
        "llm_provider": settings.llm_provider,
        "database": env.get("database"),
        "python": env.get("python"),
        "platform": env.get("platform"),
        "llm_pricing_configured": pricing_configured(),
        "suites_requested": ",".join(args.only) if args.only else "all",
        "unit_tests_skipped": bool(args.skip_unit_tests),
        "tracing_enabled_during_run": bool(args.trace),
    }

    # Model identity, name only. No key, and no base URL — a base URL can carry a
    # token in its path, and the model name is what a reader needs to reproduce.
    if settings.llm_provider == "anthropic":
        params["llm_model"] = settings.anthropic_model
    elif settings.llm_provider == "openai":
        params["llm_model"] = settings.openai_model
    else:
        params["llm_model"] = "none (deterministic templates)"

    params["judge_requested"] = bool(args.judge)
    if args.judge:
        params["judge_provider"] = os.environ.get("EVAL_JUDGE_PROVIDER", "anthropic")
        params["judge_model"] = os.environ.get("EVAL_JUDGE_MODEL", "") or "provider default"
        params["judge_rubric_version"] = explanation_quality.RUBRIC_VERSION
        params["judge_rubric_sha256"] = explanation_quality.rubric_sha()
        params["judge_temperature"] = explanation_quality.TEMPERATURE

    secrets = _secrets()
    return {key: _redact(str(value), secrets) for key, value in params.items() if value is not None}


def _metrics(report: Report) -> dict[str, float]:
    """Totals, per-suite outcomes, and every numeric metric a suite measured.

    Suites that could not measure something report ``None``, and a ``None`` is
    skipped here rather than logged as zero — the distinction between "measured
    zero" and "not measured" survives into MLflow.
    """
    payload = report.to_dict()
    totals = payload["totals"]
    metrics: dict[str, float] = {
        "suites_total": totals["suites_total"],
        "suites_passed": totals["suites_passed"],
        "cases_total": totals["cases_total"],
        "cases_passed": totals["cases_passed"],
        "cases_failed": totals["cases_total"] - totals["cases_passed"],
        "duration_ms": totals["duration_ms"],
    }
    overall = ratio(totals["cases_passed"], totals["cases_total"])
    if overall is not None:
        metrics["pass_rate"] = overall

    cost_total = 0.0
    priced = False

    for suite in payload["suites"]:
        prefix = _slug(suite["id"])
        metrics[f"{prefix}.cases_total"] = suite["cases_total"]
        metrics[f"{prefix}.cases_passed"] = suite["cases_passed"]
        metrics[f"{prefix}.duration_ms"] = suite["duration_ms"]
        rate = ratio(suite["cases_passed"], suite["cases_total"])
        if rate is not None:
            metrics[f"{prefix}.pass_rate"] = rate

        for metric in suite["metrics"]:
            key = f"{prefix}.{_slug(metric['label'])}"
            value = metric["value"]
            # bool is an int in Python; log it as 0/1 deliberately rather than by
            # accident.
            if isinstance(value, bool):
                value = int(value)
            elif isinstance(value, str):
                # Several suites report a count as "5/5", which is right for a
                # report a person reads and useless for comparing two runs. Split
                # it into the two numbers it already contains — no inference, and
                # the string itself stays in the attached report.
                pair = _FRACTION.fullmatch(value.strip())
                if pair:
                    numerator, denominator = float(pair[1]), float(pair[2])
                    metrics[f"{key}_count"] = numerator
                    metrics[f"{key}_total"] = denominator
                    if denominator:
                        metrics[f"{key}_ratio"] = round(numerator / denominator, 4)
                continue
            elif not isinstance(value, (int, float)):
                continue
            metrics[key] = float(value)
            if metric.get("unit") == "USD":
                cost_total += float(value)
                priced = True

    # "Cost when available", in the literal sense. ``estimate_usd`` returns 0.0 when
    # no per-Mtok prices are configured, and a logged 0.0 would read as "this run was
    # free" rather than "nobody told the system what the tokens cost". So the total
    # is only recorded when prices exist to derive it from.
    from app.agent.llm import pricing_configured

    if priced and pricing_configured():
        metrics["cost_usd_estimated_total"] = round(cost_total, 6)

    return metrics


def _tags(report: Report) -> dict[str, str]:
    return {
        "evaluation.status": report.status,
        "evaluation.harness": "evals.runner",
        "evaluation.generated_at": report.generated_at,
    }


def log_to_mlflow(report: Report, args: argparse.Namespace) -> str | None:
    """Log the run. Returns a human-readable location, or ``None`` if it did not.

    Never raises. A telemetry problem is not an evaluation result, and this
    command's exit code belongs to the evaluation.
    """
    from app.observability import tracing

    mlflow = tracing.import_mlflow()
    if mlflow is None:
        print(
            "mlflow is not installed — evaluation ran, nothing logged. Install with:\n"
            '  VIRTUAL_ENV=$PWD/backend/.venv uv pip install "./backend[tracing]"',
            file=sys.stderr,
        )
        return None

    uri = tracking_uri()
    experiment = experiment_name()
    try:
        mlflow.set_tracking_uri(uri)
        mlflow.set_experiment(experiment)
    except Exception as exc:
        print(f"MLflow setup failed, nothing logged: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None

    run_name = args.run_name or f"{report.git_commit or 'nogit'}-{report.generated_at}"
    try:
        with mlflow.start_run(run_name=run_name) as run:
            mlflow.set_tags(_tags(report))
            mlflow.log_params(_params(report, args))
            mlflow.log_metrics(_metrics(report))
            for artifact in (args.output, args.output.with_name("judge-verdicts.json")):
                if artifact.exists():
                    mlflow.log_artifact(str(artifact))
            location = f"{_safe_uri(uri)}  experiment={experiment}  run={run.info.run_id}"
    except Exception as exc:
        print(f"MLflow logging failed part-way: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None

    return location


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Before ``environment.configure``, because that is the last point at which
    # anything can be pinned ahead of the first ``app`` import.
    os.environ.setdefault("MLFLOW_TRACKING_URI", tracking_uri())
    if args.trace:
        os.environ["MLFLOW_TRACING_ENABLED"] = "true"
        os.environ["MLFLOW_EXPERIMENT"] = experiment_name()

    environment.configure(keep_keys=args.judge)

    if args.trace:
        from app.agent.investigator import prompt_identity
        from app.config import get_settings
        from app.domain import METHODOLOGY_VERSION
        from app.observability import tracing

        # Same call the pipeline CLI makes, for the same reason: configured once
        # per process, outside the pipeline, and its result changes no behaviour.
        active = tracing.configure(
            get_settings(),
            extra_defaults={
                "environment": "eval",
                "methodology_version": METHODOLOGY_VERSION,
                "command": "evals.experiment",
                **prompt_identity(),
            },
        )
        if not active:
            print(f"tracing not active: {tracing.describe()['status']}", file=sys.stderr)

    report = runner.execute(
        judge=args.judge,
        skip_unit_tests=args.skip_unit_tests,
        only=args.only,
        verdict_path=args.output.with_name("judge-verdicts.json"),
    )
    if report is None:
        return 2

    runner.write_report(report, args.output)
    runner.print_summary(report)
    print(f"  report written to {args.output}")

    if args.trace:
        from app.observability import tracing

        tracing.flush()

    location = log_to_mlflow(report, args)
    print(f"  logged to {location}" if location else "  not logged to MLflow (see above)")
    print()

    return 0 if report.status == STATUS_PASSED else 1


if __name__ == "__main__":
    raise SystemExit(main())
