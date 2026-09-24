"""The NL→SQL chat agent: turn a question into a read-only query and an answer.

This package is the "ask questions about the data" half of observability. It is
deliberately the smallest safe thing: a curated schema description, a
read-only SQL validator and executor, and a thin LLM wrapper that returns SQL
plus a plain-language answer. The actual answer rows are attached by the
executor — never by the model — so a hallucinated number cannot survive the
round trip.

``answer_question`` answers questions about the application's stored data (via a
read-only SELECT). ``answer_agent_question`` answers questions about the agentic
pipeline flow, grounded in recent MLflow traces.

Nothing here writes to the database, and nothing here is enabled unless
``CHAT_ENABLED`` is set.
"""

from app.chat.agent import answer_agent_question, answer_question

CHAT_PATH = "/api/chat"

__all__ = ["CHAT_PATH", "answer_agent_question", "answer_question"]
