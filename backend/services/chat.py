"""The conversation engine: streams model output, runs tools through the permission
gate, pauses for approvals, and supports cancellation at any point."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .. import db, providers
from ..providers.base import ProviderError, ToolCall, TurnResult
from ..tools import registry
from ..tools.registry import TOOLS, Level, ToolContext, ToolError
from . import agents, approvals, persona, settings

log = logging.getLogger("ev.chat")
MAX_HISTORY_MESSAGES = 80
MAX_TOOL_RESULT_CHARS = 12000
FALLBACK_KINDS = {"connection", "server", "rate_limit", "auth", "setup", "not_found"}


@dataclass
class Run:
    id: str
    conversation_id: int
    agent: str
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    started: float = field(default_factory=time.time)


RUNS: dict[str, Run] = {}


def cancel(run_id: str) -> bool:
    run = RUNS.get(run_id)
    if not run:
        return False
    run.cancel.set()
    return True


def active_agents() -> set[str]:
    return {r.agent for r in RUNS.values()}


# ------------------------------------------------------------------ storage
def create_conversation(agent: str = "ev", title: str = "New conversation") -> dict[str, Any]:
    now = db.now_iso()
    cid = db.execute("INSERT INTO conversations(title, agent, created_at, updated_at) VALUES(?,?,?,?)",
                     (title, agent, now, now))
    return db.row("SELECT * FROM conversations WHERE id = ?", (cid,))  # type: ignore[return-value]


def _save(conversation_id: int, role: str, content: str, **kw: Any) -> int:
    return db.execute(
        "INSERT INTO messages(conversation_id, role, content, tool_calls, tool_call_id, name, provider, model, raw, meta, status, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (conversation_id, role, content,
         db.dumps(kw["tool_calls"]) if kw.get("tool_calls") else None,
         kw.get("tool_call_id"), kw.get("name"), kw.get("provider"), kw.get("model"),
         db.dumps(kw["raw"]) if kw.get("raw") is not None else None,
         db.dumps(kw["meta"]) if kw.get("meta") else None,
         kw.get("status", "complete"), db.now_iso()),
    )


def history(conversation_id: int) -> list[dict[str, Any]]:
    """Canonical history for the model. Deterministic, so replays are byte-identical."""
    rows = db.rows("SELECT * FROM messages WHERE conversation_id = ? AND status != 'error' ORDER BY id", (conversation_id,))
    out: list[dict[str, Any]] = []
    open_calls: dict[str, str] = {}
    for m in rows:
        if m["role"] != "tool" and open_calls:  # repair: every tool call needs a result
            for cid, name in open_calls.items():
                out.append({"role": "tool", "tool_call_id": cid, "name": name, "content": '{"status":"cancelled"}'})
            open_calls = {}
        if m["role"] == "user":
            meta = db.loads(m["meta"], {})
            content = f"{meta['context']}\n\n{m['content']}" if meta.get("context") else m["content"]
            out.append({"role": "user", "content": content})
        elif m["role"] == "assistant":
            calls = db.loads(m["tool_calls"], []) or []
            out.append({"role": "assistant", "content": m["content"], "tool_calls": calls,
                        "raw": db.loads(m["raw"]), "provider": m["provider"], "model": m["model"]})
            open_calls = {c["id"]: c["name"] for c in calls}
        elif m["role"] == "tool":
            open_calls.pop(m["tool_call_id"], None)
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "name": m["name"], "content": m["content"]})
    for cid, name in open_calls.items():
        out.append({"role": "tool", "tool_call_id": cid, "name": name, "content": '{"status":"cancelled"}'})
    if len(out) > MAX_HISTORY_MESSAGES:
        start = len(out) - MAX_HISTORY_MESSAGES
        while start < len(out) and out[start]["role"] != "user":
            start += 1
        out = out[start:]
    return out


# ------------------------------------------------------------------ helpers
def _provider_chain() -> list[tuple[providers.Provider, str]]:
    primary = settings.get("llm.provider") or "groq"
    chain = [(providers.get(primary), settings.model_for(primary))]
    fb = settings.get("llm.fallback_provider")
    if fb and fb != primary and fb in providers.all_providers():
        chain.append((providers.get(fb), settings.model_for(fb)))
    return chain


def _tool_names(agent: agents.Agent, max_level: Level | None = None) -> list[str]:
    names = agent.tools if agent.tools is not None else sorted(TOOLS)
    disabled = registry.disabled_tools()
    out = []
    for n in names:
        t = TOOLS.get(n)
        if not t or n in disabled:
            continue
        if max_level is not None and registry.LEVEL_RANK[t.level] > registry.LEVEL_RANK[max_level]:
            continue
        out.append(n)
    return out


def _tool_content(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + f'… [truncated {len(text) - MAX_TOOL_RESULT_CHARS} chars]"'
    return text


async def _one_turn(run_cancel: asyncio.Event | None, provider: providers.Provider, model: str, system: str,
                    messages: list[dict[str, Any]], specs: list, on_text) -> TurnResult | None:
    """Consume one streamed model call. Returns None if cancelled."""
    temperature = settings.get("llm.temperature")
    stream = provider.stream_turn(model=model, system=system, messages=messages, tools=specs,
                                  temperature=float(temperature) if temperature is not None else None)
    result: TurnResult | None = None
    try:
        async for ev in stream:
            if run_cancel is not None and run_cancel.is_set():
                return None
            if ev["type"] == "text":
                await on_text(ev["text"])
            elif ev["type"] == "turn":
                result = ev["result"]
    finally:
        await stream.aclose()
    if result is None:
        raise ProviderError("server", f"{provider.label} ended the response unexpectedly.", provider=provider.id)
    return result


# ------------------------------------------------------------------ interactive chat
async def stream_chat(conversation_id: int, text: str, agent_id: str | None = None) -> AsyncIterator[dict[str, Any]]:
    conv = db.row("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
    if not conv:
        yield {"type": "error", "kind": "not_found", "message": "Conversation not found."}
        return
    agent = agents.get(agent_id or conv["agent"])
    tool_names = _tool_names(agent)
    specs = [TOOLS[n].spec() for n in tool_names]
    system = persona.system_prompt(agent.id, tool_names)
    _save(conversation_id, "user", text, meta={"context": persona.turn_context(agent.id), "agent": agent.id})
    if conv["title"] == "New conversation":
        title = " ".join(text.split())[:60] or "New conversation"
        db.execute("UPDATE conversations SET title = ?, agent = ? WHERE id = ?", (title, agent.id, conversation_id))
    db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (db.now_iso(), conversation_id))

    run = Run(id=uuid.uuid4().hex, conversation_id=conversation_id, agent=agent.id)
    RUNS[run.id] = run
    chain = _provider_chain()
    provider, model = chain[0]
    yield {"type": "run", "run_id": run.id, "conversation_id": conversation_id, "provider": provider.id,
           "provider_label": provider.label, "model": model, "agent": agent.id}

    max_steps = int(settings.get("llm.max_steps") or 8)
    partial: list[str] = []
    pending: dict[str, str] = {}  # tool calls without a saved result yet
    queue: asyncio.Queue = asyncio.Queue()
    finished = False

    async def on_text(chunk: str) -> None:
        partial.append(chunk)
        await queue.put({"type": "delta", "text": chunk})

    try:
        for step in range(max_steps):
            partial.clear()
            result: TurnResult | None = None
            attempt_chain = list(chain) if step == 0 else [(provider, model)]
            for idx, (prov, mdl) in enumerate(attempt_chain):
                turn_task = asyncio.ensure_future(_one_turn(run.cancel, prov, mdl, system, history(conversation_id), specs, on_text))
                try:
                    while not turn_task.done():
                        try:
                            ev = await asyncio.wait_for(queue.get(), timeout=0.25)
                            yield ev
                        except asyncio.TimeoutError:
                            if run.cancel.is_set():
                                turn_task.cancel()
                    while not queue.empty():
                        yield queue.get_nowait()
                    result = turn_task.result() if not turn_task.cancelled() else None
                    provider, model = prov, mdl
                    break
                except ProviderError as e:
                    can_fallback = idx + 1 < len(attempt_chain) and not partial and e.kind in FALLBACK_KINDS
                    if not can_fallback:
                        raise
                    nxt = attempt_chain[idx + 1][0]
                    yield {"type": "notice", "message": f"{prov.label} unavailable ({e.message[:120]}). Using {nxt.label} for this reply."}
                finally:
                    if not turn_task.done():
                        turn_task.cancel()

            if run.cancel.is_set() or result is None:
                if partial:
                    _save(conversation_id, "assistant", "".join(partial) + "\n\n[Reply stopped by the user]",
                          provider=provider.id, model=model, status="cancelled")
                yield {"type": "cancelled"}
                finished = True
                return

            calls = [c for c in result.tool_calls]
            _save(conversation_id, "assistant", result.text, tool_calls=[c.as_dict() for c in calls] or None,
                  raw=result.raw, provider=provider.id, model=result.model or model)
            partial.clear()
            if result.finish_reason == "length":
                yield {"type": "notice", "message": "The reply hit the model's length limit and was cut off."}
            if not calls:
                yield {"type": "done", "usage": result.usage}
                finished = True
                return

            pending = {c.id: c.name for c in calls}
            for call in calls:
                async for ev in _handle_tool(run, call):
                    yield ev
                pending.pop(call.id, None)
                if run.cancel.is_set():
                    for cid, name in pending.items():
                        _save(conversation_id, "tool", '{"status":"cancelled","message":"Stopped by the user before running."}',
                              tool_call_id=cid, name=name)
                    pending = {}
                    yield {"type": "cancelled"}
                    finished = True
                    return
            yield {"type": "step", "step": step + 1}
        yield {"type": "notice", "message": f"Stopped after {max_steps} tool steps to stay safe. Ask me to continue if needed."}
        yield {"type": "done"}
        finished = True
    except ProviderError as e:
        log.info("provider error: %s", e.message)
        if partial:
            _save(conversation_id, "assistant", "".join(partial), provider=provider.id, model=model, status="error")
        _save(conversation_id, "assistant", e.message, provider=provider.id, model=model, status="error", meta={"error": e.as_dict()})
        yield {"type": "error", **e.as_dict()}
        finished = True
    finally:
        RUNS.pop(run.id, None)
        if not finished:  # client disconnected or server shutting down mid-run
            run.cancel.set()
            if partial:
                _save(conversation_id, "assistant", "".join(partial) + "\n\n[Reply interrupted]",
                      provider=provider.id, model=model, status="cancelled")
            for cid, name in pending.items():
                _save(conversation_id, "tool", '{"status":"cancelled","message":"Interrupted before running."}',
                      tool_call_id=cid, name=name)


async def _handle_tool(run: Run, call: ToolCall) -> AsyncIterator[dict[str, Any]]:
    ctx = ToolContext(actor="assistant", conversation_id=run.conversation_id)
    t = TOOLS.get(call.name)
    yield {"type": "tool_start", "id": call.id, "name": call.name, "args": call.arguments or {},
           "level": t.level.value if t else "unknown"}
    approval_id: str | None = None
    try:
        prepared = registry.prepare(call.name, call.arguments, ctx, parse_error=call.parse_error)
        if prepared.needs_approval:
            appr = approvals.create(prepared, ctx)
            approval_id = appr["id"]
            yield {"type": "approval_required", "approval": appr}
            deadline = time.time() + float(settings.get("permissions.approval_timeout_s") or 600)
            decision = None
            while decision is None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    approvals.expire(approval_id)
                    decision = "expired"
                    break
                decision = await approvals.wait(approval_id, min(15.0, remaining), run.cancel)
                if decision is None:
                    yield {"type": "ping"}
            yield {"type": "approval_resolved", "approval_id": approval_id, "decision": decision}
            if decision != "approved":
                message = {"denied": "The user denied this action. Do not retry it unless they ask.",
                           "expired": "No approval was given in time, so nothing was done.",
                           "cancelled": "The user stopped the conversation; nothing was done."}[decision]
                _save(run.conversation_id, "tool", _tool_content({"status": decision, "message": message}),
                      tool_call_id=call.id, name=call.name)
                yield {"type": "tool_result", "id": call.id, "name": call.name, "status": decision, "summary": message}
                return
        result = await registry.run(prepared, ctx, approval_id=approval_id)
        if approval_id:
            approvals.finish(approval_id, "executed", result)
        _save(run.conversation_id, "tool", _tool_content({"status": "ok", "result": result}), tool_call_id=call.id, name=call.name)
        yield {"type": "tool_result", "id": call.id, "name": call.name, "status": "ok", "summary": _short(result)}
    except ToolError as e:
        if approval_id:
            approvals.finish(approval_id, "failed", e.message)
        _save(run.conversation_id, "tool", _tool_content({"status": e.status, "error": e.message}), tool_call_id=call.id, name=call.name)
        yield {"type": "tool_result", "id": call.id, "name": call.name, "status": e.status, "summary": e.message}


def _short(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("created", "updated", "deleted", "saved", "draft", "workflow"):
            v = result.get(key)
            if isinstance(v, dict):
                label = v.get("title") or v.get("content") or v.get("name") or v.get("subject") or ""
                return f"{key} #{v.get('id', '')} {label}"[:160]
        if "sent" in result:
            times = f" ×{result['times']}" if result.get("times", 1) > 1 else ""
            return f"pressed {str(result['sent']).replace('_', ' ')}{times}"
        if "opened" in result:
            return f"opened {result['opened']}"
        for key, v in result.items():
            if isinstance(v, list):
                return f"{len(v)} {key}"
        scalars = [f"{k}: {v}" for k, v in result.items() if isinstance(v, (str, int, float, bool)) and k != "note"]
        if scalars:
            return " · ".join(scalars[:3])[:160]
    return str(result)[:160]


# ------------------------------------------------------------------ background (no approvals possible)
async def run_headless(prompt: str, *, label: str = "", use_tools: bool = True) -> str:
    agent = agents.get("ev")
    names = [n for n in _tool_names(agent, max_level=Level.PREPARE) if not registry.needs_approval(TOOLS[n])] if use_tools else []
    specs = [TOOLS[n].spec() for n in names]
    system = persona.system_prompt("ev", names) + (
        "\n\n## Background run\nYou are running as a scheduled workflow"
        + (f" named “{label}”" if label else "") + ". Nobody is available to approve actions, so only read and prepare. "
        "Write the result as a short, self-contained note.")
    messages: list[dict[str, Any]] = [{"role": "user", "content": f"{persona.turn_context('ev')}\n\n{prompt}"}]
    ctx = ToolContext(actor="job", max_level=Level.PREPARE)
    chain = _provider_chain()
    texts: list[str] = []

    async def collect(chunk: str) -> None:
        texts.append(chunk)

    last_error: ProviderError | None = None
    for prov, mdl in chain:
        try:
            for _ in range(5):
                texts.clear()
                result = await _one_turn(None, prov, mdl, system, messages, specs, collect)
                assert result is not None
                messages.append({"role": "assistant", "content": result.text, "tool_calls": [c.as_dict() for c in result.tool_calls],
                                 "raw": result.raw, "provider": prov.id, "model": result.model or mdl})
                if not result.tool_calls:
                    return result.text.strip() or "(no output)"
                for call in result.tool_calls:
                    try:
                        prepared = registry.prepare(call.name, call.arguments, ctx, parse_error=call.parse_error)
                        if prepared.needs_approval:
                            raise ToolError("denied", "Needs approval, which isn't possible in a background run.")
                        payload = {"status": "ok", "result": await registry.run(prepared, ctx)}
                    except ToolError as e:
                        payload = {"status": e.status, "error": e.message}
                    messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": _tool_content(payload)})
            return "Stopped after 5 steps without a final answer."
        except ProviderError as e:
            last_error = e
            if e.kind not in FALLBACK_KINDS:
                break
    raise last_error or ProviderError("setup", "No AI provider is configured.")
