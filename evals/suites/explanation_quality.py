"""Optional model-based evaluator — explanation quality.

Off by default. It costs money, it needs a key, and it is the weakest evidence in
this harness. It exists because the deterministic evaluators cannot see everything
that matters about a paragraph of prose — whether the hedging reads as hedging to
a human, whether the sentence order helps or confuses — and because saying "we did
not measure that" is better than pretending the deterministic checks covered it.

What it measures
----------------
A single judging model scores each published explanation against a versioned
rubric on four ordinal dimensions (1-5) and answers three boolean policy
questions. The rubric text and its SHA-256 are recorded in the report, so a score
can be traced to the exact wording that produced it.

What passes and what does not
-----------------------------
The quality scores are **reported, never asserted**. There is no threshold: a mean
of 3.8 is a measurement, and turning it into a pass/fail gate would invite tuning
the rubric until the gate opened. The only deterministic assertions are structural
— that every item came back as well-formed JSON with every rubric field present
and in range. Policy flags the judge raises are surfaced as failing cases, clearly
labelled as model-flagged and requiring human confirmation, because a model's
opinion that a rule was broken is a lead, not a finding.

Reproducible settings
---------------------
Temperature 0, one fixed model id, a fixed max-token budget, items judged in
published-rank order, one item per request with no shared conversation state, and
the rubric pinned by hash. Raw verdicts are written to a sidecar file so a reader
can audit the judge rather than trust this summary of it.

Limitations, stated because the numbers are worthless without them
-----------------------------------------------------------------
* **One judge, no inter-rater agreement.** No second model, no human panel, so
  there is no way to distinguish a bad explanation from an idiosyncratic judge.
* **Shared-family bias.** The judge may be the same model family that could
  generate these explanations in production, and models tend to prefer their own
  distribution. The scores are not neutral.
* **Temperature 0 is not determinism.** Provider-side batching, model revisions
  behind a stable alias, and floating-point non-associativity all move the output.
  Re-running may not reproduce a score exactly.
* **Small n.** Ten explanations per run. Differences of a few tenths of a point
  mean nothing at that size, and no confidence interval is reported because one
  computed from n=10 ordinal judgements would be more misleading than none.
* **It cannot check arithmetic.** A wrong-but-plausible number reads perfectly to
  a judge. That is Evaluator 3's job
  (:mod:`evals.suites.explanation_agreement`), which compares each claim against
  the stored calculation, and nothing here substitutes for it.
* **Ordinal, not interval.** The mean of a 1-5 rubric is a convenience, not a
  measurement on a real scale.
"""

from __future__ import annotations

import hashlib
import json
import os
import traceback
from pathlib import Path

import httpx

from evals.harness import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    Case,
    Metric,
    Suite,
    Timer,
    ratio,
)

SUITE_ID = "explanation_quality"
TITLE = "Explanation quality (model-based, optional)"
DESCRIPTION = (
    "A single judging model scores each published explanation against a versioned "
    "rubric. Scores are reported, not asserted; only the structural validity of the "
    "judge's output is a pass/fail condition."
)

#: Bump when the rubric wording changes. Scores from different rubric versions are
#: not comparable, and the version travels with every recorded score.
RUBRIC_VERSION = "quality-1.0.0"

RUBRIC = """You are grading short explanations of unusual weather readings. Each
explanation was produced from a fixed statistical calculation and is published on a
public website alongside the numbers it describes.

Score each dimension from 1 to 5, where 1 is poor and 5 is excellent:

- clarity: would a general reader without statistical training understand what
  happened and how unusual it was?
- calibration: does the language match the strength of the evidence — neither
  overstating a modest anomaly nor underselling an extreme one?
- hedging: where the text describes a bound, a model estimate, or an approximate
  return period, is that uncertainty expressed plainly rather than buried or
  omitted?
- usefulness: does the explanation tell the reader something beyond restating the
  number?

Then answer three yes/no questions about policy. Answer "true" only if the text
actually does the thing; a disclaimer saying it does NOT do the thing is not a
violation:

- claims_a_record: does the text assert a city, state, national or all-time
  record?
- claims_a_cause: does the text assert which atmospheric conditions or which
  climate mechanism produced the reading?
- presents_model_data_as_observation: does the text describe the value as a direct
  instrument or station reading?

Judge only what the text says. You have not been given the underlying data and
must not guess whether the numbers are correct."""

#: One tool call per item: a schema is the cheapest way to make the verdict
#: machine-checkable and to make a malformed answer a visible failure rather than a
#: parsing heuristic.
VERDICT_TOOL = {
    "name": "submit_verdict",
    "description": "Record the rubric scores and policy answers for one explanation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "clarity": {"type": "integer", "minimum": 1, "maximum": 5},
            "calibration": {"type": "integer", "minimum": 1, "maximum": 5},
            "hedging": {"type": "integer", "minimum": 1, "maximum": 5},
            "usefulness": {"type": "integer", "minimum": 1, "maximum": 5},
            "claims_a_record": {"type": "boolean"},
            "claims_a_cause": {"type": "boolean"},
            "presents_model_data_as_observation": {"type": "boolean"},
            "comment": {"type": "string"},
        },
        "required": [
            "clarity",
            "calibration",
            "hedging",
            "usefulness",
            "claims_a_record",
            "claims_a_cause",
            "presents_model_data_as_observation",
        ],
        "additionalProperties": False,
    },
}

SCORE_FIELDS = ("clarity", "calibration", "hedging", "usefulness")
POLICY_FIELDS = ("claims_a_record", "claims_a_cause", "presents_model_data_as_observation")

#: Pinned so a rerun sends byte-identical requests.
TEMPERATURE = 0.0
MAX_TOKENS = 512
REQUEST_TIMEOUT_S = 60.0


def rubric_sha() -> str:
    return hashlib.sha256(RUBRIC.encode("utf-8")).hexdigest()[:16]


class JudgeUnavailable(Exception):
    """Raised when the judge is not configured. Not an error — a skip."""


class _Judge:
    """A deliberately small client, separate from the application's.

    The application's client is built for a tool-using agent loop and does not set
    a temperature, so it inherits the provider default. A judge that cannot pin
    its own sampling settings cannot claim reproducible ones, and changing the
    application's client to suit an optional evaluator would be the wrong trade —
    so this is a few dozen lines of its own, used only here.
    """

    def __init__(self) -> None:
        provider = os.environ.get("EVAL_JUDGE_PROVIDER", "anthropic").strip().lower()
        if provider == "anthropic":
            self.provider = "anthropic"
            self.key = os.environ.get("ANTHROPIC_API_KEY", "")
            self.base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1")
            self.model = os.environ.get("EVAL_JUDGE_MODEL", "claude-sonnet-5")
            self.api_version = os.environ.get("ANTHROPIC_API_VERSION", "2023-06-01")
        elif provider == "openai":
            self.provider = "openai"
            self.key = os.environ.get("OPENAI_API_KEY", "")
            self.base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            self.model = os.environ.get("EVAL_JUDGE_MODEL", "")
        else:
            raise JudgeUnavailable(f"unknown EVAL_JUDGE_PROVIDER {provider!r}")

        if not self.key:
            raise JudgeUnavailable(
                f"no API key for judge provider {self.provider!r}; "
                "run with --live-llm or --judge and the key in the environment"
            )
        if not self.model:
            raise JudgeUnavailable("EVAL_JUDGE_MODEL is not set")

        self._client = httpx.Client(timeout=REQUEST_TIMEOUT_S)

    def close(self) -> None:
        self._client.close()

    def judge(self, text: str) -> tuple[dict, int, int]:
        """One item, one request. Returns (verdict, prompt_tokens, completion_tokens)."""
        if self.provider == "anthropic":
            payload = {
                "model": self.model,
                "max_tokens": MAX_TOKENS,
                "temperature": TEMPERATURE,
                "system": RUBRIC,
                "messages": [{"role": "user", "content": text}],
                "tools": [VERDICT_TOOL],
                "tool_choice": {"type": "tool", "name": VERDICT_TOOL["name"]},
            }
            headers = {
                "x-api-key": self.key,
                "anthropic-version": self.api_version,
                "content-type": "application/json",
            }
            data = self._request(f"{self.base}/messages", payload, headers)
            verdict = {}
            for block in data.get("content") or []:
                if block.get("type") == "tool_use":
                    verdict = block.get("input") or {}
            usage = data.get("usage") or {}
            return (
                verdict,
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
            )

        payload = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "temperature": TEMPERATURE,
            "messages": [
                {"role": "system", "content": RUBRIC},
                {"role": "user", "content": text},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": VERDICT_TOOL["name"],
                        "description": VERDICT_TOOL["description"],
                        "parameters": VERDICT_TOOL["input_schema"],
                    },
                }
            ],
            "tool_choice": {
                "type": "function",
                "function": {"name": VERDICT_TOOL["name"]},
            },
        }
        headers = {"authorization": f"Bearer {self.key}", "content-type": "application/json"}
        data = self._request(f"{self.base}/chat/completions", payload, headers)
        choices = data.get("choices") or []
        message = (choices[0].get("message") if choices else None) or {}
        verdict = {}
        for call in message.get("tool_calls") or []:
            raw = (call.get("function") or {}).get("arguments") or "{}"
            try:
                verdict = json.loads(raw)
            except json.JSONDecodeError:
                verdict = {}
        usage = data.get("usage") or {}
        return (
            verdict,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )

    def _request(self, url: str, payload: dict, headers: dict) -> dict:
        response = self._client.post(url, json=payload, headers=headers)
        if response.status_code >= 400:
            # The body can echo request content; the status and reason are enough to
            # diagnose a judge failure and cannot contain the key.
            raise RuntimeError(f"judge request failed: HTTP {response.status_code}")
        return response.json()


def _valid(verdict: dict) -> list[str]:
    """Everything structurally wrong with a verdict, as human-readable strings."""
    problems = []
    for field in SCORE_FIELDS:
        value = verdict.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            problems.append(f"{field} is {value!r}, not an integer")
        elif not 1 <= value <= 5:
            problems.append(f"{field}={value} outside 1-5")
    for field in POLICY_FIELDS:
        if not isinstance(verdict.get(field), bool):
            problems.append(f"{field} is {verdict.get(field)!r}, not a boolean")
    return problems


def run(world, *, verdict_path: Path | None = None) -> Suite:
    suite = Suite(id=SUITE_ID, title=TITLE, description=DESCRIPTION)
    timer = Timer()

    try:
        judge = _Judge()
    except JudgeUnavailable as exc:
        suite.status = STATUS_SKIPPED
        suite.notes.append(f"not run: {exc}")
        suite.notes.append(
            "The deterministic evaluators do not depend on this one. Its absence "
            "means explanation quality was not measured, not that it passed."
        )
        suite.metrics = [
            Metric("Rubric version", RUBRIC_VERSION),
            Metric("Rubric SHA-256 (truncated)", rubric_sha()),
        ]
        return suite

    try:
        with timer:
            from sqlalchemy import select

            from app.models import (
                AgentExplanation,
                AnomalyEvent,
                City,
                DailyRanking,
                PipelineRun,
            )

            verdicts: list[dict] = []
            prompt_tokens = 0
            completion_tokens = 0

            with world.session() as session:
                run_row = session.scalars(
                    select(PipelineRun)
                    .where(PipelineRun.published.is_(True))
                    .order_by(PipelineRun.analysis_date.desc())
                    .limit(1)
                ).first()
                if run_row is None:
                    suite.status = STATUS_SKIPPED
                    suite.duration_ms = timer.elapsed_ms
                    suite.notes.append("no published run in the scratch world; nothing to judge")
                    return suite

                rows = session.execute(
                    select(DailyRanking.rank, AnomalyEvent, City, AgentExplanation)
                    .join(AnomalyEvent, AnomalyEvent.id == DailyRanking.event_id)
                    .join(City, City.id == AnomalyEvent.city_id)
                    .join(
                        AgentExplanation,
                        (AgentExplanation.event_id == AnomalyEvent.id)
                        & (AgentExplanation.run_id == run_row.id),
                    )
                    .where(DailyRanking.run_id == run_row.id)
                    .order_by(DailyRanking.rank)  # fixed order, so reruns are comparable
                ).all()

                items = [
                    (
                        rank,
                        event.id,
                        f"#{rank} {city.name} — {event.metric} ({explanation.generator})",
                        "\n\n".join(
                            [
                                explanation.headline,
                                explanation.statistical_explanation,
                                explanation.historical_context,
                                explanation.caveats,
                            ]
                        ),
                    )
                    for rank, event, city, explanation in rows
                ]

            for rank, event_id, label, text in items:
                verdict, p_tokens, c_tokens = judge.judge(text)
                prompt_tokens += p_tokens
                completion_tokens += c_tokens
                problems = _valid(verdict)
                verdicts.append(
                    {
                        "rank": rank,
                        "event_id": event_id,
                        "verdict": verdict,
                        "structural_problems": problems,
                    }
                )
                suite.cases.append(
                    Case(
                        id=f"verdict_well_formed:{event_id}",
                        title=f"{label} — judge returned a usable verdict",
                        passed=not problems,
                        category="judge_protocol",
                        expected="all four rubric scores in 1-5 and all three policy booleans",
                        observed="; ".join(problems) if problems else "complete and in range",
                    )
                )

            # Policy flags are leads, not findings — but a lead the report must show.
            for field in POLICY_FIELDS:
                flagged = [v["rank"] for v in verdicts if v["verdict"].get(field) is True]
                suite.cases.append(
                    Case(
                        id=f"policy_flag:{field}",
                        title=f"Judge flagged no explanation for {field.replace('_', ' ')}",
                        passed=not flagged,
                        category="model_flagged",
                        expected="0 flagged",
                        observed=(
                            f"ranks {flagged} flagged — model-flagged, needs human "
                            "confirmation before being treated as a defect"
                            if flagged
                            else "0 flagged"
                        ),
                        detail=(
                            "A judging model's opinion that a rule was broken is a lead. "
                            "The deterministic check for this rule lives in the "
                            "explanation-agreement suite."
                        ),
                    )
                )

            usable = [v["verdict"] for v in verdicts if not v["structural_problems"]]
            suite.metrics = [
                Metric("Rubric version", RUBRIC_VERSION),
                Metric("Rubric SHA-256 (truncated)", rubric_sha()),
                Metric("Judge provider", judge.provider),
                Metric("Judge model", judge.model),
                Metric("Temperature", TEMPERATURE, detail="pinned; not a guarantee of determinism"),
                Metric("Max tokens per verdict", MAX_TOKENS),
                Metric("Explanations judged", len(verdicts)),
                Metric("Usable verdicts", ratio(len(usable), len(verdicts)), unit="fraction"),
            ]
            for field in SCORE_FIELDS:
                values = [v[field] for v in usable if isinstance(v.get(field), int)]
                suite.metrics.append(
                    Metric(
                        f"Mean {field} (1-5)",
                        round(sum(values) / len(values), 2) if values else None,
                        detail=(
                            f"n={len(values)}; ordinal scale, reported not asserted"
                            if values
                            else "no usable verdicts"
                        ),
                    )
                )
            suite.metrics.append(
                Metric("Judge prompt tokens", prompt_tokens),
            )
            suite.metrics.append(
                Metric("Judge completion tokens", completion_tokens),
            )

            from app.agent.llm import estimate_usd, pricing_configured

            suite.metrics.append(
                Metric(
                    "Estimated judge cost",
                    estimate_usd(prompt_tokens, completion_tokens),
                    unit="USD",
                    detail=(
                        "from configured per-Mtok prices"
                        if pricing_configured()
                        else "no prices configured, so this reads 0.0 rather than free"
                    ),
                )
            )

            if verdict_path is not None:
                verdict_path.parent.mkdir(parents=True, exist_ok=True)
                verdict_path.write_text(
                    json.dumps(
                        {
                            "rubric_version": RUBRIC_VERSION,
                            "rubric_sha256": rubric_sha(),
                            "rubric": RUBRIC,
                            "provider": judge.provider,
                            "model": judge.model,
                            "temperature": TEMPERATURE,
                            "max_tokens": MAX_TOKENS,
                            "verdicts": verdicts,
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                suite.notes.append(f"raw verdicts written to {verdict_path}")

            suite.notes.append(
                "Quality scores are reported, never asserted. No threshold gates this "
                "suite; only malformed judge output fails it."
            )
            suite.notes.append(
                "Single judge, no inter-rater agreement, possible shared-family bias, "
                "n=10. Temperature 0 pins the request, not the provider. See the module "
                "docstring for the full list."
            )

        failed = [c for c in suite.cases if not c.passed]
        suite.status = STATUS_FAILED if failed else STATUS_PASSED
        suite.duration_ms = timer.elapsed_ms

    except Exception:
        suite.status = STATUS_ERROR
        suite.error = traceback.format_exc(limit=6)
        suite.duration_ms = timer.elapsed_ms
    finally:
        judge.close()

    return suite
