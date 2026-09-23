"""The evaluation entry point.

    backend/.venv/bin/python -m evals.runner

Writes ``evals/reports/latest.json``, prints a summary, and exits non-zero if any
suite failed — which is what makes it usable as a CI gate as well as the source
for the evaluation dashboard.

The order of operations matters and is not negotiable: the environment is pinned
*before* anything from ``app`` is imported, so a developer's ``.env`` cannot point
the evaluation at a real database or a paid model. Every suite import is therefore
deferred into :func:`execute`, which :func:`main` calls once the pins are in place.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evals import environment
from evals.harness import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_PASSED,
    Report,
    Suite,
    describe_environment,
    git_state,
    now_iso,
)

DEFAULT_OUTPUT = environment.REPO_ROOT / "evals" / "reports" / "latest.json"

#: Stated in every report, because the numbers above them are only meaningful with
#: these attached.
LIMITATIONS: tuple[str, ...] = (
    "Every figure in this report was measured by the run that produced it. Nothing "
    "is hardcoded; a suite that cannot measure something reports null.",
    "The pipeline, API and published-explanation checks run against "
    "'synthetic_fixture_v1', a deterministic synthetic weather provider. They "
    "measure whether the machinery is correct and reproducible, not the accuracy "
    "of real weather data.",
    "Latency figures come from an in-process client over an in-memory SQLite "
    "database. They are a floor for the deployed system, not a measurement of it.",
    "The anomaly-score evaluator compares production against independent arithmetic "
    "in two regimes. Where the observation sits at or beyond the fifth order "
    "statistic from either end the stored quantile sketch is lossless and exact "
    "agreement is required. In the interior the sketch interpolates across its "
    "probability grid, so the two are only required to agree within the width of the "
    "grid interval that brackets the observation — a property of the interpolation, "
    "not a tolerance chosen to make the run pass. The measured worst case is "
    "reported either way.",
    "Grounding accuracy is measured on a small hand-labelled case set. It "
    "quantifies the guards, not any language model's general reliability.",
    "LLM spend is an estimate derived from configured token prices. When no prices "
    "are configured it reads 0.0, which means 'not priced' rather than 'free'.",
    "The city registry is a curated sample of major North American cities. It is "
    "not a representative sample of the continent's climate, and the rankings are "
    "statistical outliers within that sample rather than records of any kind.",
)

_STATUS_MARK = {
    STATUS_PASSED: "PASS",
    STATUS_FAILED: "FAIL",
    STATUS_ERROR: "ERROR",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.runner",
        description="Run the Weather Outliers evaluation suites and write a report.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Where to write the JSON report (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--live-llm",
        action="store_true",
        help=(
            "Leave LLM_PROVIDER and the API keys as configured, so the grounding "
            "suite additionally measures a real model. Costs money."
        ),
    )
    parser.add_argument(
        "--judge",
        action="store_true",
        help=(
            "Additionally run the optional model-based explanation-quality evaluator. "
            "Keeps the model API keys in the environment but still generates the "
            "explanations from templates, so the only paid call is the judging. "
            "Costs money."
        ),
    )
    parser.add_argument(
        "--skip-unit-tests",
        action="store_true",
        help="Skip the pytest suite. Useful while iterating on the other suites.",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="SUITE",
        help="Run only the named suite. Repeatable.",
    )
    return parser.parse_args(argv)


def print_summary(report: Report) -> None:
    payload = report.to_dict()
    totals = payload["totals"]

    print()
    print(f"Weather Outliers evaluation — {payload['status'].upper()}")
    commit = payload["git_commit"] or "unknown commit"
    dirty = " (uncommitted changes present)" if payload["git_dirty"] else ""
    print(f"  {payload['generated_at']}  ·  {commit}{dirty}")
    print(
        f"  methodology {payload['methodology_version']}  ·  "
        f"weather {payload['environment']['weather_provider']}  ·  "
        f"explanations {payload['environment']['llm_mode']}"
    )
    print()

    width = max((len(s["title"]) for s in payload["suites"]), default=10)
    for suite in payload["suites"]:
        mark = _STATUS_MARK.get(suite["status"], suite["status"].upper())
        print(
            f"  {mark:<5} {suite['title']:<{width}}  "
            f"{suite['cases_passed']}/{suite['cases_total']} checks  "
            f"{suite['duration_ms']} ms"
        )
        if suite["error"]:
            for line in suite["error"].strip().splitlines()[-3:]:
                print(f"        {line}")
        for case in suite["cases"]:
            if not case["passed"]:
                print(
                    f"        · {case['id']}: expected {case['expected']}, got {case['observed']}"
                )
        for note in suite["notes"]:
            print(f"        note: {note}")

    print()
    print(
        f"  {totals['suites_passed']}/{totals['suites_total']} suites, "
        f"{totals['cases_passed']}/{totals['cases_total']} checks, "
        f"{totals['duration_ms']} ms total"
    )
    print()


def execute(
    *,
    judge: bool = False,
    skip_unit_tests: bool = False,
    only: list[str] | None = None,
    verdict_path: Path | None = None,
) -> Report | None:
    """Run the selected suites and return the report.

    ``None`` means the scratch world could not publish a board, which is a setup
    failure rather than an evaluation result — there is nothing to report on.

    Separate from :func:`main` so ``python -m evals.experiment`` can run exactly
    the same suites and then log them, instead of shelling out to this module and
    parsing its output back.

    The caller must have run :func:`evals.environment.configure` already.
    """
    # Imported here, not at module scope: they pull in ``app``, which must not be
    # imported before the environment is pinned.
    from app.domain import METHODOLOGY_VERSION
    from app.providers.fixture import DATASET
    from evals.suites import (
        anomaly_score,
        api_contract,
        explanation_agreement,
        explanation_quality,
        grounding,
        missing_data,
        ranking,
        reproducibility,
        unit_tests,
    )
    from evals.world import build_world

    wanted = set(only or [])

    def selected(suite_id: str) -> bool:
        return not wanted or suite_id in wanted

    suites: list[Suite] = []

    if selected(unit_tests.SUITE_ID) and not skip_unit_tests:
        suites.append(unit_tests.run())

    # Neither of these needs a database — they score generated baselines directly,
    # so they run before the scratch world exists and still report if building it
    # fails.
    if selected(anomaly_score.SUITE_ID):
        suites.append(anomaly_score.run())
    if selected(missing_data.SUITE_ID):
        suites.append(missing_data.run())

    world_suites = (
        grounding.SUITE_ID,
        reproducibility.SUITE_ID,
        api_contract.SUITE_ID,
        ranking.SUITE_ID,
        explanation_agreement.SUITE_ID,
    ) + ((explanation_quality.SUITE_ID,) if judge else ())
    needs_world = any(selected(sid) for sid in world_suites)
    world = build_world() if needs_world else None

    if world is not None:
        if world.run_report is None or world.run_report.status != "succeeded":
            detail = world.run_report.error if world.run_report else "no run report"
            print(f"scratch world could not publish a board: {detail}", file=sys.stderr)
            world.dispose()
            return None

        if selected(ranking.SUITE_ID):
            suites.append(ranking.run(world))
        if selected(grounding.SUITE_ID):
            suites.append(grounding.run(world, llm_mode=environment.llm_mode()))
        if selected(explanation_agreement.SUITE_ID):
            suites.append(explanation_agreement.run(world))
        # Optional and off by default: the only suite in the harness that spends
        # money, and the only one whose result is an opinion.
        if judge and selected(explanation_quality.SUITE_ID):
            suites.append(explanation_quality.run(world, verdict_path=verdict_path))
        if selected(reproducibility.SUITE_ID):
            suites.append(reproducibility.run(world))
        # Last, because it redirects ``app.db`` at the scratch engine for the rest
        # of the process.
        if selected(api_contract.SUITE_ID):
            suites.append(api_contract.run(world))

    commit, dirty = git_state(environment.REPO_ROOT)
    report = Report(
        generated_at=now_iso(),
        git_commit=commit,
        git_dirty=dirty,
        methodology_version=METHODOLOGY_VERSION,
        environment=describe_environment(
            weather_provider=DATASET,
            llm_mode=environment.llm_mode(),
        ),
        suites=suites,
        limitations=list(LIMITATIONS),
    )

    if world is not None:
        world.dispose()

    return report


def write_report(report: Report, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    environment.configure(live_llm=args.live_llm, keep_keys=args.judge)

    report = execute(
        judge=args.judge,
        skip_unit_tests=args.skip_unit_tests,
        only=args.only,
        verdict_path=args.output.with_name("judge-verdicts.json"),
    )
    if report is None:
        return 2

    write_report(report, args.output)
    print_summary(report)
    print(f"  report written to {args.output}")
    print()

    return 0 if report.status == STATUS_PASSED else 1


if __name__ == "__main__":
    raise SystemExit(main())
