"""Process setup that must happen before ``app`` is imported.

Two jobs, both of which have to run first or not at all:

1. Put ``backend/`` on ``sys.path``. The harness lives beside the backend rather
   than inside it, because it evaluates the whole system — pipeline, guards, and
   HTTP surface — and burying it in one service's package would misrepresent
   that.
2. Pin the configuration to an offline one. A developer's ``.env`` may hold a
   real weather key and a real model key, and an evaluation that quietly spent
   money or hit a rate limit would be worse than no evaluation. The pins below
   are explicit environment variables, which outrank any ``.env`` file
   pydantic-settings would otherwise read.

``--live-llm`` on the runner removes the LLM pin, which is the one case where
reaching a paid provider is the point of the run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"

#: The scratch database. In memory, so an evaluation never touches a real one and
#: leaves nothing behind to clean up.
SCRATCH_DATABASE_URL = "sqlite+pysqlite:///:memory:"


def configure(*, live_llm: bool = False) -> None:
    """Prepare the interpreter. Call once, before importing anything from ``app``."""
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))

    os.environ["DATABASE_URL"] = SCRATCH_DATABASE_URL
    os.environ["ENVIRONMENT"] = "test"
    os.environ["WEATHER_PROVIDER"] = "fixture"
    # The harness prints a summary table; per-request application logs would bury
    # it. Failures are captured in the report, not in the log stream.
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    os.environ.pop("OPEN_METEO_API_KEY", None)

    if live_llm:
        # Leave LLM_PROVIDER and the API keys as the operator set them.
        return

    os.environ["LLM_PROVIDER"] = "none"
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        os.environ.pop(key, None)


def llm_mode() -> str:
    """How the grounding suite should describe the model it evaluated."""
    provider = os.environ.get("LLM_PROVIDER", "none")
    if provider == "none":
        return "deterministic_templates_only"
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"))
    return f"live_llm:{provider}" if has_key else "deterministic_templates_only"
