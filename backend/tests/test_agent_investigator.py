"""Agent behaviour tests.

These are the tests that matter most for publishing. The agent writes prose that
goes on a public website under numbers a reader might act on, so what is verified
here is not "did the model respond" but:

* a grounded answer is accepted and attributed to the LLM
* a fabricated number is rejected, corrected, and accepted on the retry
* a persistent record claim never reaches the page
* every provider failure mode degrades to the deterministic template
* the bounds (iterations, tool calls, deadline, budget) actually bind

No network is touched. The model is a scripted stand-in implementing the same
:class:`~app.agent.llm.LLMClient` protocol the real adapters implement.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from app.agent.investigator import (
    SUBMIT_TOOL_SPEC,
    BudgetState,
    Investigator,
    investigate_events,
    month_to_date_budget,
)
from app.agent.llm import LLMBadRequest, LLMResponse, LLMUnavailable, ToolCall
from app.config import get_settings
from app.domain import Generator, Metric
from tests.conftest import make_city, make_event, make_run

# ---------------------------------------------------------------------------
# Scripted model
# ---------------------------------------------------------------------------


class ScriptedClient:
    """A stand-in model driven by a list of behaviours, one per turn.

    Each behaviour is either a callable ``(turns) -> LLMResponse``, or an
    exception instance to raise. Anything left over after the script runs out
    repeats the final behaviour, which is what makes "always fails" cases easy
    to express.
    """

    provider = "scripted"
    model = "scripted-1"

    def __init__(self, behaviours: list) -> None:
        self.behaviours = behaviours
        self.calls = 0
        self.closed = False

    def complete(self, *, system, turns, tools, max_tokens):
        self.calls += 1
        behaviour = self.behaviours[min(self.calls - 1, len(self.behaviours) - 1)]
        if isinstance(behaviour, Exception):
            raise behaviour
        return behaviour(turns)

    def close(self) -> None:
        self.closed = True


def _response(
    *,
    text: str | None = None,
    tool_calls: list[ToolCall] | None = None,
    prompt_tokens: int = 900,
    completion_tokens: int = 320,
) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=tool_calls or [],
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        stop_reason="tool_use" if tool_calls else "end_turn",
        model="scripted-1",
    )


def fetch_evidence(event_id: str, call_id: str = "call-1"):
    def behaviour(_turns):
        return _response(
            text="Let me pull the calculation.",
            tool_calls=[
                ToolCall(
                    id=call_id,
                    name="get_anomaly_evidence",
                    arguments={"event_id": event_id},
                )
            ],
        )

    return behaviour


def submit(payload: dict, call_id: str = "sub-1"):
    def behaviour(_turns):
        return _response(
            tool_calls=[ToolCall(id=call_id, name="submit_explanation", arguments=payload)]
        )

    return behaviour


def grounded_payload(event, city) -> dict:
    """A submission whose every number is one the tools returned."""
    return {
        "headline": (
            f"{city.name} reached {event.observed_value:.1f} °C, "
            f"{abs(event.deviation):.1f} °C above its seasonal average."
        ),
        "statistical_explanation": (
            f"The 1991-2020 reference sample for this seasonal window holds "
            f"{event.baseline_n} daily highs for {city.name}, with a median of "
            f"{event.baseline_median:.1f} °C. The reading of {event.observed_value:.1f} °C "
            f"sits at the {event.percentile:.1f} percentile of that distribution, an "
            f"empirical tail probability of {event.tail_probability:.5f}."
        ),
        "historical_context": (
            f"The highest value in the same window across the reference period is "
            f"{event.baseline_max:.1f} °C, so this reading sits far out in the upper tail "
            f"of what those {event.baseline_n} days contain."
        ),
        "caveats": (
            "The value is a reanalysis estimate for the grid cell nearest the city, not a "
            "station reading. It is a statistical comparison against a seasonal "
            "distribution and is not an official record of any kind."
        ),
        "evidence": [
            {
                "label": "Observed daily high",
                "value": round(event.observed_value, 2),
                "unit": "°C",
                "source_tool": "get_anomaly_evidence",
            },
            {
                "label": "Seasonal median",
                "value": round(event.baseline_median, 2),
                "unit": "°C",
                "source_tool": "get_anomaly_evidence",
            },
        ],
        "confidence": "high",
    }


def seed_only_payload(event, city) -> dict:
    """A submission citing nothing but the facts stated in the opening message.

    Useful for the cases that must succeed without any tool call — it proves the
    prompt's own figures are legitimately citable, and nothing else is.
    """
    return {
        "headline": (
            f"{city.name} reached {event.observed_value:.1f} °C, an unusual reading for "
            f"the date."
        ),
        "statistical_explanation": (
            f"{city.name} reached {event.observed_value:.1f} °C on "
            f"{event.local_date.isoformat()}. That is the daily high under investigation "
            f"and the figure the ranking is built on."
        ),
        "historical_context": (
            f"This entry covers the daily high metric for {city.name} on that local date."
        ),
        "caveats": (
            "The value is a model estimate for the grid cell nearest the city, not a "
            "station reading, and it is not an official record."
        ),
        "evidence": [
            {
                "label": "Observed daily high",
                "value": round(event.observed_value, 2),
                "unit": "°C",
                "source_tool": "prompt",
            }
        ],
        "confidence": "medium",
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def event_and_city(session):
    city = make_city(session)
    run = make_run(session)
    event = make_event(session, city, run=run, rank=1, observed_value=41.6)
    return event, city, run


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_grounded_submission_is_accepted(session, event_and_city):
    event, city, _ = event_and_city
    client = ScriptedClient(
        [fetch_evidence(event.id), submit(grounded_payload(event, city))]
    )
    inv = Investigator(session, client=client)

    outcome = inv.investigate(event, city, rank=1)

    assert outcome.generator == Generator.LLM.value
    assert outcome.fallback_reason is None
    assert outcome.attempts == 2
    assert outcome.tool_call_count == 1
    assert outcome.validation["ok"] is True
    assert outcome.validation["stage"] == "grounding"
    assert outcome.prompt_tokens == 1800
    assert city.name in outcome.explanation.headline
    # Tokens are counted, but dollars stay at zero until the operator supplies prices.
    assert outcome.estimated_usd == 0.0


def test_tool_calls_are_logged_with_their_arguments(session, event_and_city):
    event, city, _ = event_and_city
    client = ScriptedClient(
        [fetch_evidence(event.id), submit(grounded_payload(event, city))]
    )
    outcome = Investigator(session, client=client).investigate(event, city, rank=1)

    assert [c["tool"] for c in outcome.tool_calls] == ["get_anomaly_evidence"]
    assert outcome.tool_calls[0]["arguments"] == {"event_id": event.id}
    assert outcome.tool_calls[0]["ok"] is True


def test_submit_tool_is_not_charged_against_the_tool_budget(session, event_and_city):
    """``submit_explanation`` is an output channel, not a data source.

    The tool budget is set to exactly one call, spent on fetching evidence. If
    submitting counted against that budget the answer could never be returned.
    """
    event, city, _ = event_and_city
    client = ScriptedClient(
        [fetch_evidence(event.id), submit(grounded_payload(event, city))]
    )
    inv = Investigator(session, client=client)
    inv.settings = inv.settings.model_copy(update={"agent_max_tool_calls": 1})

    outcome = inv.investigate(event, city, rank=1)

    assert outcome.generator == Generator.LLM.value
    assert outcome.tool_call_count == 1


# ---------------------------------------------------------------------------
# Grounding failures
# ---------------------------------------------------------------------------


def test_fabricated_number_is_corrected_then_accepted(session, event_and_city):
    event, city, _ = event_and_city
    bad = grounded_payload(event, city)
    bad["historical_context"] = (
        "The previous highest reading for this date was 39.2 °C, set in 1994."
    )

    client = ScriptedClient(
        [
            fetch_evidence(event.id),
            submit(bad, call_id="sub-bad"),
            submit(grounded_payload(event, city), call_id="sub-good"),
        ]
    )
    outcome = Investigator(session, client=client).investigate(event, city, rank=1)

    assert outcome.generator == Generator.LLM.value
    assert outcome.attempts == 3
    # The rejection was fed back as a tool result, not silently retried.
    assert "1994" not in outcome.explanation.historical_context


def test_persistent_record_claim_falls_back_to_the_template(session, event_and_city):
    event, city, _ = event_and_city
    bad = grounded_payload(event, city)
    bad["headline"] = f"{city.name} sets an all-time record at {event.observed_value:.1f} °C."

    client = ScriptedClient([fetch_evidence(event.id), submit(bad)])
    outcome = Investigator(session, client=client).investigate(event, city, rank=1)

    assert outcome.generator == Generator.TEMPLATE.value
    assert "grounding checks" in (outcome.fallback_reason or "")
    assert "record" not in outcome.explanation.headline.lower()
    assert "all-time" not in outcome.explanation.all_text().lower()
    # The failed attempt is still recorded, so the failure is inspectable.
    assert any("record" in v for v in outcome.validation["violations"])


def test_causal_attribution_is_rejected(session, event_and_city):
    event, city, _ = event_and_city
    bad = grounded_payload(event, city)
    bad["statistical_explanation"] = (
        f"{city.name} hit {event.observed_value:.1f} °C, driven by a heat dome parked over "
        f"the Southwest, well above the {event.baseline_median:.1f} °C median."
    )

    client = ScriptedClient([fetch_evidence(event.id), submit(bad)])
    outcome = Investigator(session, client=client).investigate(event, city, rank=1)

    assert outcome.generator == Generator.TEMPLATE.value
    violations = " ".join(outcome.validation["violations"])
    assert "unsupported_causal_claim" in violations


def test_invented_source_reference_is_rejected(session, event_and_city):
    event, city, _ = event_and_city
    bad = grounded_payload(event, city)
    bad["caveats"] = (
        "Figures are drawn from NOAA's climate archive and may be revised. This is not a "
        "station reading."
    )

    client = ScriptedClient([fetch_evidence(event.id), submit(bad)])
    outcome = Investigator(session, client=client).investigate(event, city, rank=1)

    assert outcome.generator == Generator.TEMPLATE.value
    assert "unsupported_source_reference:noaa" in outcome.validation["violations"]


def test_wrong_city_is_rejected(session, event_and_city):
    event, city, _ = event_and_city
    bad = grounded_payload(event, city)
    bad["headline"] = f"Tucson reached {event.observed_value:.1f} °C on an unusual day."
    bad["statistical_explanation"] = bad["statistical_explanation"].replace(city.name, "Tucson")
    bad["historical_context"] = bad["historical_context"].replace(city.name, "Tucson")

    client = ScriptedClient([fetch_evidence(event.id), submit(bad)])
    outcome = Investigator(session, client=client).investigate(event, city, rank=1)

    assert outcome.generator == Generator.TEMPLATE.value
    assert f"required_term_missing:{city.name}" in outcome.validation["violations"]


def test_schema_violation_is_reported_as_a_schema_stage_failure(session, event_and_city):
    event, city, _ = event_and_city
    client = ScriptedClient([submit({"headline": "too short"})])
    outcome = Investigator(session, client=client).investigate(event, city, rank=1)

    assert outcome.generator == Generator.TEMPLATE.value
    assert outcome.validation["stage"] == "schema"
    assert outcome.validation["ok"] is False


# ---------------------------------------------------------------------------
# Provider failure modes
# ---------------------------------------------------------------------------


def test_no_client_uses_the_template(session, event_and_city):
    event, city, _ = event_and_city
    outcome = Investigator(session, client=None).investigate(event, city, rank=1)

    assert outcome.generator == Generator.TEMPLATE.value
    assert outcome.fallback_reason == "no LLM provider configured"
    assert outcome.prompt_tokens == 0
    assert outcome.explanation.evidence


def test_provider_unavailable_retries_then_falls_back(
    session, event_and_city, monkeypatch
):
    monkeypatch.setattr("app.agent.investigator.time.sleep", lambda _s: None)
    event, city, _ = event_and_city
    client = ScriptedClient([LLMUnavailable("503 upstream")])

    outcome = Investigator(session, client=client).investigate(event, city, rank=1)

    assert outcome.generator == Generator.TEMPLATE.value
    assert "provider unavailable" in (outcome.fallback_reason or "")
    # max_retries=2 means the initial attempt plus two retries.
    assert client.calls == 3


def test_bad_request_is_not_retried(session, event_and_city):
    event, city, _ = event_and_city
    client = ScriptedClient([LLMBadRequest("400: unknown model")])

    outcome = Investigator(session, client=client).investigate(event, city, rank=1)

    assert outcome.generator == Generator.TEMPLATE.value
    assert "rejected the request" in (outcome.fallback_reason or "")
    assert client.calls == 1


def test_prose_instead_of_a_tool_call_is_nudged_once(session, event_and_city):
    event, city, _ = event_and_city
    prose = lambda _turns: _response(text="Sure! Here's a summary of the weather.")  # noqa: E731
    client = ScriptedClient([prose])

    outcome = Investigator(session, client=client).investigate(event, city, rank=1)

    assert outcome.generator == Generator.TEMPLATE.value
    assert "prose instead of calling" in (outcome.fallback_reason or "")
    assert client.calls == 2


def test_tool_loop_hits_the_iteration_cap(session, event_and_city):
    """A model that only ever fetches data is stopped by the iteration bound."""
    event, city, _ = event_and_city
    client = ScriptedClient([fetch_evidence(event.id)])

    inv = Investigator(session, client=client)
    outcome = inv.investigate(event, city, rank=1)

    assert outcome.generator == Generator.TEMPLATE.value
    assert "iteration cap" in (outcome.fallback_reason or "")
    assert client.calls == inv.settings.agent_max_iterations


def test_tool_call_budget_is_enforced(session, event_and_city, monkeypatch):
    """Tool executions stop at the budget even if iterations remain."""
    event, city, _ = event_and_city

    def many_tools(_turns):
        return _response(
            tool_calls=[
                ToolCall(id=f"c{i}", name="get_city_weather", arguments={
                    "city_id": city.id, "date": event.local_date.isoformat()
                })
                for i in range(4)
            ]
        )

    client = ScriptedClient([many_tools])
    inv = Investigator(session, client=client)
    inv.settings = inv.settings.model_copy(update={"agent_max_tool_calls": 5})

    outcome = inv.investigate(event, city, rank=1)

    assert outcome.tool_call_count == 5
    assert outcome.generator == Generator.TEMPLATE.value


def test_deadline_stops_the_loop(session, event_and_city):
    event, city, _ = event_and_city
    client = ScriptedClient([fetch_evidence(event.id)])
    inv = Investigator(session, client=client)
    inv.settings = inv.settings.model_copy(update={"agent_timeout_seconds": -1.0})

    outcome = inv.investigate(event, city, rank=1)

    assert outcome.generator == Generator.TEMPLATE.value
    assert "deadline" in (outcome.fallback_reason or "")
    assert client.calls == 0


def test_unexpected_exception_still_publishes_a_template(session, event_and_city):
    event, city, _ = event_and_city

    def boom(_turns):
        raise RuntimeError("scripted crash")

    client = ScriptedClient([boom])
    outcome = Investigator(session, client=client).investigate(event, city, rank=1)

    assert outcome.generator == Generator.TEMPLATE.value
    assert "RuntimeError" in (outcome.fallback_reason or "")


def test_invalid_tool_response_is_data_not_a_crash(session, event_and_city):
    """A bad tool argument comes back as an error payload the model can read."""
    event, city, _ = event_and_city

    def bad_args(_turns):
        return _response(
            tool_calls=[
                ToolCall(id="c1", name="get_city_weather", arguments={"city_id": "nope"})
            ]
        )

    client = ScriptedClient(
        [bad_args, fetch_evidence(event.id, "c2"), submit(grounded_payload(event, city))]
    )
    outcome = Investigator(session, client=client).investigate(event, city, rank=1)

    assert outcome.generator == Generator.LLM.value
    assert outcome.tool_calls[0]["ok"] is False
    assert "nope" in outcome.tool_calls[0]["error"]
    assert outcome.tool_calls[1]["ok"] is True


def test_unknown_tool_name_is_reported_back(session, event_and_city):
    event, city, _ = event_and_city

    def unknown(_turns):
        return _response(tool_calls=[ToolCall(id="c1", name="get_the_future", arguments={})])

    client = ScriptedClient(
        [unknown, fetch_evidence(event.id, "c2"), submit(grounded_payload(event, city))]
    )
    outcome = Investigator(session, client=client).investigate(event, city, rank=1)

    assert outcome.generator == Generator.LLM.value
    assert outcome.tool_calls[0]["ok"] is False
    assert "unknown tool" in outcome.tool_calls[0]["error"]


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_month_to_date_budget_sums_only_this_month(session):
    make_run(
        session,
        run_id="old",
        started_at=datetime(2026, 8, 15, tzinfo=UTC),
        llm_calls=500,
        llm_estimated_usd=4.0,
    )
    make_run(
        session,
        run_id="new",
        started_at=datetime.now(UTC),
        llm_calls=12,
        llm_estimated_usd=0.25,
    )

    budget = month_to_date_budget(session, today=date(2026, 9, 22))

    assert budget.calls == 12
    assert budget.estimated_usd == pytest.approx(0.25)
    assert budget.exhausted is False


def test_exhausted_call_cap_skips_the_llm(session, event_and_city):
    event, city, _ = event_and_city
    client = ScriptedClient([submit(grounded_payload(event, city))])
    budget = BudgetState(
        calls=2000, estimated_usd=0.0, max_calls=2000, usd_budget=5.0, pricing_configured=False
    )

    outcome = Investigator(session, client=client, budget=budget).investigate(event, city)

    assert outcome.generator == Generator.TEMPLATE.value
    assert "call cap" in (outcome.fallback_reason or "")
    assert client.calls == 0


def test_usd_budget_does_not_bind_without_configured_prices(session, event_and_city):
    """A dollar ceiling cannot be enforced against an estimate of zero.

    This is why the call cap exists: it is the bound that holds out of the box.
    """
    event, city, _ = event_and_city
    client = ScriptedClient([submit(seed_only_payload(event, city))])
    budget = BudgetState(
        calls=1,
        estimated_usd=99.0,
        max_calls=2000,
        usd_budget=5.0,
        pricing_configured=False,
    )

    outcome = Investigator(session, client=client, budget=budget).investigate(event, city)
    assert outcome.generator == Generator.LLM.value


def test_usd_budget_binds_once_prices_are_configured(session, event_and_city):
    event, city, _ = event_and_city
    client = ScriptedClient([submit(grounded_payload(event, city))])
    budget = BudgetState(
        calls=1, estimated_usd=99.0, max_calls=2000, usd_budget=5.0, pricing_configured=True
    )

    outcome = Investigator(session, client=client, budget=budget).investigate(event, city)

    assert outcome.generator == Generator.TEMPLATE.value
    assert "budget" in (outcome.fallback_reason or "")
    assert client.calls == 0


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


def test_investigate_events_respects_top_n(session):
    city_a = make_city(session, city_id="us-phoenix-az", name="Phoenix")
    city_b = make_city(
        session,
        city_id="us-denver-co",
        name="Denver",
        admin="Colorado",
        timezone="America/Denver",
    )
    run = make_run(session)
    event_a = make_event(session, city_a, run=run, rank=1, observed_value=41.6)
    event_b = make_event(session, city_b, run=run, rank=2, observed_value=38.4)

    settings = get_settings().model_copy(update={"agent_investigate_top_n": 1})
    batch = investigate_events(
        session, [(1, event_a, city_a), (2, event_b, city_b)], settings=settings
    )

    assert len(batch.outcomes) == 1
    assert batch.outcomes[0].event_id == event_a.id


def test_investigate_events_aggregates_usage(session):
    city = make_city(session)
    run = make_run(session)
    event = make_event(session, city, run=run, rank=1, observed_value=41.6)

    client = ScriptedClient([fetch_evidence(event.id), submit(grounded_payload(event, city))])
    batch = investigate_events(session, [(1, event, city)], client=client)

    assert batch.llm_generated == 1
    assert batch.template_generated == 0
    assert batch.prompt_tokens == 1800
    assert batch.completion_tokens == 640
    assert batch.llm_calls == 2
    assert client.closed is True


def test_investigate_events_without_a_client_is_all_templates(session):
    city = make_city(session)
    run = make_run(session)
    event = make_event(session, city, run=run, rank=1, observed_value=41.6)

    batch = investigate_events(session, [(1, event, city)])

    assert batch.template_generated == 1
    assert batch.llm_generated == 0
    assert batch.estimated_usd == 0.0


# ---------------------------------------------------------------------------
# Prompt and tool surface
# ---------------------------------------------------------------------------


def test_submit_tool_schema_is_the_output_contract():
    schema = SUBMIT_TOOL_SPEC["input_schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "headline",
        "statistical_explanation",
        "historical_context",
        "caveats",
        "evidence",
        "confidence",
    }


def test_prompt_seed_facts_are_whitelisted(session, event_and_city):
    """The model may repeat figures we handed it in the opening message."""
    event, city, _ = event_and_city
    # Submitted with no tool call at all, so the only citable numbers are the ones
    # the opening message stated.
    client = ScriptedClient([submit(seed_only_payload(event, city))])

    outcome = Investigator(session, client=client).investigate(event, city, rank=1)

    assert outcome.generator == Generator.LLM.value
    assert outcome.tool_call_count == 0


def test_baseline_figures_are_not_citable_without_fetching_them(session, event_and_city):
    """The whitelist is built from what the model was actually shown.

    The payload here is entirely *correct* — every figure matches the database.
    It is still rejected, because the model never called a tool that returned
    them, so from the guards' point of view it guessed.
    """
    event, city, _ = event_and_city
    client = ScriptedClient([submit(grounded_payload(event, city))])

    outcome = Investigator(session, client=client).investigate(event, city, rank=1)

    assert outcome.generator == Generator.TEMPLATE.value
    assert any(
        v.startswith("ungrounded_number") for v in outcome.validation["violations"]
    )


def test_correction_message_names_the_actual_problem(session, event_and_city):
    event, city, _ = event_and_city
    bad = grounded_payload(event, city)
    bad["headline"] = f"{city.name} broke its all-time record at {event.observed_value:.1f} °C."

    captured: list[str] = []

    def submit_bad(turns):
        for turn in turns:
            for result in turn.tool_results:
                if result.name == "submit_explanation":
                    captured.append(result.content)
        return _response(
            tool_calls=[ToolCall(id="s", name="submit_explanation", arguments=bad)]
        )

    client = ScriptedClient([submit_bad])
    Investigator(session, client=client).investigate(event, city, rank=1)

    assert captured, "the rejection should be fed back to the model"
    feedback = json.loads(captured[0])
    assert feedback["accepted"] is False
    assert "record" in feedback["instruction"].lower()
    assert any("record" in v for v in feedback["problems"])


def test_temperature_metrics_are_not_the_only_supported_path(session):
    """Precipitation events carry the zero-inflated mixture through the template."""
    city = make_city(session, city_id="mx-merida-yuc", name="Mérida", admin="Yucatán",
                     country="MX", region="MX Yucatán", latitude=20.97, longitude=-89.62,
                     timezone="America/Merida", population=921770)
    run = make_run(session)
    wet = [0.0] * 300 + [round(0.4 * i, 1) for i in range(1, 151)]
    event = make_event(
        session,
        city,
        run=run,
        rank=1,
        metric=Metric.PRECIPITATION,
        observed_value=148.6,
        values=wet,
    )

    outcome = Investigator(session, client=None).investigate(event, city, rank=1)

    assert outcome.generator == Generator.TEMPLATE.value
    assert "mm" in outcome.explanation.headline
    assert "record" not in outcome.explanation.all_text().lower().replace(
        "not an official record", ""
    )
