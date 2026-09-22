"""Suite 2 — does the grounding layer catch ungrounded prose?

Two measurements, both against the production guards rather than a copy of them:

1. **Labelled cases.** ``evals/cases/grounding.json`` fixes the tool output an
   agent was shown, the prose it produced, and the verdict a correct guard must
   reach. Half the cases are faithful and must be accepted; the rest each carry
   exactly one defect and must be rejected for the right reason. Accepting bad
   prose and rejecting good prose are both failures, and the suite reports them
   separately, because a guard that rejects everything is useless in a way a
   single accuracy number would hide.

2. **Published explanations.** Every explanation the scratch pipeline actually
   published is re-validated against the numbers the read-only tools return for
   its event. With no LLM key configured these are the deterministic templates, so
   this measures whether the fallback the site depends on survives the same guards
   applied to model output. It is not a measurement of any model's quality, and
   the report says which mode it ran in.
"""

from __future__ import annotations

import json
import traceback

from evals.environment import REPO_ROOT
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
from evals.world import World

SUITE_ID = "grounding"
TITLE = "Grounding guards"
DESCRIPTION = (
    "Replays labelled explanation cases through the production guards, then "
    "re-validates every explanation the pipeline published against the numbers "
    "its read-only tools return."
)

CASES_PATH = REPO_ROOT / "evals" / "cases" / "grounding.json"

#: Tools primed before validating a published explanation, in the order the
#: system prompt tells the agent to call them. Priming with the real tools is the
#: point: a whitelist assembled by hand here would not prove the published prose
#: is citable from what the agent could actually see.
PRIMING_CALLS = (
    "get_anomaly_evidence",
    "get_city_weather",
    "get_city_baseline",
    "get_historical_extremes",
    "get_daily_rankings",
)


def _load_cases() -> list[dict]:
    with CASES_PATH.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    cases = payload.get("cases", [])
    if not cases:
        raise ValueError(f"{CASES_PATH} contains no cases")
    return cases


def _run_labelled_cases(suite: Suite) -> dict[str, int]:
    from app.agent.guards import build_allowed_numbers, validate_explanation

    counts = {
        "total": 0,
        "correct": 0,
        # Confusion matrix for the "reject" decision.
        "true_reject": 0,
        "false_reject": 0,
        "true_accept": 0,
        "false_accept": 0,
        "right_reason": 0,
        "reason_expected": 0,
    }

    for spec in _load_cases():
        counts["total"] += 1
        allowed = build_allowed_numbers(
            {float(n) for n in spec.get("tool_numbers", [])},
            [str(t) for t in spec.get("tool_texts", [])],
        )
        report = validate_explanation(
            text=spec["text"],
            evidence_values=list(spec.get("evidence_values", [])),
            allowed_numbers=allowed,
            required_numbers=[float(n) for n in spec.get("required_numbers", [])] or None,
            required_terms=list(spec.get("required_terms", [])) or None,
        )

        expect_ok = bool(spec["expect_ok"])
        decision_correct = report.ok == expect_ok

        prefix = spec.get("expect_violation_prefix")
        reason_correct = True
        if prefix:
            counts["reason_expected"] += 1
            reason_correct = any(v.startswith(prefix) for v in report.violations)
            if reason_correct:
                counts["right_reason"] += 1

        passed = decision_correct and reason_correct
        counts["correct"] += int(decision_correct)
        if expect_ok:
            counts["true_accept" if report.ok else "false_reject"] += 1
        else:
            counts["false_accept" if report.ok else "true_reject"] += 1

        observed = "accepted" if report.ok else f"rejected ({report.summary})"
        detail = spec.get("note")
        if report.negated_terms:
            negated = ", ".join(sorted(set(report.negated_terms)))
            detail = f"{detail + ' ' if detail else ''}negated terms allowed: {negated}"

        suite.cases.append(
            Case(
                id=spec["id"],
                title=spec["title"],
                passed=passed,
                category=spec["category"],
                expected=(
                    "accepted" if expect_ok else f"rejected with {prefix or 'a violation'}"
                ),
                observed=observed,
                detail=detail,
            )
        )

    return counts


def _validate_published(suite: Suite, world: World) -> dict[str, int]:
    from sqlalchemy import select

    from app.agent.guards import build_allowed_numbers, validate_explanation
    from app.agent.tools import AgentToolkit
    from app.domain import METRIC_LABELS
    from app.domain import Metric as MetricEnum
    from app.models import AgentExplanation, AnomalyEvent, City, DailyRanking, PipelineRun

    counts = {"total": 0, "passed": 0, "llm": 0, "template": 0}

    with world.session() as session:
        run = session.scalars(
            select(PipelineRun)
            .where(PipelineRun.published.is_(True))
            .order_by(PipelineRun.analysis_date.desc())
            .limit(1)
        ).first()
        if run is None:
            suite.notes.append(
                "no published run in the scratch world; published explanations not checked"
            )
            return counts

        rows = session.execute(
            select(DailyRanking.rank, AnomalyEvent, City, AgentExplanation)
            .join(AnomalyEvent, AnomalyEvent.id == DailyRanking.event_id)
            .join(City, City.id == AnomalyEvent.city_id)
            .join(
                AgentExplanation,
                (AgentExplanation.event_id == AnomalyEvent.id)
                & (AgentExplanation.run_id == run.id),
            )
            .where(DailyRanking.run_id == run.id)
            .order_by(DailyRanking.rank)
        ).all()

        for rank, event, city, explanation in rows:
            counts["total"] += 1
            counts["llm" if explanation.generator == "llm" else "template"] += 1

            toolkit = AgentToolkit(session, world.settings)
            toolkit.register_context(
                {
                    "event_id": event.id,
                    "rank": rank,
                    "city_id": city.id,
                    "city": city.name,
                    "admin": city.admin,
                    "country": city.country,
                    "local_date": event.local_date.isoformat(),
                    "metric": event.metric,
                    "metric_label": METRIC_LABELS[MetricEnum(event.metric)],
                    "observed_value": round(event.observed_value, 2),
                    "unit": event.unit,
                }
            )
            arguments = {
                "get_anomaly_evidence": {"event_id": event.id},
                "get_city_weather": {
                    "city_id": city.id,
                    "date": event.local_date.isoformat(),
                },
                "get_city_baseline": {
                    "city_id": city.id,
                    "metric": event.metric,
                    "date": event.local_date.isoformat(),
                },
                "get_historical_extremes": {"city_id": city.id, "metric": event.metric},
                "get_daily_rankings": {"date": event.local_date.isoformat()},
            }
            for name in PRIMING_CALLS:
                toolkit.call(name, arguments[name])

            text = "\n".join(
                [
                    explanation.headline,
                    explanation.statistical_explanation,
                    explanation.historical_context,
                    explanation.caveats,
                ]
            )
            report = validate_explanation(
                text=text,
                evidence_values=[row.get("value") for row in (explanation.evidence or [])],
                allowed_numbers=build_allowed_numbers(
                    toolkit.observed_numbers, toolkit.observed_text
                ),
                required_numbers=[event.observed_value],
                required_terms=[city.name],
            )
            counts["passed"] += int(report.ok)

            suite.cases.append(
                Case(
                    id=f"published:{event.id}",
                    title=f"#{rank} {city.name} — {event.metric} ({explanation.generator})",
                    passed=report.ok,
                    category="published_explanation",
                    expected="every number citable from the tools",
                    observed=(
                        f"{report.numbers_grounded}/{report.numbers_found} numbers grounded"
                        if report.ok
                        else report.summary
                    ),
                )
            )

    return counts


def run(world: World, *, llm_mode: str) -> Suite:
    suite = Suite(id=SUITE_ID, title=TITLE, description=DESCRIPTION)
    with Timer() as timer:
        try:
            labelled = _run_labelled_cases(suite)
            published = _validate_published(suite, world)
        except Exception:
            suite.status = STATUS_ERROR
            suite.error = traceback.format_exc(limit=6)
            suite.duration_ms = timer.elapsed_ms
            return suite
    suite.duration_ms = timer.elapsed_ms

    to_reject = labelled["true_reject"] + labelled["false_accept"]
    rejected = labelled["true_reject"] + labelled["false_reject"]

    suite.metrics = [
        Metric("Labelled cases", labelled["total"], "cases"),
        Metric(
            "Correct verdicts",
            labelled["correct"],
            "cases",
            "accept/reject decision matched the label",
        ),
        Metric("Verdict accuracy", ratio(labelled["correct"], labelled["total"]), "fraction"),
        Metric(
            "Rejection recall",
            ratio(labelled["true_reject"], to_reject),
            "fraction",
            "share of defective explanations that were rejected",
        ),
        Metric(
            "Rejection precision",
            ratio(labelled["true_reject"], rejected),
            "fraction",
            "share of rejections that were of genuinely defective explanations",
        ),
        Metric(
            "False acceptances",
            labelled["false_accept"],
            "cases",
            "defective prose that would have been published",
        ),
        Metric(
            "False rejections",
            labelled["false_reject"],
            "cases",
            "faithful prose that would have been discarded",
        ),
        Metric(
            "Correct violation reasons",
            ratio(labelled["right_reason"], labelled["reason_expected"]),
            "fraction",
            "rejections that cited the defect the case was built around",
        ),
        Metric("Published explanations checked", published["total"], "explanations"),
        Metric(
            "Published explanations passing guards",
            published["passed"],
            "explanations",
            "re-validated against the numbers the read-only tools return",
        ),
        Metric(
            "Model-written",
            published["llm"],
            "explanations",
            "the rest are deterministic templates",
        ),
        Metric("Explanation source", llm_mode),
    ]

    failed = [c for c in suite.cases if not c.passed]
    suite.status = STATUS_FAILED if failed else STATUS_PASSED
    if llm_mode == "deterministic_templates_only":
        suite.notes.append(
            "No LLM key configured: this run measured the guards and the "
            "deterministic templates. It makes no claim about any model's accuracy."
        )
    return suite
