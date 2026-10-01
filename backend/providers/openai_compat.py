"""Providers that speak the OpenAI Chat Completions wire format:
Groq, OpenAI, Google Gemini (OpenAI-compatible endpoint), OpenRouter, Ollama."""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any, AsyncIterator

import httpx

from .. import config
from ..services import settings
from .base import RETRYABLE, Provider, ProviderError, ToolCall, ToolSpec, TurnResult, error_from_status

NON_CHAT_MODEL_HINTS = ("whisper", "tts", "playai", "guard", "embed", "orpheus", "distil")


class ThinkFilter:
    """Removes <think>…</think> sections some open models emit inline, even when
    a tag is split across stream chunks."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.buf = ""
        self.inside = False

    def feed(self, chunk: str) -> str:
        self.buf += chunk
        out: list[str] = []
        while True:
            if self.inside:
                i = self.buf.find(self.CLOSE)
                if i == -1:
                    self.buf = self.buf[-(len(self.CLOSE) - 1):]
                    break
                self.buf = self.buf[i + len(self.CLOSE):].lstrip("\n")
                self.inside = False
            else:
                i = self.buf.find(self.OPEN)
                if i == -1:
                    keep = 0
                    for k in range(1, len(self.OPEN)):
                        if self.buf.endswith(self.OPEN[:k]):
                            keep = k
                    out.append(self.buf[: len(self.buf) - keep])
                    self.buf = self.buf[len(self.buf) - keep:]
                    break
                out.append(self.buf[:i])
                self.buf = self.buf[i + len(self.OPEN):]
                self.inside = True
        return "".join(out)

    def flush(self) -> str:
        rest = "" if self.inside else self.buf
        self.buf = ""
        return rest


def to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": json.dumps(tc.get("arguments") or {})}}
                    for tc in m["tool_calls"]
                ]
                if not msg["content"]:
                    msg["content"] = None
            out.append(msg)
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
    return out


def _retry_after(headers: httpx.Headers) -> float | None:
    value = headers.get("retry-after")
    try:
        return float(value) if value else None
    except ValueError:
        return None


class OpenAICompatProvider(Provider):
    def __init__(self, pid: str, label: str, base_url: str | None, key_env: str | None,
                 extra_headers: dict[str, str] | None = None):
        self.id = pid
        self.label = label
        self._base_url = base_url
        self.needs_key = key_env
        self.extra_headers = extra_headers or {}

    # -- configuration -------------------------------------------------
    @property
    def base_url(self) -> str:
        if self.id == "ollama":
            return (settings.get("providers.ollama.base_url") or "http://localhost:11434/v1").rstrip("/")
        return (self._base_url or "").rstrip("/")

    def configured(self) -> bool:
        return self.needs_key is None or bool(config.get_secret(self.needs_key))

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", **self.extra_headers}
        if self.needs_key:
            key = config.get_secret(self.needs_key)
            if not key:
                raise ProviderError("setup", f"{self.label} is not set up yet: add {self.needs_key} in Settings → Integrations.", provider=self.id)
            h["Authorization"] = f"Bearer {key}"
        return h

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0))

    def _error(self, status: int, body: str, headers: httpx.Headers) -> ProviderError:
        detail, code = body, ""
        try:
            err = json.loads(body).get("error", {})
            if isinstance(err, dict):
                detail, code = err.get("message", body), err.get("code") or ""
            elif isinstance(err, str):
                detail = err
        except (ValueError, AttributeError):
            pass
        if code == "tool_use_failed":
            return ProviderError("tool_format", f"{self.label}'s model produced a malformed tool call. Try again, or choose a model with stronger tool use in AI Core.", status=status, provider=self.id)
        return error_from_status(self.label, status, str(detail), _retry_after(headers))

    # -- API -------------------------------------------------------------
    async def list_models(self) -> list[str]:
        try:
            async with self._client() as client:
                r = await client.get(f"{self.base_url}/models", headers=self._headers())
        except httpx.TimeoutException as e:
            raise ProviderError("connection", f"{self.label} timed out while listing models.", provider=self.id) from e
        except httpx.TransportError as e:
            hint = " Is Ollama running?" if self.id == "ollama" else " Check your internet connection."
            raise ProviderError("connection", f"Couldn't reach {self.label}.{hint}", provider=self.id) from e
        if r.status_code != 200:
            raise self._error(r.status_code, r.text, r.headers)
        data = r.json().get("data", [])
        ids = sorted({m.get("id") for m in data if isinstance(m, dict) and m.get("id")})
        return [i for i in ids if not any(h in i.lower() for h in NON_CHAT_MODEL_HINTS)]

    async def stream_turn(self, *, model: str, system: str, messages: list[dict[str, Any]],
                          tools: list[ToolSpec], temperature: float | None = None) -> AsyncIterator[dict[str, Any]]:
        if not model:
            raise ProviderError("setup", f"No model selected for {self.label}. Choose one in AI Core.", provider=self.id)
        body: dict[str, Any] = {"model": model, "messages": to_openai_messages(system, messages), "stream": True}
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools]
            body["tool_choice"] = "auto"
        url = f"{self.base_url}/chat/completions"
        headers = self._headers()

        attempt = 0
        started = False
        while True:
            try:
                async for event in self._stream_once(url, headers, body):
                    if event["type"] == "text":
                        started = True
                    yield event
                return
            except ProviderError as e:
                retryable = e.kind in RETRYABLE or (e.kind == "tool_format" and attempt == 0)
                if started or not retryable or attempt >= 2:
                    raise
                attempt += 1
                await asyncio.sleep(min(e.retry_after or 2 ** attempt, 20))

    async def _stream_once(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish = "stop"
        usage: dict[str, Any] = {}
        think = ThinkFilter()
        try:
            async with self._client() as client:
                async with client.stream("POST", url, json=body, headers=headers) as resp:
                    if resp.status_code != 200:
                        raw = (await resp.aread()).decode(errors="replace")
                        raise self._error(resp.status_code, raw, resp.headers)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        if chunk.get("error"):
                            err = chunk["error"]
                            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                            raise ProviderError("server", f"{self.label} stopped mid-response: {msg}", provider=self.id)
                        usage = chunk.get("usage") or (chunk.get("x_groq") or {}).get("usage") or usage
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            piece = delta.get("content")
                            if piece:
                                visible = think.feed(piece)
                                if visible:
                                    text_parts.append(visible)
                                    yield {"type": "text", "text": visible}
                            for tc in delta.get("tool_calls") or []:
                                slot = calls.setdefault(tc.get("index", len(calls)), {"id": "", "name": "", "args": ""})
                                slot["id"] = tc.get("id") or slot["id"]
                                fn = tc.get("function") or {}
                                slot["name"] += fn.get("name") or ""
                                slot["args"] += fn.get("arguments") or ""
                            if choice.get("finish_reason"):
                                finish = choice["finish_reason"]
        except httpx.TimeoutException as e:
            raise ProviderError("connection", f"{self.label} took too long to respond (timeout).", provider=self.id) from e
        except httpx.TransportError as e:
            hint = " Is Ollama running?" if self.id == "ollama" else " Check your internet connection."
            raise ProviderError("connection", f"Lost connection to {self.label}.{hint}", provider=self.id) from e

        tail = think.flush()
        if tail:
            text_parts.append(tail)
            yield {"type": "text", "text": tail}

        tool_calls: list[ToolCall] = []
        for _, slot in sorted(calls.items()):
            raw_args = slot["args"] or "{}"
            try:
                args = json.loads(raw_args)
                error = None if isinstance(args, dict) else "arguments were not a JSON object"
                args = args if isinstance(args, dict) else None
            except ValueError:
                args, error = None, "arguments were not valid JSON"
            call_id = re.sub(r"[^A-Za-z0-9_-]", "_", slot["id"]) or f"call_{uuid.uuid4().hex[:12]}"
            tool_calls.append(ToolCall(id=call_id, name=slot["name"], arguments=args, raw_arguments=raw_args, parse_error=error))
        if finish == "length":
            tool_calls = []  # a truncated tool call must never run
        result = TurnResult(
            text="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else ("length" if finish == "length" else "stop"),
            usage=usage if isinstance(usage, dict) else {},
            model=body["model"],
        )
        yield {"type": "turn", "result": result}
