"""The delivery entry point's small pure helpers."""

from __future__ import annotations

from evals.deliver import _safe_url


def test_safe_url_redacts_credentials():
    assert (
        _safe_url("postgresql+psycopg://user:secret@db.internal:5432/weather")
        == "postgresql+psycopg://db.internal:5432/…"
    )


def test_safe_url_passes_sqlite_through():
    assert _safe_url("sqlite+pysqlite:///tmp/x.db") == "sqlite+pysqlite:///tmp/x.db"
