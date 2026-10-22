"""Running a specialist on a task and turning its answer into a checked report.

One code path serves every way a specialist produces a report:
  * E.V. delegates (services/delegation.py — the delegate_to_specialist tool)
  * you press "Analyze company" in the specialist's own workspace (routes/specialist_routes.py)

Both use the same tool list, action-level cap, memory filter, connection/model
routing, step limit, wall-clock timeout, and cancellation. Data provenance in the
report is collected by code from the actual tool results and overrides whatever
the model says about its data.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError

from .. import db
from ..providers.base import ProviderError
from ..tools.registry import Level, ToolContext, ToolError
from . import agents, chat, persona, routing


# ------------------------------------------------------------------ report model
class Finding(BaseModel):
    point: str = Field("", max_length=1000)
    basis: Literal["tool_data", "model_knowledge", "inference"] = "inference"
    source_ref: str | None = Field(None, max_length=200)


class DataUsed(BaseModel):
    description: str = Field("", max_length=300)
    source: str = Field("", max_length=300)
    as_of: str | None = Field(None, max_length=60)


class Scenario(BaseModel):
    case: Literal["bull", "base", "bear"]
    description: str = Field("", max_length=600)
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    # An assumption-driven illustration, never a forecast or a probability.
    illustrative_change_percent: float | None = Field(None, ge=-100, le=1000)


class ProposedAction(BaseModel):
    action: str = Field("", max_length=400)
    reason: str = Field("", max_length=400)


class Report(BaseModel):
    headline: str = Field("", max_length=300)
    summary: str = Field("", max_length=4000)
    findings: list[Finding] = Field(default_factory=list, max_length=20)
    data_used: list[DataUsed] = Field(default_factory=list, max_length=20)
    scenarios: list[Scenario] = Field(default_factory=list, max_length=3)
    assumptions: list[str] = Field(default_factory=list, max_length=15)
    risks: list[str] = Field(default_factory=list, max_length=15)
    action_plan: list[str] = Field(default_factory=list, max_length=10)
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
        if isinstance(obj.get("scenarios"), list):
            seen, scen = set(), []
            for x in obj["scenarios"]:
                if isinstance(x, dict) and str(x.get("case", "")).lower() in ("bull", "base", "bear"):
                    x = {**x, "case": str(x["case"]).lower()}
                    if x["case"] not in seen:
                        seen.add(x["case"])
                        if not isinstance(x.get("illustrative_change_percent"), (int, float)):
                            x["illustrative_change_percent"] = None
                        scen.append(x)
            obj["scenarios"] = scen
        for key in ("assumptions", "risks", "open_questions", "action_plan"):
            if isinstance(obj.get(key), list):
                obj[key] = [str(x)[:500] for x in obj[key]][:15]
        if isinstance(obj.get("confidence"), str):
            obj["confidence"] = obj["confidence"].strip().lower()
        for key, limit in (("summary", 4000), ("headline", 300)):
            if isinstance(obj.get(key), str):
                obj[key] = obj[key][:limit]
        try:
            return Report.model_validate(obj), True
        except ValidationError:
            continue
    return Report(summary=(text or "").strip()[:4000] or "(The specialist returned no report.)"), False


# ------------------------------------------------------------------ provenance (code, not model)
DATA_NOTICE_NONE = ("No market data was retrieved for this report. Anything it says about prices or company figures "
                    "comes from the AI model's general knowledge, which may be out of date. It is not live data.")
DATA_NOTICE_TEST = ("TEST DATA: the market figures in this report are synthetic numbers generated by E.V. for testing. "
                    "They are not real prices, company data, or news. No live market-data source is connected.")


def collect_provenance(trace: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str, str]:
    """Returns (provenance list, data_status, data_notice) from real tool results.

    data_status: NONE | TEST_DATA | LIVE | DELAYED | CACHED | END_OF_DAY | MIXED"""
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
        return [], "NONE", DATA_NOTICE_NONE
    statuses = {p["data_status"] for p in prov}
    if "TEST_DATA" in statuses:
        return prov, "TEST_DATA", DATA_NOTICE_TEST
    when = ", ".join(sorted({f"{p['source']} as of {p['as_of']}" for p in prov})[:4])
    if len(statuses) == 1:
        status = statuses.pop()
        label = {"LIVE": "Live market data", "DELAYED": "Delayed market data", "CACHED": "Cached market data",
                 "END_OF_DAY": "End-of-day market data"}.get(status, "Market data")
        return prov, status, f"{label} from {when}."
    return prov, "MIXED", f"Mixed data freshness (live, delayed, or cached) — check each timestamp: {when}."


# ------------------------------------------------------------------ the runner
async def run_task(spec: agents.Agent, *, task: str, context: str = "", requested_by: str,
                   conversation_id: int | None, cancel: asyncio.Event | None,
                   emit: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Run a specialist on one task and return a checked report.

    requested_by: "ev" (delegation) or "user" (Analyze in the specialist's workspace).
    Raises ToolError for timeout, cancellation, or an unreachable model."""

    def progress(ev: dict[str, Any]) -> None:
        if emit:
            body = {("event" if k == "type" else k): v for k, v in ev.items()}
            emit({"type": "agent_progress", "agent": spec.id, "agent_name": spec.name, **body})

    started = time.monotonic()
    progress({"stage": "started", "task": task[:300]})
    tool_names = chat._tool_names(spec)
    sctx = ToolContext(actor=f"agent:{spec.id}", conversation_id=conversation_id, max_level=spec.max_level or Level.READ,
                       agent=spec.id, depth=agents.MAX_DELEGATION_DEPTH, cancel=cancel, emit=None, run_state={})
    system = persona.specialist_system_prompt(spec, tool_names, mode="report", requested_by=requested_by)
    who = "E.V." if requested_by == "ev" else "the user (Analyze, in your workspace)"
    body = f"{persona.specialist_context(spec)}\n\nTask from {who}:\n{task}"
    if context.strip():
        body += f"\n\nContext from {who}:\n{context.strip()}"
    messages: list[dict[str, Any]] = [{"role": "user", "content": body}]
    try:
        out = await asyncio.wait_for(
            chat.run_loop(agent_route=spec.id, system=system, messages=messages, tool_names=tool_names, ctx=sctx,
                          max_steps=spec.max_steps, on_event=progress),
            timeout=spec.timeout_s)
    except asyncio.TimeoutError:
        progress({"stage": "timeout"})
        raise ToolError("timeout", f"{spec.name} didn't finish within {int(spec.timeout_s)}s. Nothing was changed.")
    except ProviderError as e:
        progress({"stage": "error", "message": e.message[:200]})
        raise ToolError("unavailable", f"{spec.name} couldn't reach its AI model: {e.message}") from e

    if out.status == "cancelled":
        progress({"stage": "cancelled"})
        raise ToolError("cancelled", f"{spec.name} was stopped by the user.")
    report, format_ok = parse_report(out.text)
    provenance, data_status, data_notice = collect_provenance(out.tool_trace)
    status = {"completed": "completed", "step_limit": "incomplete"}[out.status]
    progress({"stage": "finished", "status": status, "data_status": data_status})
    try:
        conn_label = routing.Route(out.connection, out.model, "").label()
    except Exception:
        conn_label = out.connection
    symbols = [t.get("args", {}).get("symbol") for t in out.tool_trace if isinstance(t.get("args"), dict) and t["args"].get("symbol")]
    result = {
        "kind": "specialist_report",
        "agent": spec.id, "agent_name": spec.name, "status": status, "requested_by": requested_by,
        "status_detail": None if status == "completed" else f"Stopped at the {spec.max_steps}-step limit before finishing.",
        "symbol": (str(symbols[0]).upper() if symbols else None),
        "report": report.model_dump(), "report_format_ok": format_ok,
        "data_status": data_status, "data_notice": data_notice, "provenance": provenance,
        "tools_used": [{"tool": t["tool"], "args": t.get("args"), "status": t.get("status")} for t in out.tool_trace],
        "model": {"connection": conn_label, "model": out.model}, "steps": out.steps,
        "duration_s": round(time.monotonic() - started, 1), "created_at": db.now_iso(),
    }
    result["report_id"] = save_report(result, task)
    return result


# ------------------------------------------------------------------ storage (shown in the specialist's workspace)
def save_report(result: dict[str, Any], task: str) -> int:
    return db.execute(
        "INSERT INTO agent_reports(agent, requested_by, symbol, task, status, data_status, payload, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (result["agent"], result["requested_by"], result.get("symbol"), task[:2000], result["status"],
         result["data_status"], db.dumps({k: v for k, v in result.items() if k != "report_id"}), result["created_at"]))


def list_reports(agent_id: str, symbol: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    sql = "SELECT id, agent, requested_by, symbol, task, status, data_status, created_at FROM agent_reports WHERE agent = ?"
    params: list[Any] = [agent_id]
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol.upper())
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 100)))
    return db.rows(sql, tuple(params))


def get_report(agent_id: str, report_id: int) -> dict[str, Any] | None:
    r = db.row("SELECT * FROM agent_reports WHERE id = ? AND agent = ?", (report_id, agent_id))
    if not r:
        return None
    payload = db.loads(r["payload"], {}) or {}
    return {**payload, "report_id": r["id"], "task": r["task"]}
