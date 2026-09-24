"""The chat agent: question → read-only query → answer grounded in the result.

The model is called twice, deliberately:

1. **Plan** — the model writes a single read-only SELECT (plus a short note on
   what it does). It is *not* asked for the answer here, because the results do
   not exist yet and anything it said would be a prediction, not an answer.

2. **Answer** — after the query runs, the actual rows are fed back and the model
   states the specific result: the names, numbers and dates, read off the rows.

Splitting it this way is what stops the answer from becoming a sentence that
describes how to read the table ("see the row with the highest score") instead of
the answer itself ("Mexico City's mean temperature, at 2.77"). A refusal, or a
query that the validator rejects, returns without ever inventing a figure.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app import monitoring
from app.agent.llm import Turn
from app.chat.schema_context import build_agent_system_prompt, build_system_prompt
from app.chat.sql import SqlRejected, execute_query
from app.config import Settings

_MAX_OUTPUT_TOKENS = 800
_ANSWER_PREVIEW_ROWS = 20


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

    answer = _compose_answer(question, columns, rows, truncated, client)
    return ChatResult(
        question=question,
        answer=answer,
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


def _compose_answer(
    question: str,
    columns: list[str],
    rows: list[list],
    truncated: bool,
    client: Any,
) -> str:
    """State the answer from the query's actual results, never a description of it."""
    if not rows:
        return "No matching records were found for that question."

    preview = [list(row) for row in rows[:_ANSWER_PREVIEW_ROWS]]
    payload = json.dumps({"columns": columns, "rows": preview}, default=str)

    system = (
        "You are a data analyst answering a question using the query results provided. "
        "State the specific answer — the actual names, numbers and dates — directly, in a "
        "few plain sentences. Do not describe how to read the table and do not tell the "
        "reader to look at a row; say what the result is. Never invent a value that is "
        "not in the results."
    )
    user = f"Question: {question}\n\nQuery results:\n{payload}"
    if truncated:
        user += "\n\nThe result was truncated to a preview; more rows exist than are shown."

    response = client.complete(
        system=system,
        turns=[Turn(role="user", text=user)],
        tools=[],
        max_tokens=500,
    )
    text = (getattr(response, "text", None) or "").strip()
    if text:
        return text

    if len(rows) == 1:
        parts = [f"{column} = {rows[0][index]}" for index, column in enumerate(columns)]
        return "The result is " + ", ".join(parts) + "."
    return f"The query returned {len(rows)} rows."


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


def answer_agent_question(
    question: str, *, client: Any, settings: Settings, limit: int = 20
) -> dict[str, Any]:
    """Answer a question about the agentic flow from the recent MLflow traces."""
    data = monitoring.recent_trace_data(settings, limit=limit)

    if not data["available"]:
        return {
            "available": False,
            "note": data["note"],
            "trace_count": 0,
            "answer": f"Trace data is not available: {data['note']}",
        }

    traces = data["traces"]
    if not traces:
        return {
            "available": True,
            "note": data["note"],
            "trace_count": 0,
            "answer": "No traces have been recorded yet, so there is nothing to report on the "
            "agent's recent runs.",
        }

    payload = json.dumps(traces, default=str)
    response = client.complete(
        system=build_agent_system_prompt(),
        turns=[Turn(role="user", text=f"Question: {question}\n\nRecent traces:\n{payload}")],
        tools=[],
        max_tokens=600,
    )
    text = (getattr(response, "text", None) or "").strip()
    return {
        "available": True,
        "note": None,
        "trace_count": len(traces),
        "answer": text or f"{len(traces)} traces found — see the monitor page for detail.",
    }
