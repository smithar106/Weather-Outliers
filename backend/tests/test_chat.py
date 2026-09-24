"""The chat agent: SQL validation/execution and the LLM wrapper."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.chat.agent import _parse_plan, answer_agent_question, answer_question
from app.chat.sql import SqlRejected, _jsonable, execute_query, validate_sql

# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_validate_sql_accepts_a_select():
    assert validate_sql("SELECT * FROM cities") == "SELECT * FROM cities"
    assert validate_sql("  select id from cities  ") == "select id from cities"


def test_validate_sql_rejects_an_empty_query():
    with pytest.raises(SqlRejected):
        validate_sql("")
    with pytest.raises(SqlRejected):
        validate_sql(None)


def test_validate_sql_rejects_non_select():
    with pytest.raises(SqlRejected):
        validate_sql("DELETE FROM cities")
    with pytest.raises(SqlRejected):
        validate_sql("WITH x AS (SELECT 1) SELECT * FROM x")


def test_validate_sql_rejects_multiple_statements():
    with pytest.raises(SqlRejected):
        validate_sql("SELECT 1; DROP TABLE cities")


def test_validate_sql_rejects_write_keywords():
    with pytest.raises(SqlRejected):
        validate_sql("SELECT * INTO other FROM cities")


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------


def test_execute_query_returns_columns_and_rows(session):
    columns, rows, truncated = execute_query(session, "SELECT 1 AS n", max_rows=100)
    assert columns == ["n"]
    assert rows == [[1]]
    assert truncated is False


def test_execute_query_caps_rows(session):
    _, rows, truncated = execute_query(
        session, "SELECT 1 AS n UNION ALL SELECT 2", max_rows=1
    )
    assert rows == [[1]]
    assert truncated is True


def test_jsonable_converts_non_json_types():
    assert _jsonable(None) is None
    assert _jsonable(3) == 3
    assert _jsonable("x") == "x"
    assert _jsonable(date(2026, 1, 1)) == "2026-01-01"
    assert _jsonable(datetime(2026, 1, 1, 12, 0, 0)) == "2026-01-01T12:00:00"
    assert _jsonable(Decimal("1.5")) == 1.5


# ---------------------------------------------------------------------------
# plan parsing
# ---------------------------------------------------------------------------


def test_parse_plan_strips_markdown_fences():
    plan = _parse_plan('```json\n{"sql": "SELECT 1", "answer": "x"}\n```')
    assert plan["sql"] == "SELECT 1"


def test_parse_plan_handles_refusal():
    plan = _parse_plan('{"error": "cannot_answer", "answer": "nope"}')
    assert plan["error"] == "cannot_answer"
    assert plan["answer"] == "nope"


def test_parse_plan_tolerates_garbage():
    assert _parse_plan("")["error"] == "empty_response"
    assert _parse_plan("not json")["error"] == "unparseable"


# ---------------------------------------------------------------------------
# the agent
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)

    def complete(self, *, system, turns, tools, max_tokens):
        text = self._responses.pop(0) if self._responses else ""
        return SimpleNamespace(text=text)


def test_answer_question_returns_executed_rows(session):
    client = _FakeClient('{"sql": "SELECT 1 AS n", "explanation": "why"}', "The answer is one.")
    result = answer_question("q?", client=client, session=session, max_rows=100)
    assert result.refused is False
    assert result.sql == "SELECT 1 AS n"
    assert result.columns == ("n",)
    assert result.rows == ((1,),)
    assert result.answer == "The answer is one."


def test_answer_question_grounds_answer_in_rows(session):
    client = _FakeClient(
        '{"sql": "SELECT 1 AS n", "explanation": "why"}',
        "The most unusual event is Mexico City with a score of 2.77.",
    )
    result = answer_question("most unusual?", client=client, session=session, max_rows=100)
    assert result.answer == "The most unusual event is Mexico City with a score of 2.77."


def test_answer_question_empty_result_needs_no_model(session):
    client = _FakeClient('{"sql": "SELECT 1 AS n WHERE 1 = 0", "explanation": "why"}')
    result = answer_question("q?", client=client, session=session, max_rows=100)
    assert result.refused is False
    assert result.answer == "No matching records were found for that question."


def test_answer_question_refuses_when_model_refuses(session):
    client = _FakeClient('{"error": "cannot_answer", "answer": "no schema"}')
    result = answer_question("q?", client=client, session=session, max_rows=100)
    assert result.refused is True
    assert result.sql is None
    assert result.rows == ()


def test_answer_question_rejects_writing_sql(session):
    client = _FakeClient('{"sql": "DELETE FROM cities", "explanation": "nope"}')
    result = answer_question("q?", client=client, session=session, max_rows=100)
    assert result.refused is True
    assert "rejected" in result.answer


def test_answer_question_handles_unparseable_response(session):
    client = _FakeClient("garbage that is not json")
    result = answer_question("q?", client=client, session=session, max_rows=100)
    assert result.refused is True


def test_answer_agent_question_is_grounded_in_analytics(monkeypatch):
    from app import monitoring

    monkeypatch.setattr(
        monitoring,
        "trace_analytics",
        lambda settings, since_days=30: {
            "available": True,
            "note": None,
            "window_days": 30,
            "traces": 1,
            "runs": 1,
            "spans": [
                {"name": "fetch_observations", "count": 1, "avg_ms": 9000, "max_ms": 9000, "errors": 0}
            ],
            "errors": [],
            "fallbacks": [],
            "generator": {"llm": 0, "template": 10},
            "tokens": {"prompt": 0, "completion": 0},
            "estimated_usd": 0.0,
            "recent_runs": [{"run_id": "r", "status": "OK", "duration_ms": 9000}],
        },
    )
    client = _FakeClient("The pipeline is healthy; the slowest stage was the fetch at 9s.")
    result = answer_agent_question("how's the agent?", client=client, settings=object())
    assert result["available"] is True
    assert result["trace_count"] == 1
    assert "9s" in result["answer"]


def test_answer_agent_question_handles_no_traces(monkeypatch):
    from app import monitoring

    monkeypatch.setattr(
        monitoring,
        "trace_analytics",
        lambda settings, since_days=30: {
            "available": True,
            "note": "no traces yet",
            "traces": 0,
            "runs": 0,
        },
    )
    result = answer_agent_question("how's the agent?", client=_FakeClient(), settings=object())
    assert result["trace_count"] == 0
    assert "No traces" in result["answer"]


# ---------------------------------------------------------------------------
# the endpoint
# ---------------------------------------------------------------------------


def test_chat_disabled_returns_404(client):
    resp = client.post("/api/chat", json={"question": "hello"})
    assert resp.status_code == 404


def test_post_to_read_routes_is_rejected(client):
    resp = client.post("/api/rankings/latest")
    assert resp.status_code == 405


def test_chat_happy_path(monkeypatch, client, test_settings):
    # The route reads the cached settings, so mutate that instance in place.
    test_settings.chat_enabled = True
    test_settings.llm_provider = "openai"
    test_settings.openai_api_key = "test-key"
    test_settings.openai_model = "test-model"
    test_settings.agent_monthly_usd_budget = 5.0

    class FakeClient:
        model = "test-model"

        def __init__(self):
            self._responses = [
                '{"sql": "SELECT 1 AS n", "explanation": "constant"}',
                "One row.",
            ]

        def complete(self, *, system, turns, tools, max_tokens):
            return SimpleNamespace(text=self._responses.pop(0))

        def close(self):
            pass

    monkeypatch.setattr("app.api.routes.get_llm_client", lambda s: FakeClient())
    monkeypatch.setattr(
        "app.api.routes.month_to_date_budget",
        lambda s, st: SimpleNamespace(exhausted=False, reason=None),
    )

    resp = client.post("/api/chat", json={"question": "what is one?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["refused"] is False
    assert body["sql"] == "SELECT 1 AS n"
    assert body["columns"] == ["n"]
    assert body["rows"] == [[1]]
    assert body["row_count"] == 1

