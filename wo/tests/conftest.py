"""Test configuration for the ``wo`` package.

Mirrors ``backend/tests/conftest.py`` and ``evals/environment.py``: put ``backend``
on ``sys.path`` and pin the environment so a developer's ``.env`` or shell cannot
point the tests at a real database, a real weather provider, or a paid model.
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

for var in (
    "DATABASE_URL",
    "LLM_PROVIDER",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "WEATHER_PROVIDER",
    "OPEN_METEO_API_KEY",
    "AGENT_USD_PER_MTOK_INPUT",
    "AGENT_USD_PER_MTOK_OUTPUT",
    "MLFLOW_TRACKING_URI",
    "MLFLOW_TRACKING_USERNAME",
    "MLFLOW_TRACKING_PASSWORD",
):
    os.environ.pop(var, None)

os.environ["ENVIRONMENT"] = "test"
os.environ["LLM_PROVIDER"] = "none"
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
