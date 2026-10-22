"""Hub-and-spoke delegation: E.V. hands a task to a specialist and gets a report back.

Enforced here, in code:
  * Only E.V.'s own interactive turn may delegate (depth 0). Specialists can't
    delegate (their tool lists exclude this tool, and the depth check refuses it).
    Background workflows can't delegate either.
  * At most agents.MAX_DELEGATIONS_PER_REPLY delegations per reply.
  * The specialist runs with ITS tool list, capped at ITS max action level (READ for
    F.R.I.D.A.Y.), a step limit, and a wall-clock timeout. Anything needing approval
    is refused inside a specialist — it can only propose actions.
  * Stop cancels the specialist mid-run.
  * The specialist's memories are filtered by its memory policy.
  * Data provenance in the report is collected from the actual tool results by code,
    and overrides whatever the specialist claims about its data.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from ..tools.registry import Level, ToolContext, ToolError, tool
from . import agents, chat, persona, routing


class DelegateArgs(BaseModel):
    specialist: str = Field(..., max_length=30, description="Specialist id, e.g. 'friday'")
    task: str = Field(..., min_length=5, max_length=2000,
                      description="What you need, stated completely (the specialist can't see this conversation)")
    context: str = Field("", max_length=4000, description="Relevant facts from the conversation the specialist needs")


# ------------------------------------------------------------------ report model
class Finding(BaseModel):
    point: str = Field("", max_length=1000)
    basis: Literal["tool_data", "model_knowledge", "inference"] = "inference"
    source_ref: str | None = Field(None, max_length=200)


class DataUsed(BaseModel):
    description: str = Field("", max_length=300)
    source: str = Field("", max_length=300)
    as_of: str | None = Field(None, max_length=60)


class ProposedAction(BaseModel):
    action: str = Field("", max_length=400)
    reason: str = Field("", max_length=400)


class Report(BaseModel):
    summary: str = Field("", max_length=4000)
    findings: list[Finding] = Field(default_factory=list, max_length=20)
    data_used: list[DataUsed] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=15)
    risks: list[str] = Field(default_factory=list, max_length=15)
    confidence: Literal["low", "medium", "high"] = "low"
    open_questions: list[str] = Field(default_factory=list, max_length=10)
    proposed_actions: list[ProposedAction] = Field(default_factory=list, max_length=10)


def _candidates(text: str) -> list[str]:
    out = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)]
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        out.append(text[first:last + 1])
    return out


def parse_report(text: str) -> tuple[Report, bool]:
    """(report, format_ok). A report that isn't valid JSON is kept as a plain summary."""
    for cand in _candidates(text or ""):
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        for key in ("findings", "data_used", "proposed_actions"):   # drop malformed list items rather than everything
            if isinstance(obj.get(key), list):
                obj[key] = [x for x in obj[key] if isinstance(x, dict)][:20]
        for key in ("assumptions", "risks", "open_questions"):
            if isinstance(obj.get(key), list):
                obj[key] = [str(x)[:500] for x in obj[key]][:15]
        if isinstance(obj.get("confidence"), str):
            obj["confidence"] = obj["confidence"].strip().lower()
        if isinstance(obj.get("summary"), str):
            obj["summary"] = obj["summary"][:4000]
        try:
            return Report.model_validate(obj), True
        except ValidationError:
            continue
    return Report(summary=(text or "").strip()[:4000] or "(The specialist returned no report.)"), False


# ------------------------------------------------------------------ provenance (code, not model)
def collect_provenance(trace: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str, str]:
    """Returns (provenance list, data_status, data_notice) from real tool results."""
    seen: dict[tuple, dict[str, Any]] = {}
    for entry in trace:
        res = entry.get("result")
        if entry.get("status") != "ok" or not isinstance(res, dict) or not isinstance(res.get("provenance"), dict):
            continue
        p = res["provenance"]
        key = (p.get("source_id"), entry.get("tool"), res.get("symbol"))
        seen[key] = {"tool": entry.get("tool"), "symbol": res.get("symbol"), "source": p.get("source"),
                     "source_id": p.get("source_id"), "data_status": p.get("data_status"),
                     "is_live": bool(p.get("is_live")), "as_of": p.get("as_of"), "retrieved_at": p.get("retrieved_at")}
    prov = list(seen.values())
    if not prov:
        return [], "NONE", ("No market data was retrieved for this report. Anything it says about prices or company "
                            "figures comes from the AI model's general knowledge, which may be out of date. It is not live data.")
    statuses = {p["data_status"] for p in prov}
    if "TEST_DATA" in statuses:
        return prov, "TEST_DATA", ("TEST DATA: the market figures in this report are synthetic numbers generated by E.V. for "
                                   "testing. They are not real prices, company data, or news. No live market-data source is connected.")
    if all(p["is_live"] for p in prov):
        when = ", ".join(sorted({f"{p['source']} as of {p['as_of']}" for p in prov})[:4])
        return prov, "LIVE", f"Market data from {when}."
    return prov, "MIXED", "Some market figures are delayed or end-of-day rather than live; check each source's timestamp."


# ------------------------------------------------------------------ the tool
def _describe() -> str:
    roster = "; ".join(f"'{a.id}' = {a.name} ({a.domain})" for a in agents.specialists())
    return ("Hand a focused task to a specialist agent and receive its structured report. The specialist works only "
            f"through its own read-only tools and can't see this conversation, so include the needed context. Specialists: {roster}")


@tool("delegate_to_specialist", level=Level.READ, args=DelegateArgs, category="agents",
      tags={"orchestrator_only"}, timeout=max([a.timeout_s for a in agents.specialists()] + [60.0]) + 15,
      description=_describe(),
      summarize=lambda a: f"Ask {a.specialist}: {a.task[:120]}")
async def delegate_to_specialist(a: DelegateArgs, ctx: ToolContext) -> dict[str, Any]:
    if ctx.depth >= agents.MAX_DELEGATION_DEPTH or ctx.actor != "assistant" or ctx.agent != "ev":
        raise ToolError("denied", "Only E.V.'s own reply can delegate; specialists and background workflows can't.")
    spec = agents.get_any(a.specialist.strip().lower())
    if spec is None or spec.kind != "specialist":
        names = ", ".join(s.id for s in agents.specialists())
        raise ToolError("invalid", f"Unknown specialist '{a.specialist}'. Available: {names}.")
    count = int(ctx.run_state.get("delegations", 0))
    if count >= agents.MAX_DELEGATIONS_PER_REPLY:
        raise ToolError("denied", f"Delegation limit reached ({agents.MAX_DELEGATIONS_PER_REPLY} per reply). "
                                  "Answer with what you have.")
    ctx.run_state["delegations"] = count + 1

    def emit(ev: dict[str, Any]) -> None:
        """Relay specialist progress to the interface as agent_progress events.
        The specialist loop's own event kind (tool, tool_done, fallback) arrives as 'event'."""
        if ctx.emit:
            body = {("event" if k == "type" else k): v for k, v in ev.items()}
            ctx.emit({"type": "agent_progress", "agent": spec.id, "agent_name": spec.name, **body})

    run_specs = ctx.run_state.setdefault("_active_specialists", set())
    run_specs.add(spec.id)
    started = time.monotonic()
    emit({"stage": "started", "task": a.task[:300]})
    try:
        tool_names = chat._tool_names(spec)
        sctx = ToolContext(actor=f"agent:{spec.id}", conversation_id=ctx.conversation_id, max_level=spec.max_level or Level.READ,
                           agent=spec.id, depth=ctx.depth + 1, cancel=ctx.cancel, emit=None, run_state={})
        system = persona.specialist_system_prompt(spec, tool_names)
        body = f"{persona.specialist_context(spec)}\n\nTask from E.V.:\n{a.task}"
        if a.context.strip():
            body += f"\n\nContext from E.V.:\n{a.context.strip()}"
        messages: list[dict[str, Any]] = [{"role": "user", "content": body}]
        try:
            out = await asyncio.wait_for(
                chat.run_loop(agent_route=spec.id, system=system, messages=messages, tool_names=tool_names, ctx=sctx,
                              max_steps=spec.max_steps, on_event=emit),
                timeout=spec.timeout_s)
        except asyncio.TimeoutError:
            emit({"stage": "timeout"})
            raise ToolError("timeout", f"{spec.name} didn't finish within {int(spec.timeout_s)}s. Nothing was changed.")
        except Exception as e:  # provider errors etc. become a readable tool error for E.V.
            from ..providers.base import ProviderError
            if isinstance(e, ProviderError):
                emit({"stage": "error", "message": e.message[:200]})
                raise ToolError("unavailable", f"{spec.name} couldn't reach its AI model: {e.message}") from e
            raise
    finally:
        run_specs.discard(spec.id)

    if out.status == "cancelled":
        emit({"stage": "cancelled"})
        raise ToolError("cancelled", f"{spec.name} was stopped by the user.")
    report, format_ok = parse_report(out.text)
    provenance, data_status, data_notice = collect_provenance(out.tool_trace)
    status = {"completed": "completed", "step_limit": "incomplete"}[out.status]
    emit({"stage": "finished", "status": status, "data_status": data_status})
    try:
        conn_label = routing.Route(out.connection, out.model, "").label()
    except Exception:
        conn_label = out.connection
    return {
        "kind": "specialist_report",
        "agent": spec.id, "agent_name": spec.name, "status": status,
        "status_detail": None if status == "completed" else f"Stopped at the {spec.max_steps}-step limit before finishing.",
        "report": report.model_dump(), "report_format_ok": format_ok,
        "data_status": data_status, "data_notice": data_notice, "provenance": provenance,
        "tools_used": [{"tool": t["tool"], "args": t.get("args"), "status": t.get("status")} for t in out.tool_trace],
        "model": {"connection": conn_label, "model": out.model}, "steps": out.steps,
        "duration_s": round(time.monotonic() - started, 1),
    }
