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
from . import agents, approvals, connections, persona, routing, settings
from .routing import FALLBACK_KINDS  # noqa: F401  (re-exported for older imports)

log = logging.getLogger("ev.chat")
MAX_HISTORY_MESSAGES = 80
MAX_TOOL_RESULT_CHARS = 12000


@dataclass
class Run:
    id: str
    conversation_id: int
    agent: str
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    started: float = field(default_factory=time.time)
    state: dict[str, Any] = field(default_factory=dict)   # per-reply counters shared with tools


RUNS: dict[str, Run] = {}


def cancel(run_id: str) -> bool:
    run = RUNS.get(run_id)
    if not run:
        return False
    run.cancel.set()
    return True


def active_agents() -> set[str]:
    out = {r.agent for r in RUNS.values()}
    for r in RUNS.values():
        out |= set(r.state.get("_active_specialists", ()))
    return out


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
def _route_agent(agent: agents.Agent) -> str:
    """Modes (Coding, Research…) are E.V. herself, so they use E.V.'s connection."""
    return agent.id if agent.kind == "specialist" else "ev"


def _tool_names(agent: agents.Agent, max_level: Level | None = None) -> list[str]:
    """The tools an agent may see. Enforced in code from agents.py — prompts can't change it."""
    if agent.tools is not None:
        names = agent.tools
    else:  # 'every tool' still excludes tools reserved for specialists
        names = sorted(n for n, t in TOOLS.items() if "specialist_only" not in t.tags)
    disabled = registry.disabled_tools()
    cap = agent.max_level if max_level is None else max_level
    if agent.max_level is not None and max_level is not None and registry.LEVEL_RANK[agent.max_level] < registry.LEVEL_RANK[max_level]:
        cap = agent.max_level
    out = []
    for n in names:
        t = TOOLS.get(n)
        if not t or n in disabled:
            continue
        if cap is not None and registry.LEVEL_RANK[t.level] > registry.LEVEL_RANK[cap]:
            continue
        if "orchestrator_only" in t.tags and agent.kind == "specialist":
            continue
        out.append(n)
    return out


def _tool_content(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + f'… [truncated {len(text) - MAX_TOOL_RESULT_CHARS} chars]"'
    return text


async def _one_turn(run_cancel: asyncio.Event | None, provider: providers.Provider, model: str, system: str,
                    messages: list[dict[str, Any]], specs: list, on_text, temperature: float | None = None) -> TurnResult | None:
    """Consume one streamed model call. Returns None if cancelled."""
    stream = provider.stream_turn(model=model, system=system, messages=messages, tools=specs, temperature=temperature)
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

    try:
        attempts = routing.Attempts(routing.chain_for(_route_agent(agent)))
        route = attempts.first()
        provider, model = route.provider(), route.model
    except ProviderError as e:
        _save(conversation_id, "assistant", e.message, status="error", meta={"error": e.as_dict()})
        yield {"type": "error", **e.as_dict()}
        return
    run = Run(id=uuid.uuid4().hex, conversation_id=conversation_id, agent=agent.id)
    RUNS[run.id] = run
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
            while True:
                turn_task = asyncio.ensure_future(_one_turn(run.cancel, provider, model, system, history(conversation_id), specs,
                                                            on_text, connections.temperature(route.connection)))
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
                    break
                except ProviderError as e:
                    # Fallback only before anything was streamed, and only for the first model call of a reply.
                    nxt = attempts.after_failure(route, e, streamed=bool(partial) or step > 0)
                    if nxt is None:
                        raise
                    try:
                        nxt_provider = nxt.provider()
                    except ProviderError:
                        raise e from None
                    yield {"type": "notice", "message": f"{provider.label} unavailable ({e.message[:120]}). Using {nxt_provider.label} for this reply."}
                    route, provider, model = nxt, nxt_provider, nxt.model
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
                async for ev in _handle_tool(run, call, allowed=set(tool_names)):
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


async def _handle_tool(run: Run, call: ToolCall, allowed: set[str] | None = None) -> AsyncIterator[dict[str, Any]]:
    progress: asyncio.Queue = asyncio.Queue()
    ctx = ToolContext(actor="assistant", conversation_id=run.conversation_id, agent="ev", depth=0,
                      cancel=run.cancel, emit=progress.put_nowait, run_state=run.state)
    t = TOOLS.get(call.name)
    yield {"type": "tool_start", "id": call.id, "name": call.name, "args": call.arguments or {},
           "level": t.level.value if t else "unknown"}
    approval_id: str | None = None
    try:
        if allowed is not None and call.name not in allowed:
            # The model named a tool this agent wasn't given. Refuse in code, whatever the prompt said.
            registry.audit(actor=ctx.actor, tool_name=call.name, level=t.level.value if t else "unknown",
                           args=call.arguments, status="denied", detail="not in this agent's tool list",
                           conversation_id=run.conversation_id)
            raise ToolError("denied", f"'{call.name}' isn't available in this mode.")
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
        # Run the tool while relaying its progress (e.g. a specialist's steps) and watching Stop.
        task = asyncio.ensure_future(registry.run(prepared, ctx, approval_id=approval_id))
        try:
            while not task.done():
                try:
                    yield await asyncio.wait_for(progress.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    if run.cancel.is_set():
                        task.cancel()
                        break
            while not progress.empty():
                yield progress.get_nowait()
            if task.cancelled() or run.cancel.is_set() and not task.done():
                raise asyncio.CancelledError
            result = task.result()
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
            registry.audit(actor=ctx.actor, tool_name=call.name, level=prepared.tool.level.value,
                           args=prepared.args.model_dump(mode="json"), status="cancelled",
                           detail="stopped by the user", conversation_id=run.conversation_id)
            msg = "Stopped by the user while running; any partial work was discarded."
            _save(run.conversation_id, "tool", _tool_content({"status": "cancelled", "message": msg}),
                  tool_call_id=call.id, name=call.name)
            yield {"type": "tool_result", "id": call.id, "name": call.name, "status": "cancelled", "summary": msg}
            return
        if approval_id:
            approvals.finish(approval_id, "executed", result)
        _save(run.conversation_id, "tool", _tool_content({"status": "ok", "result": result}), tool_call_id=call.id, name=call.name)
        yield {"type": "tool_result", "id": call.id, "name": call.name, "status": "ok", "summary": _short(result),
               **({"report": _report_card(result)} if isinstance(result, dict) and result.get("kind") == "specialist_report" else {})}
    except ToolError as e:
        if approval_id:
            approvals.finish(approval_id, "failed", e.message)
        _save(run.conversation_id, "tool", _tool_content({"status": e.status, "error": e.message}), tool_call_id=call.id, name=call.name)
        yield {"type": "tool_result", "id": call.id, "name": call.name, "status": e.status, "summary": e.message}


def _report_card(result: dict[str, Any]) -> dict[str, Any]:
    """The small piece of a specialist report the interface shows on the tool card."""
    rep = result.get("report") or {}
    return {"agent": result.get("agent"), "agent_name": result.get("agent_name"), "status": result.get("status"),
            "data_status": result.get("data_status"), "data_notice": result.get("data_notice"),
            "summary": str(rep.get("summary", ""))[:600], "confidence": rep.get("confidence"),
            "sources": [{"source": p.get("source"), "as_of": p.get("as_of"), "is_live": p.get("is_live")}
                        for p in (result.get("provenance") or [])][:8]}


def _short(result: Any) -> str:
    if isinstance(result, dict) and result.get("kind") == "specialist_report":
        return f"{result.get('agent_name')} report · {result.get('status')} · data: {result.get('data_status')}"[:160]
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


# ------------------------------------------------------------------ non-interactive loops
@dataclass
class LoopResult:
    text: str
    status: str                      # completed | step_limit | cancelled
    steps: int
    tool_trace: list[dict[str, Any]]
    connection: str
    model: str


async def run_loop(*, agent_route: str, system: str, messages: list[dict[str, Any]], tool_names: list[str],
                   ctx: ToolContext, max_steps: int, on_event=None) -> LoopResult:
    """A tool-using model loop with no approvals possible (background jobs, specialists).

    Any tool that would need the user's approval is refused. Cancellation is checked
    between and during model calls and between tool calls."""
    specs = [TOOLS[n].spec() for n in tool_names]
    attempts = routing.Attempts(routing.chain_for(agent_route))
    route = attempts.first()
    provider = route.provider()
    trace: list[dict[str, Any]] = []
    texts: list[str] = []

    async def collect(chunk: str) -> None:
        texts.append(chunk)

    for step in range(max_steps):
        if ctx.cancel is not None and ctx.cancel.is_set():
            return LoopResult("", "cancelled", step, trace, route.connection, route.model)
        while True:
            texts.clear()
            try:
                result = await _one_turn(ctx.cancel, provider, route.model, system, messages, specs, collect,
                                         connections.temperature(route.connection))
                break
            except ProviderError as e:
                nxt = attempts.after_failure(route, e, streamed=False)  # nothing reaches the user mid-loop
                if nxt is None:
                    raise
                if on_event:
                    on_event({"type": "fallback", "from": provider.label, "reason": e.message[:120]})
                route, provider = nxt, nxt.provider()
        if result is None:
            return LoopResult("", "cancelled", step, trace, route.connection, route.model)
        messages.append({"role": "assistant", "content": result.text, "tool_calls": [c.as_dict() for c in result.tool_calls],
                         "raw": result.raw, "provider": provider.id, "model": result.model or route.model})
        if not result.tool_calls:
            return LoopResult(result.text.strip(), "completed", step + 1, trace, route.connection, route.model)
        for call in result.tool_calls:
            if ctx.cancel is not None and ctx.cancel.is_set():
                return LoopResult("", "cancelled", step + 1, trace, route.connection, route.model)
            if on_event:
                on_event({"type": "tool", "name": call.name, "args": call.arguments or {}})
            entry: dict[str, Any] = {"tool": call.name, "args": call.arguments or {}}
            try:
                if call.name not in tool_names:  # the model named a tool it was never given
                    raise ToolError("denied", f"'{call.name}' isn't available to this agent.")
                prepared = registry.prepare(call.name, call.arguments, ctx, parse_error=call.parse_error)
                if prepared.needs_approval:
                    raise ToolError("denied", "Needs the user's approval, which isn't possible here. Propose it instead.")
                res = await registry.run(prepared, ctx)
                payload = {"status": "ok", "result": res}
                entry.update(status="ok", result=res)
            except ToolError as e:
                payload = {"status": e.status, "error": e.message}
                entry.update(status=e.status, error=e.message)
            trace.append(entry)
            if on_event:
                on_event({"type": "tool_done", "name": call.name, "status": entry["status"]})
            messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": _tool_content(payload)})
    return LoopResult("", "step_limit", max_steps, trace, route.connection, route.model)


async def run_headless(prompt: str, *, label: str = "", use_tools: bool = True) -> str:
    agent = agents.get("ev")
    names = [n for n in _tool_names(agent, max_level=Level.PREPARE) if not registry.needs_approval(TOOLS[n])
             and "orchestrator_only" not in TOOLS[n].tags] if use_tools else []
    system = persona.system_prompt("ev", names) + (
        "\n\n## Background run\nYou are running as a scheduled workflow"
        + (f" named “{label}”" if label else "") + ". Nobody is available to approve actions, so only read and prepare. "
        "Write the result as a short, self-contained note.")
    messages: list[dict[str, Any]] = [{"role": "user", "content": f"{persona.turn_context('ev')}\n\n{prompt}"}]
    ctx = ToolContext(actor="job", max_level=Level.PREPARE)
    out = await run_loop(agent_route="ev", system=system, messages=messages, tool_names=names, ctx=ctx, max_steps=5)
    if out.status == "step_limit":
        return "Stopped after 5 steps without a final answer."
    return out.text or "(no output)"
