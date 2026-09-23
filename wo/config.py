"""Configuration resolution for the CLI.

The CLI reads the same environment variables the pipeline and evaluation harness
do, so pointing it at a deployment is a matter of exporting what is already
exported. Command-line flags override environment variables; anything not set
falls back to the project's defaults (experiment ``weather-outliers``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

DEFAULT_EXPERIMENT = "weather-outliers"


@dataclass(frozen=True)
class Config:
    #: The application database URL. ``None`` means "use the application's own
    #: settings", which also normalises Railway's ``postgres://`` scheme.
    database_url: str | None = None
    mlflow_tracking_uri: str | None = None
    mlflow_experiment: str = DEFAULT_EXPERIMENT
    mlflow_username: str | None = None
    mlflow_password: str | None = None


def from_env() -> Config:
    return Config(
        database_url=os.environ.get("DATABASE_URL") or None,
        mlflow_tracking_uri=os.environ.get("MLFLOW_TRACKING_URI") or None,
        mlflow_experiment=os.environ.get("MLFLOW_EXPERIMENT") or DEFAULT_EXPERIMENT,
        mlflow_username=os.environ.get("MLFLOW_TRACKING_USERNAME") or None,
        mlflow_password=os.environ.get("MLFLOW_TRACKING_PASSWORD") or None,
    )


def with_overrides(config: Config, **overrides: str | None) -> Config:
    """Return a copy of ``config`` with any non-``None`` overrides applied."""
    updates = {key: value for key, value in overrides.items() if value is not None}
    return replace(config, **updates)
