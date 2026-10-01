"""Provider wire-format tests against mocked HTTP — no network, no real keys."""
import asyncio
import json

import anthropic
import httpx
import httpx2
import pytest

from backend import config
from backend.providers.anthropic_provider import AnthropicProvider
from backend.providers.base import ProviderError, ToolSpec
from backend.providers.openai_compat import OpenAICompatProvider, ThinkFilter


def sse(*chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def make_groq(monkeypatch, handler):
    monkeypatch.setitem(config._SECRETS, "GROQ_API_KEY", "test-key-123456")
    p = OpenAICompatProvider("groq", "Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY")
    monkeypatch.setattr(p, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return p


async def drain(gen):
    texts, result = [], None
    async for ev in gen:
        if ev["type"] == "text":
            texts.append(ev["text"])
        else:
            result = ev["result"]
    return "".join(texts), result


TOOL = ToolSpec("create_task", "Add a task", {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]})


def test_openai_compat_streams_text_and_split_tool_calls(monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        body = sse(
            {"choices": [{"delta": {"content": "Sure, "}}]},
            {"choices": [{"delta": {"content": "<think>hidden</think>adding it."}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_a1", "function": {"name": "create_task", "arguments": "{\"ti"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "tle\": \"Pay rent\"}"}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    p = make_groq(monkeypatch, handler)
    text, result = asyncio.run(drain(p.stream_turn(model="openai/gpt-oss-120b", system="sys",
                                                   messages=[{"role": "user", "content": "hi"}], tools=[TOOL])))
    assert text == "Sure, adding it."
    assert result.tool_calls[0].name == "create_task" and result.tool_calls[0].arguments == {"title": "Pay rent"}
    assert result.finish_reason == "tool_calls"
    assert seen["auth"] == "Bearer test-key-123456"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["body"]["tools"][0]["function"]["name"] == "create_task"


def test_openai_compat_retries_rate_limit_then_succeeds(monkeypatch):
    attempts = {"n": 0}

    def handler(req):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"retry-after": "0"})
        return httpx.Response(200, text=sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}))

    p = make_groq(monkeypatch, handler)
    text, _ = asyncio.run(drain(p.stream_turn(model="m", system="s", messages=[], tools=[])))
    assert text == "ok" and attempts["n"] == 2


def test_openai_compat_auth_error_is_readable(monkeypatch):
    p = make_groq(monkeypatch, lambda req: httpx.Response(401, json={"error": {"message": "Invalid API Key"}}))
    with pytest.raises(ProviderError) as e:
        asyncio.run(drain(p.stream_turn(model="m", system="s", messages=[], tools=[])))
    assert e.value.kind == "auth" and "Settings" in e.value.message


def test_openai_compat_missing_key_says_setup_required(monkeypatch):
    monkeypatch.delitem(config._SECRETS, "GROQ_API_KEY", raising=False)
    p = OpenAICompatProvider("groq", "Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY")
    with pytest.raises(ProviderError) as e:
        asyncio.run(drain(p.stream_turn(model="m", system="s", messages=[], tools=[])))
    assert e.value.kind == "setup"


def test_openai_compat_truncated_tool_call_never_runs(monkeypatch):
    body = sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c", "function": {"name": "create_task", "arguments": "{\"title\": \"Pa"}}]}, "finish_reason": "length"}]})
    p = make_groq(monkeypatch, lambda req: httpx.Response(200, text=body))
    _, result = asyncio.run(drain(p.stream_turn(model="m", system="s", messages=[], tools=[TOOL])))
    assert result.tool_calls == [] and result.finish_reason == "length"


def test_think_filter_handles_split_tags():
    f = ThinkFilter()
    out = f.feed("Hel") + f.feed("lo <thi") + f.feed("nk>secret</th") + f.feed("ink>world") + f.flush()
    assert out == "Hello world"


# ---------------------------------------------------------------- Anthropic (official SDK, mocked HTTP)
def anthropic_sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def test_anthropic_request_shape_and_tool_parsing(monkeypatch):
    monkeypatch.setitem(config._SECRETS, "ANTHROPIC_API_KEY", "sk-test-not-real")
    captured = {}

    def handler(req: httpx2.Request):
        captured["body"] = json.loads(req.content)
        captured["beta"] = req.headers.get("anthropic-beta", "")
        events = [
            {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                                                  "content": [], "stop_reason": None, "stop_sequence": None,
                                                  "usage": {"input_tokens": 10, "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "On it."}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_01", "name": "create_task", "input": {}}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"title\": \"Pay rent\"}"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 20}},
            {"type": "message_stop"},
        ]
        return httpx2.Response(200, text=anthropic_sse(events), headers={"content-type": "text/event-stream"})

    p = AnthropicProvider()
    monkeypatch.setattr(p, "_client", lambda: anthropic.AsyncAnthropic(
        api_key="sk-test-not-real", max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))))
    history = [
        {"role": "user", "content": "add a task"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call.x", "name": "list_tasks", "arguments": {}}], "provider": "groq"},
        {"role": "tool", "tool_call_id": "call.x", "name": "list_tasks", "content": "{}"},
    ]
    text, result = asyncio.run(drain(p.stream_turn(model="claude-opus-5-5", system="sys", messages=history, tools=[TOOL])))
    body = captured["body"]
    assert text == "On it."
    assert result.tool_calls[0].arguments == {"title": "Pay rent"} and result.finish_reason == "tool_calls"
    assert result.raw[1]["type"] == "tool_use"  # replayable content blocks
    assert body["fallbacks"] == "default"
    assert body["thinking"] == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert body["output_config"] == {"effort": "medium"}
    assert body["tools"][0]["eager_input_streaming"] is True
    assert "temperature" not in body
    assert "server-side-fallback-2026-07-01" in captured["beta"] and "thinking-binding-controls-2026-08-01" in captured["beta"]
    # tool ids from another provider are sanitised, results grouped in one user turn
    assert body["messages"][1]["content"][0]["id"] == "call_x"
    assert body["messages"][2]["content"][0]["type"] == "tool_result"


def test_anthropic_refusal_becomes_readable_error(monkeypatch):
    monkeypatch.setitem(config._SECRETS, "ANTHROPIC_API_KEY", "sk-test-not-real")

    def handler(req):
        events = [
            {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                                                  "content": [], "stop_reason": None, "stop_sequence": None,
                                                  "usage": {"input_tokens": 1, "output_tokens": 0}}},
            {"type": "message_delta", "delta": {"stop_reason": "refusal", "stop_sequence": None}, "usage": {"output_tokens": 0}},
            {"type": "message_stop"},
        ]
        return httpx2.Response(200, text=anthropic_sse(events), headers={"content-type": "text/event-stream"})

    p = AnthropicProvider()
    monkeypatch.setattr(p, "_client", lambda: anthropic.AsyncAnthropic(
        api_key="x", max_retries=0, http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))))
    with pytest.raises(ProviderError) as e:
        asyncio.run(drain(p.stream_turn(model="claude-opus-5-5", system="s", messages=[{"role": "user", "content": "x"}], tools=[])))
    assert e.value.kind == "refusal"


def test_anthropic_401_maps_to_auth(monkeypatch):
    p = AnthropicProvider()
    monkeypatch.setitem(config._SECRETS, "ANTHROPIC_API_KEY", "sk-test-not-real")
    handler = lambda req: httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})  # noqa: E731
    monkeypatch.setattr(p, "_client", lambda: anthropic.AsyncAnthropic(
        api_key="x", max_retries=0, http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))))
    with pytest.raises(ProviderError) as e:
        asyncio.run(drain(p.stream_turn(model="claude-haiku-4-5", system="s", messages=[{"role": "user", "content": "x"}], tools=[])))
    assert e.value.kind == "auth"
    assert config.get_secret("ANTHROPIC_API_KEY") == "sk-test-not-real"
