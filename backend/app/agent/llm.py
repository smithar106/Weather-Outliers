"""Provider-agnostic LLM adapter.

The investigator never imports a vendor SDK. It speaks the small neutral
vocabulary defined here — :class:`Turn`, :class:`ToolCall`, :class:`ToolResult`,
:class:`LLMResponse` — and one of the concrete clients translates that into
Anthropic's or OpenAI's wire format. Adding a third provider means writing one
class and one line in :func:`get_llm_client`; it does not mean touching the
agent loop, the guards, or the pipeline.

Two decisions worth calling out:

**The final answer arrives as a tool call.** Rather than asking for JSON in prose
and parsing whatever comes back, the model is given a ``submit_explanation``
tool whose input schema *is* the output contract. Both providers enforce tool
input schemas server-side, so this gets schema adherence for free and works
identically across vendors. ``submit_explanation`` is an output channel, not a
data source, so it is not counted against the tool-call budget.

**Cost is estimated, never assumed.** Token counts come from the provider
response; dollars come from operator-supplied prices that default to zero. A
zero-cost estimate is honest about not knowing the price rather than quietly
wrong, and the hard call cap — not the dollar figure — is what actually bounds
spend out of the box.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

SUBMIT_TOOL = "submit_explanation"


class LLMError(Exception):
    """Base class for adapter failures."""


class LLMConfigError(LLMError):
    """Misconfiguration — not retryable."""


class LLMUnavailable(LLMError):
    """Transport or server-side failure — retryable."""


class LLMBadRequest(LLMError):
    """The request itself was rejected; retrying unchanged will not help."""


# ---------------------------------------------------------------------------
# Neutral message vocabulary
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(slots=True)
class ToolResult:
    call_id: str
    name: str
    content: str  # JSON-encoded tool output


@dataclass(slots=True)
class Turn:
    role: Literal["user", "assistant"]
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)


@dataclass(slots=True)
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall]
    prompt_tokens: int
    completion_tokens: int
    stop_reason: str | None
    model: str

    @property
    def submission(self) -> ToolCall | None:
        for call in self.tool_calls:
            if call.name == SUBMIT_TOOL:
                return call
        return None

    @property
    def data_tool_calls(self) -> list[ToolCall]:
        return [c for c in self.tool_calls if c.name != SUBMIT_TOOL]


@runtime_checkable
class LLMClient(Protocol):
    """What the investigator needs from a model provider."""

    provider: str
    model: str

    def complete(
        self,
        *,
        system: str,
        turns: list[Turn],
        tools: list[dict],
        max_tokens: int,
    ) -> LLMResponse: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


class AnthropicClient:
    """Adapter for the Anthropic Messages API."""

    provider = "anthropic"

    def __init__(self, settings: Settings) -> None:
        if not settings.anthropic_api_key:
            raise LLMConfigError("ANTHROPIC_API_KEY is not set")
        self.model = settings.anthropic_model
        self._url = settings.anthropic_base_url.rstrip("/") + "/messages"
        self._client = httpx.Client(
            timeout=settings.agent_timeout_seconds,
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": settings.anthropic_api_version,
                "content-type": "application/json",
            },
        )

    def complete(
        self, *, system: str, turns: list[Turn], tools: list[dict], max_tokens: int
    ) -> LLMResponse:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [self._encode_turn(t) for t in turns],
            "tools": [
                {
                    "name": tool["name"],
                    "description": tool["description"],
                    "input_schema": tool["input_schema"],
                }
                for tool in tools
            ],
        }
        data = _post_json(self._client, self._url, payload)

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                text_parts.append(block.get("text") or "")
            elif block.get("type") == "tool_use":
                calls.append(
                    ToolCall(
                        id=block.get("id") or "",
                        name=block.get("name") or "",
                        arguments=block.get("input") or {},
                    )
                )
        usage = data.get("usage") or {}
        return LLMResponse(
            text="\n".join(p for p in text_parts if p) or None,
            tool_calls=calls,
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            stop_reason=data.get("stop_reason"),
            model=data.get("model") or self.model,
        )

    @staticmethod
    def _encode_turn(turn: Turn) -> dict:
        content: list[dict] = []
        if turn.tool_results:
            for result in turn.tool_results:
                content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": result.call_id,
                        "content": result.content,
                    }
                )
        if turn.text:
            content.append({"type": "text", "text": turn.text})
        for call in turn.tool_calls:
            content.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                }
            )
        return {"role": turn.role, "content": content}

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


class OpenAIClient:
    """Adapter for the OpenAI Chat Completions API (and compatible gateways)."""

    provider = "openai"

    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise LLMConfigError("OPENAI_API_KEY is not set")
        if not settings.openai_model:
            raise LLMConfigError(
                "OPENAI_MODEL is not set. This project ships no default OpenAI model "
                "name so that it cannot silently point at a deprecated one — set the "
                "model you intend to pay for."
            )
        self.model = settings.openai_model
        self._url = settings.openai_base_url.rstrip("/") + "/chat/completions"
        self._client = httpx.Client(
            timeout=settings.agent_timeout_seconds,
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "content-type": "application/json",
            },
        )

    def complete(
        self, *, system: str, turns: list[Turn], tools: list[dict], max_tokens: int
    ) -> LLMResponse:
        messages: list[dict] = [{"role": "system", "content": system}]
        for turn in turns:
            messages.extend(self._encode_turn(turn))

        payload = {
            "model": self.model,
            "max_completion_tokens": max_tokens,
            "messages": messages,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["input_schema"],
                    },
                }
                for tool in tools
            ],
        }
        data = _post_json(self._client, self._url, payload)

        choices = data.get("choices") or []
        message = (choices[0].get("message") if choices else None) or {}
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                # A malformed argument blob is a tool-selection failure, not a
                # crash: hand it through and let the tool layer report the error.
                arguments = {"__malformed__": function.get("arguments")}
            calls.append(
                ToolCall(
                    id=raw.get("id") or "",
                    name=function.get("name") or "",
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
        usage = data.get("usage") or {}
        return LLMResponse(
            text=message.get("content") or None,
            tool_calls=calls,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            stop_reason=(choices[0].get("finish_reason") if choices else None),
            model=data.get("model") or self.model,
        )

    @staticmethod
    def _encode_turn(turn: Turn) -> list[dict]:
        out: list[dict] = []
        # Tool results are their own role in this API and must precede any new text.
        for result in turn.tool_results:
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "content": result.content,
                }
            )
        if turn.role == "assistant":
            message: dict[str, Any] = {"role": "assistant", "content": turn.text}
            if turn.tool_calls:
                message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                    for call in turn.tool_calls
                ]
            out.append(message)
        elif turn.text:
            out.append({"role": "user", "content": turn.text})
        return out

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# Shared transport
# ---------------------------------------------------------------------------


def _post_json(client: httpx.Client, url: str, payload: dict) -> dict:
    try:
        response = client.post(url, json=payload)
    except httpx.TimeoutException as exc:
        raise LLMUnavailable(f"request to {url} timed out") from exc
    except httpx.HTTPError as exc:
        raise LLMUnavailable(f"transport error calling {url}: {exc}") from exc

    if response.status_code == 429:
        raise LLMUnavailable("rate limited by provider")
    if 400 <= response.status_code < 500:
        raise LLMBadRequest(f"{response.status_code}: {_error_text(response)}")
    if response.status_code >= 500:
        raise LLMUnavailable(f"{response.status_code}: {_error_text(response)}")

    try:
        return response.json()
    except ValueError as exc:
        raise LLMUnavailable("provider returned a non-JSON body") from exc


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)[:300]
        if error:
            return str(error)[:300]
    return json.dumps(body)[:300]


# ---------------------------------------------------------------------------
# Factory and cost estimation
# ---------------------------------------------------------------------------


def get_llm_client(settings: Settings | None = None) -> LLMClient | None:
    """Build the configured client, or ``None`` when the agent should use templates."""
    settings = settings or get_settings()
    if not settings.llm_enabled:
        return None
    if settings.llm_provider == "anthropic":
        return AnthropicClient(settings)
    if settings.llm_provider == "openai":
        return OpenAIClient(settings)
    return None


def estimate_usd(
    prompt_tokens: int, completion_tokens: int, settings: Settings | None = None
) -> float:
    """Cost estimate from operator-supplied prices; 0.0 when none are configured."""
    settings = settings or get_settings()
    per_input = settings.agent_usd_per_mtok_input
    per_output = settings.agent_usd_per_mtok_output
    if per_input <= 0 and per_output <= 0:
        return 0.0
    return round(
        (prompt_tokens / 1_000_000.0) * per_input
        + (completion_tokens / 1_000_000.0) * per_output,
        6,
    )


def pricing_configured(settings: Settings | None = None) -> bool:
    """Whether dollar figures mean anything in this deployment."""
    settings = settings or get_settings()
    return settings.agent_usd_per_mtok_input > 0 or settings.agent_usd_per_mtok_output > 0
