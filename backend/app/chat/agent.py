"""The chat agent: one question in, one query + answer out.

The model is asked for *both* the SQL and a plain-language answer, but the
answer rows returned to the caller come from the executor, not from the model —
so a number the model made up cannot survive the round trip. A question the
schema cannot answer becomes an explicit refusal rather than a fabricated query.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.agent.llm import Turn
from app.chat.schema_context import build_system_prompt
from app.chat.sql import SqlRejected, execute_query

_MAX_OUTPUT_TOKENS = 800


@dataclass(frozen=True)
class ChatResult:
    question: str
    answer: str
    sql: str | None = None
    explanation: str | None = None
    columns: tuple[str, ...] = ()
    rows: tuple[tuple, ...] = ()
    truncated: bool = False
    refused: bool = False


def answer_question(question: str, *, client: Any, session: Session, max_rows: int) -> ChatResult:
    plan = _plan(question, client)

    if plan.get("error") or not isinstance(plan.get("sql"), str):
        return ChatResult(
            question=question,
            answer=plan.get("answer") or "I cannot answer that question from this data.",
            explanation=plan.get("explanation"),
            refused=True,
        )

    sql = plan["sql"]
    try:
        columns, rows, truncated = execute_query(session, sql, max_rows=max_rows)
    except SqlRejected as exc:
        return ChatResult(
            question=question,
            answer=f"The generated query was rejected: {exc}",
            explanation=plan.get("explanation"),
            sql=sql,
            refused=True,
        )

    return ChatResult(
        question=question,
        answer=plan.get("answer") or "",
        explanation=plan.get("explanation"),
        sql=sql,
        columns=tuple(columns),
        rows=tuple(tuple(row) for row in rows),
        truncated=truncated,
        refused=False,
    )


def _plan(question: str, client: Any) -> dict[str, Any]:
    response = client.complete(
        system=build_system_prompt(),
        turns=[Turn(role="user", text=question)],
        tools=[],
        max_tokens=_MAX_OUTPUT_TOKENS,
    )
    return _parse_plan(getattr(response, "text", None))


def _parse_plan(text: str | None) -> dict[str, Any]:
    """Pull one JSON object out of the model's reply, tolerating markdown fences."""
    if not text:
        return {"error": "empty_response", "answer": "The model returned nothing."}

    candidate = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    candidate = re.sub(r"\s*```$", "", candidate)

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end <= start:
        return {"error": "unparseable", "answer": "The model did not return a valid response."}

    try:
        data = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return {"error": "unparseable", "answer": "The model did not return valid JSON."}

    if not isinstance(data, dict):
        return {"error": "unparseable", "answer": "The model's response was not an object."}

    if data.get("error"):
        return {
            "error": str(data.get("error")),
            "answer": str(data.get("answer") or "I cannot answer that question from this data."),
            "explanation": data.get("explanation"),
        }
    return data
