"""Provider-neutral types shared by every model provider.

Conversation history is kept in one canonical shape:
    {"role": "user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": [ToolCall-as-dict], "raw": ..., "provider": ..., "model": ...}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}
Each provider converts that shape to its own wire format.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema (object)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] | None
    raw_arguments: str = ""
    parse_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments or {}}


@dataclass
class TurnResult:
    """Everything a provider produced for one model call."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"   # stop | tool_calls | length | refusal | cancelled
    raw: Any = None               # provider-native assistant payload to replay later
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""


class ProviderError(Exception):
    """A failure translated into something a person can act on."""

    def __init__(self, kind: str, message: str, *, status: int | None = None,
                 retry_after: float | None = None, provider: str = ""):
        super().__init__(message)
        self.kind = kind          # auth | rate_limit | connection | not_found | bad_request | server | refusal | setup | tool_format
        self.message = message
        self.status = status
        self.retry_after = retry_after
        self.provider = provider

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "status": self.status,
                "retry_after": self.retry_after, "provider": self.provider}


RETRYABLE = {"rate_limit", "connection", "server"}


class Provider:
    id: str = ""
    label: str = ""
    needs_key: str | None = None   # env var name of the API key, if any

    def configured(self) -> bool:
        raise NotImplementedError

    async def list_models(self) -> list[str]:
        raise NotImplementedError

    def stream_turn(self, *, model: str, system: str, messages: list[dict[str, Any]],
                    tools: list[ToolSpec], temperature: float | None = None) -> AsyncIterator[dict[str, Any]]:
        """Yield {"type": "text", "text": ...} chunks, then exactly one
        {"type": "turn", "result": TurnResult}. Raise ProviderError on failure."""
        raise NotImplementedError


def error_from_status(provider: str, status: int, detail: str, retry_after: float | None = None) -> ProviderError:
    label = provider
    detail = (detail or "").strip()[:300]
    if status in (401, 403):
        return ProviderError("auth", f"{label} rejected the credentials (HTTP {status}). The API key may be wrong, revoked, or expired — update it in Settings → Integrations.", status=status, provider=provider)
    if status == 404:
        return ProviderError("not_found", f"{label} couldn't find that model or endpoint (HTTP 404). Pick an available model in AI Core. {detail}", status=status, provider=provider)
    if status == 429:
        wait = f" Try again in about {int(retry_after)}s." if retry_after else ""
        return ProviderError("rate_limit", f"{label} rate limit or quota reached (HTTP 429).{wait} {detail}", status=status, retry_after=retry_after, provider=provider)
    if status >= 500:
        return ProviderError("server", f"{label} is having problems (HTTP {status}). This is on their side; retry shortly.", status=status, provider=provider)
    return ProviderError("bad_request", f"{label} rejected the request (HTTP {status}): {detail}", status=status, provider=provider)
