"""The live-LLM evaluator's pure scoring and rendering, without a model."""

from __future__ import annotations

import pytest

from evals.live import EventResult, aggregate, render_comparison


def _results() -> list[EventResult]:
    return [
        EventResult(
            event_id="e1",
            city="Phoenix",
            metric="temp_max",
            generator="llm",
            grounded=True,
            violations=(),
            tool_call_count=3,
            attempts=1,
            latency_ms=1000,
            prompt_tokens=100,
            completion_tokens=50,
            estimated_usd=0.001,
            fallback_reason=None,
        ),
        EventResult(
            event_id="e2",
            city="Miami",
            metric="temp_min",
            generator="template",
            grounded=True,
            violations=(),
            tool_call_count=0,
            attempts=0,
            latency_ms=0,
            prompt_tokens=0,
            completion_tokens=0,
            estimated_usd=0.0,
            fallback_reason="no LLM provider configured",
        ),
        EventResult(
            event_id="e3",
            city="Denver",
            metric="wind_gust",
            generator="llm",
            grounded=False,
            violations=("ungrounded_number",),
            tool_call_count=2,
            attempts=2,
            latency_ms=1500,
            prompt_tokens=120,
            completion_tokens=60,
            estimated_usd=0.002,
            fallback_reason=None,
        ),
    ]


def test_aggregate_computes_rates():
    summary = aggregate("a", _results())
    assert summary.events == 3
    assert summary.llm_generated == 2
    assert summary.template_generated == 1
    assert summary.grounded == 2
    assert summary.grounding_rate == pytest.approx(0.6667, abs=1e-4)
    assert summary.avg_tool_calls == pytest.approx(1.6667, abs=1e-4)
    assert summary.avg_latency_ms == pytest.approx(833.3333, abs=1e-3)
    assert summary.prompt_tokens == 220
    assert summary.completion_tokens == 110
    assert summary.estimated_usd == pytest.approx(0.003)
    assert summary.violations == {"ungrounded_number": 1}


def test_render_comparison_shows_both_labels():
    text = render_comparison(
        aggregate("current", _results()), aggregate("candidate", _results()[:1])
    )
    assert "current" in text
    assert "candidate" in text
    assert "Grounding rate" in text
