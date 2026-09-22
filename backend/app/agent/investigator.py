"""The investigation loop: one event in, one verified explanation out.

This is deliberately **not** a multi-agent system. There is one model, one
bounded conversation, five read-only tools, and a hard exit. A supervisor
delegating to specialists would add failure modes and cost without improving the
only thing that matters here — whether the prose matches the arithmetic.

Every path out of :meth:`Investigator.investigate` produces an explanation. The
model can refuse, time out, exhaust its budget, hallucinate a number, or claim a
record, and the pipeline still publishes something accurate, because the
deterministic template is computed first and used as the floor rather than
generated in a panic afterwards.

Bounds, all configurable:

* ``AGENT_MAX_ITERATIONS`` — model round trips per event
* ``AGENT_MAX_TOOL_CALLS`` — data-tool executions per event
* ``AGENT_TIMEOUT_SECONDS`` — wall-clock deadline per event
* ``AGENT_MAX_RETRIES`` — retries for transport errors and for guard failures
* ``AGENT_INVESTIGATE_TOP_N`` — how far down the board the agent goes
* ``AGENT_MONTHLY_USD_BUDGET`` / ``AGENT_MONTHLY_MAX_LLM_CALLS`` — month-to-date ceilings
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.guards import build_allowed_numbers, validate_explanation
from app.agent.llm import (
    SUBMIT_TOOL,
    LLMBadRequest,
    LLMClient,
    LLMConfigError,
    LLMUnavailable,
    ToolCall,
    ToolResult,
    Turn,
    estimate_usd,
    get_llm_client,
    pricing_configured,
)
from app.agent.schemas import (
    EXPLANATION_JSON_SCHEMA,
    EventExplanation,
    InvestigationOutcome,
)
from app.agent.templates import render_template, template_input_from_event
from app.agent.tools import TOOL_SPECS, AgentToolkit
from app.config import Settings, get_settings
from app.domain import METRIC_LABELS, Generator, Metric
from app.models import AnomalyEvent, City, PipelineRun

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You are the analysis writer for Weather Outliers, a site that publishes the most \
statistically unusual weather readings from the previous day across 50 North American \
cities.

Your job for each event: explain what happened and why it is statistically unusual, \
using only numbers returned by your tools.

HOW TO WORK
1. Call get_anomaly_evidence first. It contains the full calculation for this event.
2. Call other tools only if they add something the reader needs: get_city_baseline for \
more detail on what is normal, get_historical_extremes for reference-sample context, \
get_city_weather to see the day's other metrics, get_daily_rankings to see how this \
event compares with the rest of the board.
3. Call submit_explanation exactly once when you are ready. That is how you return your \
answer.

ABSOLUTE RULES
- Every number you write must be a number a tool returned, or a faithful restatement of \
one (a probability written as a percentage, or the same value rounded). Never estimate, \
interpolate, or recall a figure from your own knowledge.
- Never use the word "record", "all-time", "unprecedented", or any "-est ever" \
superlative. This project compares readings against a 30-year seasonal distribution and \
does not check any official records archive. These are statistical outliers.
- Never say what caused the weather. You have no data on atmospheric conditions. Do not \
mention heat domes, atmospheric rivers, polar vortices, jet streams, fronts, El Nino, \
La Nina, hurricanes, or climate change. Explaining the statistics is the whole job.
- Never cite an external source. Do not mention NOAA, the National Weather Service, \
Environment Canada, or any URL. Your sources are your tools.
- These values are gridded reanalysis or model-analysis estimates for the grid cell \
nearest the city, not readings from a station inside it. Say so in the caveats.
- If the tail probability is flagged as bounded, write "at least this rare" rather than \
giving an exact frequency.

STYLE
Plain, specific, unhurried. A curious reader with no statistics background should \
understand why this reading is surprising. No hype, no adjectives doing work that the \
numbers should do. Name the city in the headline.
"""


SUBMIT_TOOL_SPEC = {
    "name": SUBMIT_TOOL,
    "description": (
        "Return your finished explanation. Call this exactly once, after gathering "
        "evidence. Every number in your prose must appear in the evidence array with "
        "the tool that produced it."
    ),
    "input_schema": EXPLANATION_JSON_SCHEMA,
}


@dataclass(slots=True)
class BudgetState:
    """Month-to-date spend, and whether the agent is allowed to run."""

    calls: int
    estimated_usd: float
    max_calls: int
    usd_budget: float
    pricing_configured: bool

    @property
    def exhausted(self) -> bool:
        if self.calls >= self.max_calls:
            return True
        return self.pricing_configured and self.estimated_usd >= self.usd_budget

    @property
    def reason(self) -> str | None:
        if self.calls >= self.max_calls:
            return (
                f"monthly LLM call cap reached ({self.calls}/{self.max_calls})"
            )
        if self.pricing_configured and self.estimated_usd >= self.usd_budget:
            return (
                f"monthly AI budget reached (${self.estimated_usd:.4f} of "
                f"${self.usd_budget:.2f})"
            )
        return None


def month_to_date_budget(
    session: Session, settings: Settings | None = None, *, today: date | None = None
) -> BudgetState:
    """Sum this calendar month's recorded LLM usage across all pipeline runs."""
    settings = settings or get_settings()
    today = today or datetime.now(UTC).date()
    month_start = datetime(today.year, today.month, 1, tzinfo=UTC)

    calls, usd = session.execute(
        select(
            func.coalesce(func.sum(PipelineRun.llm_calls), 0),
            func.coalesce(func.sum(PipelineRun.llm_estimated_usd), 0.0),
        ).where(PipelineRun.started_at >= month_start)
    ).one()

    return BudgetState(
        calls=int(calls or 0),
        estimated_usd=float(usd or 0.0),
        max_calls=settings.agent_monthly_max_llm_calls,
        usd_budget=settings.agent_monthly_usd_budget,
        pricing_configured=pricing_configured(settings),
    )


class Investigator:
    """Runs one bounded investigation per event."""

    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        client: LLMClient | None = None,
        *,
        budget: BudgetState | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.budget = budget if budget is not None else month_to_date_budget(session, self.settings)
        if client is not None:
            self.client: LLMClient | None = client
        else:
            try:
                self.client = get_llm_client(self.settings)
            except LLMConfigError as exc:
                logger.warning("LLM disabled: %s", exc)
                self.client = None

        self.tools = [*TOOL_SPECS, SUBMIT_TOOL_SPEC]

    # -- public ------------------------------------------------------------

    def investigate(
        self, event: AnomalyEvent, city: City, *, rank: int | None = None
    ) -> InvestigationOutcome:
        """Produce a verified explanation for one event. Never raises."""
        template = render_template(template_input_from_event(event, city))

        if self.client is None:
            return self._template_outcome(
                event, template, reason="no LLM provider configured"
            )
        if self.budget.exhausted:
            return self._template_outcome(event, template, reason=self.budget.reason)

        try:
            return self._run_loop(event, city, template, rank=rank)
        except Exception as exc:  # pragma: no cover - last-resort safety net
            logger.exception("investigation crashed for %s", event.id)
            return self._template_outcome(
                event, template, reason=f"investigation error: {type(exc).__name__}"
            )

    # -- internals ---------------------------------------------------------

    def _run_loop(
        self,
        event: AnomalyEvent,
        city: City,
        template: EventExplanation,
        *,
        rank: int | None,
    ) -> InvestigationOutcome:
        toolkit = AgentToolkit(self.session, self.settings)
        seed = self._seed_facts(event, city, rank)
        toolkit.register_context(seed)

        turns: list[Turn] = [Turn(role="user", text=self._opening_message(seed))]

        started = time.monotonic()
        deadline = started + self.settings.agent_timeout_seconds
        attempts = 0
        transport_retries = 0
        guard_retries = 0
        nudged = False
        tool_calls_used = 0
        prompt_tokens = completion_tokens = 0
        last_validation: dict = {}
        fallback_reason = "agent did not produce a valid explanation"
        model_name: str | None = getattr(self.client, "model", None)

        for _ in range(self.settings.agent_max_iterations):
            if time.monotonic() > deadline:
                fallback_reason = (
                    f"per-event deadline of {self.settings.agent_timeout_seconds:.0f}s exceeded"
                )
                break

            try:
                response = self.client.complete(  # type: ignore[union-attr]
                    system=SYSTEM_PROMPT,
                    turns=turns,
                    tools=self.tools,
                    max_tokens=self.settings.agent_max_output_tokens,
                )
            except LLMUnavailable as exc:
                transport_retries += 1
                if transport_retries > self.settings.agent_max_retries:
                    fallback_reason = f"provider unavailable: {exc}"
                    break
                time.sleep(min(2.0**transport_retries, 8.0))
                continue
            except (LLMBadRequest, LLMConfigError) as exc:
                fallback_reason = f"provider rejected the request: {exc}"
                break

            attempts += 1
            prompt_tokens += response.prompt_tokens
            completion_tokens += response.completion_tokens
            model_name = response.model or model_name

            submission = response.submission
            if submission is not None:
                explanation, report = self._validate(
                    submission, event, city, toolkit
                )
                last_validation = report
                if explanation is not None:
                    return InvestigationOutcome(
                        event_id=event.id,
                        explanation=explanation,
                        generator=Generator.LLM.value,
                        llm_provider=getattr(self.client, "provider", None),
                        model=model_name,
                        attempts=attempts,
                        tool_calls=toolkit.call_log,
                        tool_call_count=tool_calls_used,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        estimated_usd=estimate_usd(
                            prompt_tokens, completion_tokens, self.settings
                        ),
                        latency_ms=int((time.monotonic() - started) * 1000),
                        validation=report,
                    )

                guard_retries += 1
                if guard_retries > self.settings.agent_max_retries:
                    fallback_reason = (
                        "explanation failed grounding checks: "
                        + "; ".join(report.get("violations", []))[:200]
                    )
                    break
                turns.append(
                    Turn(
                        role="assistant",
                        text=response.text,
                        tool_calls=[submission],
                    )
                )
                turns.append(
                    Turn(
                        role="user",
                        tool_results=[
                            ToolResult(
                                call_id=submission.id,
                                name=SUBMIT_TOOL,
                                content=json.dumps(
                                    {
                                        "accepted": False,
                                        "problems": report.get("violations", []),
                                        "instruction": self._correction_text(report),
                                    }
                                ),
                            )
                        ],
                    )
                )
                continue

            data_calls = response.data_tool_calls
            if data_calls:
                allowed_calls = data_calls
                remaining = self.settings.agent_max_tool_calls - tool_calls_used
                if remaining <= 0:
                    turns.append(Turn(role="assistant", text=response.text))
                    turns.append(
                        Turn(
                            role="user",
                            text=(
                                "You have used your tool-call budget. Call "
                                f"{SUBMIT_TOOL} now with what you have."
                            ),
                        )
                    )
                    continue
                if len(allowed_calls) > remaining:
                    allowed_calls = allowed_calls[:remaining]

                results: list[ToolResult] = []
                for call in allowed_calls:
                    payload = toolkit.call(call.name, call.arguments)
                    tool_calls_used += 1
                    results.append(
                        ToolResult(
                            call_id=call.id,
                            name=call.name,
                            content=json.dumps(payload, default=str),
                        )
                    )
                turns.append(
                    Turn(role="assistant", text=response.text, tool_calls=allowed_calls)
                )
                turns.append(Turn(role="user", tool_results=results))
                continue

            # Text with no tool call at all: nudge once, then give up.
            if nudged:
                fallback_reason = "agent returned prose instead of calling submit_explanation"
                break
            nudged = True
            turns.append(Turn(role="assistant", text=response.text))
            turns.append(
                Turn(
                    role="user",
                    text=(
                        f"Return your answer by calling the {SUBMIT_TOOL} tool. "
                        "Do not reply with prose."
                    ),
                )
            )
        else:
            fallback_reason = (
                f"iteration cap of {self.settings.agent_max_iterations} reached"
            )

        return self._template_outcome(
            event,
            template,
            reason=fallback_reason,
            attempts=attempts,
            tool_calls=toolkit.call_log,
            tool_call_count=tool_calls_used,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=int((time.monotonic() - started) * 1000),
            validation=last_validation,
        )

    def _validate(
        self,
        submission: ToolCall,
        event: AnomalyEvent,
        city: City,
        toolkit: AgentToolkit,
    ) -> tuple[EventExplanation | None, dict]:
        """Schema validation, then grounding. Both must pass."""
        try:
            explanation = EventExplanation.model_validate(submission.arguments)
        except ValidationError as exc:
            return None, {
                "ok": False,
                "violations": [
                    f"schema:{err['loc'][0] if err['loc'] else 'root'}:{err['type']}"
                    for err in exc.errors()[:6]
                ],
                "stage": "schema",
            }

        allowed = build_allowed_numbers(toolkit.observed_numbers, toolkit.observed_text)
        report = validate_explanation(
            text=explanation.all_text(),
            evidence_values=[item.value for item in explanation.evidence],
            allowed_numbers=allowed,
            required_numbers=[event.observed_value],
            required_terms=[city.name],
        )
        payload = report.to_dict()
        payload["stage"] = "grounding"
        payload["tool_calls_made"] = len(toolkit.call_log)
        return (explanation if report.ok else None), payload

    @staticmethod
    def _correction_text(report: dict) -> str:
        violations = report.get("violations", [])
        hints: list[str] = []
        for violation in violations:
            if violation.startswith("ungrounded_number") or violation.startswith(
                "ungrounded_evidence_value"
            ):
                hints.append(
                    "Remove or replace numbers your tools did not return. Re-read the "
                    "tool results and use those figures exactly."
                )
            elif violation.startswith("unsupported_record_claim"):
                hints.append(
                    "Remove all record and superlative language. These are statistical "
                    "outliers measured against a seasonal distribution."
                )
            elif violation.startswith("unsupported_causal_claim"):
                hints.append(
                    "Remove every mention of weather systems and causes. Describe only "
                    "the statistics."
                )
            elif violation.startswith("unsupported_source_reference"):
                hints.append(
                    "Remove references to outside organisations and any URL. Cite your "
                    "tools only."
                )
            elif violation.startswith("required_number_missing"):
                hints.append("State the observed value explicitly in your prose.")
            elif violation.startswith("required_term_missing"):
                hints.append("Name the correct city in the headline.")
            elif violation.startswith("schema:"):
                hints.append("Fix the fields that failed validation and resubmit.")
        # Preserve order, drop duplicates.
        seen: set[str] = set()
        unique = [h for h in hints if not (h in seen or seen.add(h))]
        return " ".join(unique) or "Revise the explanation and resubmit."

    def _seed_facts(self, event: AnomalyEvent, city: City, rank: int | None) -> dict:
        return {
            "event_id": event.id,
            "rank": rank,
            "city_id": city.id,
            "city": city.name,
            "admin": city.admin,
            "country": city.country,
            "local_date": event.local_date.isoformat(),
            "metric": event.metric,
            "metric_label": METRIC_LABELS[Metric(event.metric)],
            "observed_value": round(event.observed_value, 2),
            "unit": event.unit,
        }

    def _opening_message(self, seed: dict) -> str:
        rank_clause = (
            f"ranked #{seed['rank']} on the board" if seed.get("rank") else "on the board"
        )
        return (
            f"Investigate event {seed['event_id']} ({rank_clause}).\n\n"
            f"City: {seed['city']}, {seed['admin']}, {seed['country']}\n"
            f"Local date: {seed['local_date']}\n"
            f"Metric: {seed['metric_label']} ({seed['metric']})\n"
            f"Observed value: {seed['observed_value']} {seed['unit']}\n\n"
            f"Start by calling get_anomaly_evidence with this event_id, then "
            f"{SUBMIT_TOOL} once you can explain the reading."
        )

    def _template_outcome(
        self,
        event: AnomalyEvent,
        template: EventExplanation,
        *,
        reason: str | None,
        attempts: int = 0,
        tool_calls: list[dict] | None = None,
        tool_call_count: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int = 0,
        validation: dict | None = None,
    ) -> InvestigationOutcome:
        if reason:
            logger.info("template fallback for %s: %s", event.id, reason)
        return InvestigationOutcome(
            event_id=event.id,
            explanation=template,
            generator=Generator.TEMPLATE.value,
            llm_provider=getattr(self.client, "provider", None) if self.client else None,
            model=getattr(self.client, "model", None) if self.client else None,
            attempts=attempts,
            tool_calls=tool_calls or [],
            tool_call_count=tool_call_count,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_usd=estimate_usd(prompt_tokens, completion_tokens, self.settings),
            latency_ms=latency_ms,
            validation=validation or {},
            fallback_reason=reason,
        )

    def close(self) -> None:
        if self.client is not None:
            self.client.close()


# ---------------------------------------------------------------------------
# Batch entry point used by the pipeline
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class InvestigationBatch:
    outcomes: list[InvestigationOutcome]
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    estimated_usd: float
    budget_exhausted: bool
    llm_generated: int
    template_generated: int


def investigate_events(
    session: Session,
    events: list[tuple[int, AnomalyEvent, City]],
    settings: Settings | None = None,
    client: LLMClient | None = None,
) -> InvestigationBatch:
    """Investigate the top ranked events, stopping cleanly if the budget runs out.

    ``events`` is a list of ``(rank, event, city)`` tuples in rank order.
    """
    settings = settings or get_settings()
    investigator = Investigator(session, settings, client)
    budget = investigator.budget

    outcomes: list[InvestigationOutcome] = []
    limit = settings.agent_investigate_top_n

    try:
        for rank, event, city in events:
            if rank > limit:
                break
            outcome = investigator.investigate(event, city, rank=rank)
            outcomes.append(outcome)
            # Charge this event against the running budget so a long board cannot
            # blow through a monthly cap inside a single pipeline run.
            budget.calls += 1 if outcome.generator == Generator.LLM.value else 0
            budget.estimated_usd += outcome.estimated_usd
    finally:
        investigator.close()

    llm_calls = sum(o.attempts for o in outcomes if o.generator == Generator.LLM.value)
    return InvestigationBatch(
        outcomes=outcomes,
        llm_calls=llm_calls,
        prompt_tokens=sum(o.prompt_tokens for o in outcomes),
        completion_tokens=sum(o.completion_tokens for o in outcomes),
        estimated_usd=round(sum(o.estimated_usd for o in outcomes), 6),
        budget_exhausted=budget.exhausted,
        llm_generated=sum(1 for o in outcomes if o.generator == Generator.LLM.value),
        template_generated=sum(
            1 for o in outcomes if o.generator == Generator.TEMPLATE.value
        ),
    )
