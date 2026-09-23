"""Evaluator 4 — missing, insufficient and unusable data.

The interesting failure mode of a ranking system is not a wrong number, it is a
*confident* number computed from data that could not support it. A day when two
thirds of the fetches failed still yields a plausible-looking top ten. So this
evaluator checks that every kind of absence produces either a named exclusion or
nothing at all, and never a score presented as if the data were there.

Three levels, because absence can enter at three places:

* **Scoring** — one city-metric-day with no baseline, too short a baseline, too
  few wet days, or a missing/non-finite observation. Driven by the
  ``synthetic_failure`` and ``edge`` rows of ``evals/cases/scoring.json``.
* **Publication gate** — a run whose city coverage falls under the configured
  completeness floor must raise rather than publish.
* **Exclusion vocabulary** — every reason the scorer can emit must be one the API
  schema and the methodology document both know about, so a reader who sees
  "why is my city missing" can look the answer up.

Deliberately *not* re-tested here: that a failed run leaves the previous board
standing. ``evals/suites/reproducibility.py`` already covers that with
``failed_run_does_not_publish`` and ``failed_run_preserves_board``, and a second
copy would inflate the check count without adding evidence.
"""

from __future__ import annotations

import traceback

from evals.dataset import build_baseline, load_dataset
from evals.harness import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_PASSED,
    Case,
    Metric,
    Suite,
    Timer,
    ratio,
)

SUITE_ID = "missing_data"
TITLE = "Missing-data handling"
DESCRIPTION = (
    "Every form of absent or insufficient input produces a named exclusion or no "
    "candidate at all — never a score that looks as though the data were present."
)

#: Every exclusion reason the scorer is allowed to emit. A new one appearing here
#: without being added to the methodology document and the API schema is the
#: failure this list exists to catch.
KNOWN_EXCLUSION_REASONS = frozenset(
    {
        "no_baseline",
        "insufficient_baseline",
        "dry_day_not_ranked",
        "insufficient_wet_days",
        "tail_probability_unavailable",
        "below_score_threshold",
    }
)


def run() -> Suite:
    suite = Suite(id=SUITE_ID, title=TITLE, description=DESCRIPTION)
    timer = Timer()

    try:
        with timer:
            from app.config import Settings
            from app.domain import Metric as AppMetric
            from app.stats.anomaly import compute_anomaly

            settings = Settings()
            dataset = load_dataset()
            suite.notes.append(f"dataset {dataset.dataset_version}")

            # -- level 1: scoring ----------------------------------------------
            observed_reasons: set[str] = set()
            scored_anyway = 0

            for case in dataset.cases:
                expect = case.expect
                if not (case.expects_none or "excluded_reason" in expect):
                    continue

                baseline = build_baseline(case)
                candidate = compute_anomaly(
                    city_id="eval",
                    local_date="2025-07-15",
                    metric=AppMetric(case.metric),
                    observed_value=case.observed,
                    baseline=baseline,
                    min_wet_days=case.min_wet_days_override or settings.baseline_min_wet_days,
                    min_score=settings.ranking_min_score,
                )

                if case.expects_none:
                    suite.cases.append(
                        Case(
                            id=case.id,
                            title=case.title,
                            passed=candidate is None,
                            category=case.category,
                            expected="no candidate at all",
                            observed=(
                                "None"
                                if candidate is None
                                else f"candidate with score {candidate.anomaly_score}"
                            ),
                            detail=case.note,
                        )
                    )
                    continue

                wanted = expect["excluded_reason"]
                if candidate is None:
                    suite.cases.append(
                        Case(
                            id=case.id,
                            title=case.title,
                            passed=False,
                            category=case.category,
                            expected=f"excluded: {wanted}",
                            observed="None — the reason was dropped instead of recorded",
                            detail=case.note,
                        )
                    )
                    continue

                if candidate.excluded_reason:
                    observed_reasons.add(candidate.excluded_reason)
                if candidate.eligible:
                    scored_anyway += 1

                passed = candidate.eligible is False and candidate.excluded_reason == wanted
                suite.cases.append(
                    Case(
                        id=case.id,
                        title=case.title,
                        passed=passed,
                        category=case.category,
                        expected=f"ineligible, reason={wanted}",
                        observed=(
                            f"eligible={candidate.eligible}, reason={candidate.excluded_reason}"
                        ),
                        detail=case.note,
                    )
                )

            # An ineligible candidate is still *returned*, carrying its arithmetic,
            # so the reason a city is absent from the board is inspectable rather
            # than invisible. Check that the trace survives exclusion.
            gate_case = next(c for c in dataset.cases if c.id == "fail_insufficient_years")
            gate_candidate = compute_anomaly(
                city_id="eval",
                local_date="2025-07-15",
                metric=AppMetric(gate_case.metric),
                observed_value=gate_case.observed,
                baseline=build_baseline(gate_case),
                min_score=settings.ranking_min_score,
            )
            has_trace = bool(
                gate_candidate
                and gate_candidate.evidence
                and gate_candidate.evidence.get("eligibility", {}).get("excluded_reason")
                and gate_candidate.evidence.get("baseline", {}).get("n_years") is not None
            )
            suite.cases.append(
                Case(
                    id="exclusion_keeps_its_evidence",
                    title="An excluded candidate still carries the evidence for why",
                    passed=has_trace,
                    category="synthetic_failure",
                    expected="evidence contains the exclusion reason and the baseline it was judged against",
                    observed="present" if has_trace else "missing or empty",
                    detail=(
                        "Exclusions are dropped from the board but not from the record. "
                        "Without this, 'my city is missing' has no answer."
                    ),
                )
            )

            # -- level 2: publication gate -------------------------------------
            # Constructed directly rather than by sabotaging a pipeline run: the
            # question here is whether the gate's arithmetic and threshold are
            # right, and reproducibility.py already exercises the live path.
            from app.pipeline.runner import PipelineError, RunReport

            floor = settings.pipeline_min_city_completeness
            gate_checks = [
                ("just_below_floor", floor - 0.01, True),
                ("exactly_at_floor", floor, False),
                ("well_below_floor", floor / 2.0, True),
                ("complete", 1.0, floor > 1.0),
                ("no_cities_reported", 0.0, True),
            ]
            for label, completeness, should_raise in gate_checks:
                report = RunReport(
                    run_id=f"eval-{label}",
                    kind="daily",
                    analysis_date=None,
                    status="running",
                    published=False,
                    duration_ms=0,
                    cities_total=50,
                    cities_with_data=round(completeness * 50),
                    completeness=completeness,
                )
                raised = False
                try:
                    _invoke_completeness_gate(report, 50, settings)
                except PipelineError:
                    raised = True
                suite.cases.append(
                    Case(
                        id=f"completeness_{label}",
                        title=f"Completeness {completeness:.0%} vs {floor:.0%} floor",
                        passed=raised == should_raise,
                        category="edge",
                        expected="refuses to publish" if should_raise else "publishes",
                        observed="refused" if raised else "published",
                    )
                )

            # -- level 3: exclusion vocabulary ---------------------------------
            unknown = observed_reasons - KNOWN_EXCLUSION_REASONS
            suite.cases.append(
                Case(
                    id="exclusion_vocabulary_closed",
                    title="Every exclusion reason emitted is one the documentation defines",
                    passed=not unknown,
                    category="drift",
                    expected=f"reasons ⊆ {sorted(KNOWN_EXCLUSION_REASONS)}",
                    observed=(
                        f"undocumented: {sorted(unknown)}"
                        if unknown
                        else f"observed {sorted(observed_reasons)}"
                    ),
                )
            )

            uncovered = KNOWN_EXCLUSION_REASONS - observed_reasons
            suite.notes.append(
                "Exclusion reasons not reached by this dataset: "
                + (", ".join(sorted(uncovered)) if uncovered else "none")
            )
            suite.notes.append(
                "'failed run does not overwrite the last good board' is covered by the "
                "reproducibility suite, not duplicated here."
            )

            suite.metrics = [
                Metric("Dataset version", dataset.dataset_version),
                Metric(
                    "Absence cases exercised",
                    sum(
                        1 for c in dataset.cases if c.expects_none or "excluded_reason" in c.expect
                    ),
                ),
                Metric(
                    "Exclusion reasons reached",
                    f"{len(observed_reasons)}/{len(KNOWN_EXCLUSION_REASONS)}",
                    detail=", ".join(sorted(observed_reasons)) or None,
                ),
                Metric(
                    "Ineligible cases that were scored anyway",
                    scored_anyway,
                    detail="must be 0",
                ),
                Metric(
                    "Completeness floor",
                    floor,
                    unit="fraction of registry",
                    detail="a run under this refuses to publish",
                ),
                Metric(
                    "Checks passed",
                    ratio(sum(1 for c in suite.cases if c.passed), len(suite.cases)),
                    unit="fraction",
                ),
            ]

        failed = [c for c in suite.cases if not c.passed]
        suite.status = STATUS_FAILED if failed else STATUS_PASSED
        suite.duration_ms = timer.elapsed_ms

    except Exception:
        suite.status = STATUS_ERROR
        suite.error = traceback.format_exc(limit=6)
        suite.duration_ms = timer.elapsed_ms

    return suite


def _invoke_completeness_gate(report, city_count: int, settings) -> None:
    """Call the pipeline's own completeness gate without building a pipeline.

    ``_require_completeness`` is a method on ``Pipeline`` but reads only
    ``self.settings``, so a stand-in with that one attribute exercises the real
    threshold arithmetic. Reimplementing the comparison here would have tested
    this file instead of the application.
    """
    from app.pipeline.runner import Pipeline

    stub = type("_SettingsOnly", (), {"settings": settings})()
    Pipeline._require_completeness(stub, report, city_count)
