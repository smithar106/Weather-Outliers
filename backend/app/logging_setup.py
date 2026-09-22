"""Logging configuration shared by the API and the pipeline.

This lives in its own module rather than in ``app.main`` because importing
``app.main`` builds a FastAPI application as a side effect. The cron worker needs
the log format and nothing else, and a batch job that instantiates a web server
in order to configure a handler is a job that will eventually be debugged at an
inconvenient hour.
"""

from __future__ import annotations

import json
import logging
import sys

from app.config import Settings


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for a log aggregator that expects it."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(settings: Settings) -> None:
    """Install one handler on the root logger, replacing anything already there.

    Uvicorn installs its own handlers; without replacing them, every line is
    emitted twice in one format and once in the other.
    """
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())

    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv = logging.getLogger(name)
        uv.handlers = []
        uv.propagate = True

    # SQLAlchemy's INFO level is full SQL echo; that is what db_echo is for.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
