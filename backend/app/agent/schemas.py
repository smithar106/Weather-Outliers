"""Structured output contract for the investigation agent.

The model is never allowed to return prose directly into the database. It must
return JSON that validates against :class:`EventExplanation`, and that object
must then survive the grounding guards in ``app/agent/guards.py``. Anything that
fails either step falls back to a deterministic template.

Length bounds are part of the contract rather than a style preference: they stop
a model from padding a thin evidence base into something that reads
authoritative.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EvidenceItem(BaseModel):
    """One number the explanation relies on, tied to the tool that produced it."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=80)
    value: float | int | str
    unit: str | None = Field(default=None, max_length=16)
    source_tool: str = Field(min_length=1, max_length=48)


class EventExplanation(BaseModel):
    """The agent's verdict on a single event."""

    model_config = ConfigDict(extra="forbid")

    # The bounds below are the hard validity limits. The ``maxLength`` values in
    # EXPLANATION_JSON_SCHEMA are deliberately tighter: those are the instruction
    # given to the model, and the slack here absorbs a model that runs a little
    # long without discarding an otherwise well-grounded answer.
    headline: str = Field(min_length=10, max_length=130)
    statistical_explanation: str = Field(min_length=40, max_length=900)
    historical_context: str = Field(min_length=20, max_length=700)
    caveats: str = Field(min_length=15, max_length=600)
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=8)
    confidence: Literal["high", "medium", "low"] = "medium"

    @field_validator("headline")
    @classmethod
    def _no_trailing_period_spam(cls, v: str) -> str:
        return v.strip()

    def all_text(self) -> str:
        """Everything the reader will see, for the grounding checks to scan."""
        return "\n".join(
            [self.headline, self.statistical_explanation, self.historical_context, self.caveats]
        )


class InvestigationOutcome(BaseModel):
    """Result of investigating one event, including how it was produced."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    explanation: EventExplanation
    generator: Literal["llm", "template"]
    llm_provider: str | None = None
    model: str | None = None
    attempts: int = 1
    tool_calls: list[dict] = Field(default_factory=list)
    tool_call_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_usd: float = 0.0
    latency_ms: int = 0
    validation: dict = Field(default_factory=dict)
    fallback_reason: str | None = None


#: JSON Schema handed to the model as the required shape of its final answer.
#: Written out rather than generated from the Pydantic model so that the wording
#: of each field description is deliberate — these descriptions are the
#: instructions that do the most work in practice.
EXPLANATION_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "headline",
        "statistical_explanation",
        "historical_context",
        "caveats",
        "evidence",
        "confidence",
    ],
    "properties": {
        "headline": {
            "type": "string",
            "maxLength": 110,
            "description": (
                "One sentence naming the city and what was unusual. State the observed "
                "value. Do NOT use the word 'record' and do NOT claim any superlative "
                "such as 'hottest ever'."
            ),
        },
        "statistical_explanation": {
            "type": "string",
            "maxLength": 700,
            "description": (
                "2-4 sentences explaining why this value is statistically unusual for this "
                "city at this time of year. Cite the observed value, the seasonal baseline, "
                "and the percentile or tail probability exactly as the tools returned them. "
                "Explain what the number means in plain language."
            ),
        },
        "historical_context": {
            "type": "string",
            "maxLength": 600,
            "description": (
                "1-3 sentences placing the value against the reference-period sample "
                "returned by get_city_baseline or get_historical_extremes. Describe those "
                "figures as the highest/lowest value in the 1991-2020 reference sample for "
                "this part of the calendar — never as a record of any kind."
            ),
        },
        "caveats": {
            "type": "string",
            "maxLength": 500,
            "description": (
                "1-3 sentences on the limits of this result: whether the data is reanalysis "
                "or provisional model analysis rather than a station reading, whether the "
                "tail probability hit its resolution floor, and that no meteorological cause "
                "is being asserted."
            ),
        },
        "evidence": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "description": "Every number cited above, with the tool it came from.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["label", "value", "source_tool"],
                "properties": {
                    "label": {"type": "string", "maxLength": 80},
                    "value": {"type": ["number", "string"]},
                    "unit": {"type": ["string", "null"], "maxLength": 16},
                    "source_tool": {
                        "type": "string",
                        "description": "Name of the tool call that returned this value.",
                    },
                },
            },
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": (
                "high when the baseline is sufficient and the tail probability is not "
                "bounded; medium when the probability hit its floor; low when the baseline "
                "is marked insufficient or data is incomplete."
            ),
        },
    },
}
