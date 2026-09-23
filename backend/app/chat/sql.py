"""Read-only SQL validation and execution.

This is defence in depth, not the last line. The real guarantee that a generated
query cannot write is the read-only database role the deployment should grant the
chat endpoint; this module is what stops an obviously-write query or a runaway
result set *before* it reaches the database, and makes the same guarantees
testable on SQLite.

The validator is deliberately strict and conservative: a single ``SELECT`` only,
no trailing statement separators, no write keywords. A generated query that trips
any of these is rejected with a clear message rather than executed.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


class SqlRejected(ValueError):
    """A generated query was refused before execution."""


#: Write / administrative keywords that must never appear in a chat query. The
#: list is a blacklist backstop; the read-only role is the primary control.
_FORBIDDEN = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|VACUUM|"
    r"COPY|ATTACH|DETACH|PRAGMA|EXPLAIN|ANALYZE|BEGIN|COMMIT|ROLLBACK|"
    r"SAVEPOINT|RELEASE|CALL|MERGE|REPLACE|INTO|SET|LOCK"
    r")\b",
    re.IGNORECASE,
)


def validate_sql(sql: str) -> str:
    """Return the normalised single ``SELECT`` statement, or raise :class:`SqlRejected`."""
    statement = (sql or "").strip()
    if not statement:
        raise SqlRejected("the query is empty")

    if ";" in statement:
        raise SqlRejected("only a single statement is allowed")

    head = statement.lstrip().upper()
    if not head.startswith("SELECT"):
        raise SqlRejected("only SELECT queries are allowed")

    if _FORBIDDEN.search(statement):
        raise SqlRejected("the query contains a disallowed write or administrative keyword")

    return statement.rstrip()


def execute_query(
    session: Session,
    sql: str,
    *,
    max_rows: int,
) -> tuple[list[str], list[list], bool]:
    """Execute a validated SELECT, capping rows. Returns ``(columns, rows, truncated)``.

    The row cap is appended here rather than asked of the model, so no generated
    query — however it is phrased — can return an unbounded result set.
    """
    statement = validate_sql(sql)
    limited = f"{statement} LIMIT {int(max_rows) + 1}"

    result = session.execute(text(limited))
    columns = list(result.keys())
    raw_rows = result.fetchall()

    truncated = len(raw_rows) > max_rows
    rows = [[_jsonable(value) for value in row] for row in raw_rows[:max_rows]]
    return columns, rows, truncated


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)
