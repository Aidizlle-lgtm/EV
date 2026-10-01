"""Anthropic Claude via the official `anthropic` Python SDK (Messages API, streaming).

Notes that matter for correctness on current Claude models:
* Assistant turns are replayed exactly as returned (`raw` content blocks, including
  thinking blocks) so the history stays append-only.
* `thinking.block_binding.prefix_mismatch_behavior = "drop_block"` makes the API drop
  a stale thinking block instead of failing if the history prefix ever changes
  (for example after you edit your personality settings).
* Server-side `fallbacks: "default"` lets the API retry a safety-classifier decline on
  Anthropic's recommended fallback model instead of failing the turn.
* Tools stream their input eagerly, so E.V. validates every tool input itself
  (it already does, with Pydantic) before running anything.
"""
from __future__ import annotations

import re
from typing import Any, AsyncIterator

import anthropic

from .. import config
from ..services import settings
from .base import Provider, ProviderError, ToolCall, ToolSpec, TurnResult, error_from_status

ADAPTIVE_THINKING_PREFIXES = (
    "claude-opus-5", "claude-fable-5", "claude-mythos-5", "claude-sonnet-5",
    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
)
BLOCK_BINDING_MODELS = {"claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5-5"}
FALLBACK_DEFAULT_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-fable-5", "claude-sonnet-5-5"}
MAX_TOKENS = 64000


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", value or "") or "toolu_unknown"


def to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": [{"type": "text", "text": m["content"] or "(empty)"}]})
        elif role == "assistant":
            if m.get("provider") == "anthropic" and isinstance(m.get("raw"), list) and m["raw"]:
                out.append({"role": "assistant", "content": m["raw"]})
                continue
            blocks: list[dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": _safe_id(tc["id"]), "name": tc["name"], "input": tc.get("arguments") or {}})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no reply)"}]})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": _safe_id(m["tool_call_id"]), "content": m["content"] or "(empty)"}
            if m.get("is_error"):
                block["is_error"] = True
            prev = out[-1] if out else None
            if prev and prev["role"] == "user" and all(b.get("type") == "tool_result" for b in prev["content"]):
                prev["content"].append(block)  # all results of one turn go in ONE user message
            else:
                out.append({"role": "user", "content": [block]})
    return out


class AnthropicProvider(Provider):
    id = "anthropic"
    label = "Anthropic Claude"
    needs_key = "ANTHROPIC_API_KEY"

    def configured(self) -> bool:
        return bool(config.get_secret(self.needs_key))

    def _client(self) -> anthropic.AsyncAnthropic:
        key = config.get_secret(self.needs_key)
        if not key:
            raise ProviderError("setup", "Claude is not set up yet: add ANTHROPIC_API_KEY in Settings → Integrations.", provider=self.id)
        return anthropic.AsyncAnthropic(api_key=key, max_retries=2, timeout=anthropic.Timeout(600.0, connect=10.0))

    def _map_error(self, e: Exception) -> ProviderError:
        if isinstance(e, anthropic.APIConnectionError):  # includes APITimeoutError
            return ProviderError("connection", "Couldn't reach Anthropic. Check your internet connection.", provider=self.id)
        if isinstance(e, anthropic.APIStatusError):
            retry = None
            try:
                retry = float(e.response.headers.get("retry-after", "")) if e.response is not None else None
            except ValueError:
                retry = None
            return error_from_status("Anthropic", e.status_code, getattr(e, "message", str(e)), retry)
        return ProviderError("server", f"Unexpected Anthropic error: {e}", provider=self.id)

    async def list_models(self) -> list[str]:
        client = self._client()
        try:
            return sorted([m.id async for m in client.models.list(limit=100)])
        except anthropic.APIError as e:
            raise self._map_error(e) from e
        finally:
            await client.close()

    def build_request(self, *, model: str, system: str, messages: list[dict[str, Any]], tools: list[ToolSpec]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": to_anthropic_messages(messages),
        }
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters, "eager_input_streaming": True}
                for t in tools
            ]
        betas: list[str] = []
        if model in FALLBACK_DEFAULT_MODELS:
            betas.append("server-side-fallback-2026-07-01")
            kwargs["fallbacks"] = "default"
        if model.startswith(ADAPTIVE_THINKING_PREFIXES):
            thinking: dict[str, Any] = {"type": "adaptive"}
            if model in BLOCK_BINDING_MODELS:
                betas.append("thinking-binding-controls-2026-08-01")
                thinking["block_binding"] = {"prefix_mismatch_behavior": "drop_block"}
            kwargs["thinking"] = thinking
            kwargs["output_config"] = {"effort": settings.get("anthropic.effort") or "medium"}
        if betas:
            kwargs["betas"] = betas
        return kwargs

    async def stream_turn(self, *, model: str, system: str, messages: list[dict[str, Any]],
                          tools: list[ToolSpec], temperature: float | None = None) -> AsyncIterator[dict[str, Any]]:
        if not model:
            raise ProviderError("setup", "No Claude model selected. Choose one in AI Core.", provider=self.id)
        kwargs = self.build_request(model=model, system=system, messages=messages, tools=tools)
        client = self._client()
        text_parts: list[str] = []
        attempts = 0
        try:
            while True:
                try:
                    async with client.beta.messages.stream(**kwargs) as stream:
                        async for event in stream:
                            if event.type == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                                text_parts.append(event.delta.text)
                                yield {"type": "text", "text": event.delta.text}
                        final = await stream.get_final_message()
                    break
                except ValueError as e:
                    # Eagerly streamed tool input that could not be parsed at all.
                    attempts += 1
                    if text_parts or attempts > 2:
                        raise ProviderError("tool_format", "Claude produced a tool call E.V. couldn't parse. Please try again.", provider=self.id) from e
                except anthropic.APIError as e:
                    raise self._map_error(e) from e
        finally:
            await client.close()

        stop = final.stop_reason
        if stop == "refusal":
            details = getattr(final, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            extra = f" (category: {category})" if category else ""
            raise ProviderError("refusal", f"Claude declined this request{extra}. Try rephrasing, or switch provider in AI Core.", provider=self.id)

        raw = [b.to_dict() for b in final.content]
        tool_calls: list[ToolCall] = []
        for b in final.content:
            if b.type == "tool_use":
                args = b.input if isinstance(b.input, dict) else None
                tool_calls.append(ToolCall(id=b.id, name=b.name, arguments=args,
                                           parse_error=None if args is not None else "input was not a JSON object"))
        finish = "stop"
        if stop == "max_tokens":
            tool_calls, finish = [], "length"  # never run a truncated tool input
        elif tool_calls:
            finish = "tool_calls"
        usage = final.usage.to_dict() if getattr(final, "usage", None) else {}
        yield {"type": "turn", "result": TurnResult(text="".join(text_parts), tool_calls=tool_calls,
                                                     finish_reason=finish, raw=raw, usage=usage, model=final.model or model)}
