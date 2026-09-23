"""Test configuration for the evaluation delivery modules.

Puts ``backend`` and the repository root on ``sys.path`` and pins the environment
so a developer's ``.env`` cannot leak a real database or a paid model into the
tests. ``app`` is imported lazily inside the tests, so the path setup here only
has to run before the first test body executes.
"""

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
for path in (REPO_ROOT, BACKEND):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

for var in (
    "DATABASE_URL",
    "LLM_PROVIDER",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "WEATHER_PROVIDER",
    "OPEN_METEO_API_KEY",
    "SMTP_HOST",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "EVAL_EMAIL_TO",
):
    os.environ.pop(var, None)

os.environ["ENVIRONMENT"] = "test"
os.environ["LLM_PROVIDER"] = "none"


@pytest.fixture
def report():
    """A small, deterministic report covering the value types persistence handles."""
    from evals.harness import Case, Metric, Report, Suite

    return Report(
        generated_at="2026-09-23T01:25:01+00:00",
        git_commit="abc123",
        git_dirty=False,
        methodology_version="1.0.0",
        environment={"weather_provider": "fixture"},
        suites=[
            Suite(
                id="ranking",
                title="Top-10 ranking",
                description="Does the board order correctly?",
                status="passed",
                duration_ms=5,
                metrics=[
                    Metric(label="cases", value=15),
                    Metric(label="worst_rank_delta", value=0.0),
                    Metric(label="note", value="hello"),
                    Metric(label="pricing", value=None),
                    Metric(label="enabled", value=True),
                ],
                cases=[Case(id="c1", title="tie-break", passed=True)],
            ),
            Suite(
                id="grounding",
                title="Grounding",
                description="Are the guards still working?",
                status="failed",
                duration_ms=46,
                metrics=[Metric(label="violations", value=2)],
                cases=[
                    Case(id="c2", title="ungrounded", passed=False),
                    Case(id="c3", title="record-claim", passed=True),
                ],
                error="a grounding failure",
            ),
        ],
        limitations=[],
    )
