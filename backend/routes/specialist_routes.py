"""API for a specialist's own workspace (F.R.I.D.A.Y. first).

Everything here reports real backend state: the assigned connection and whether it
has been checked, whether the agent is working right now, the market-data source and
its labels, and the (not yet connected) voice. Direct chat uses the normal
conversation endpoints with a conversation owned by the specialist
(POST /api/conversations {"agent": "friday"}).
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from ..integrations import agent_voice, market_data
from ..providers.base import ProviderError
from ..services import agents, chat, prompts, routing, settings, specialists, status
from ..tools.market import HistoryArgs, NewsArgs, SymbolArgs, market_company_overview, market_news, market_price_history, market_quote
from ..tools.registry import ToolContext, ToolError

router = APIRouter(prefix="/api", tags=["specialists"])


def _spec(aid: str) -> agents.Agent:
    spec = agents.get_any(aid)
    if spec is None or spec.kind != "specialist":
        raise HTTPException(404, "Unknown specialist.")
    return spec


# ------------------------------------------------------------------ status (all real)
def model_state(aid: str) -> dict[str, Any]:
    chain = routing.chain_for(aid)
    first = chain[0]
    try:
        prov = first.provider()
        configured, label = prov.configured(), prov.label
    except ProviderError as e:
        configured, label = False, first.connection
        return {"state": "setup_required", "detail": e.message, "connection": first.connection, "label": label, "model": first.model}
    if not first.model:
        return {"state": "setup_required", "detail": f"No model chosen for {label}.", "connection": first.connection,
                "label": label, "model": ""}
    st = status.state(f"llm:{first.connection}", configured, f"{label} needs an API key.")
    cool = routing.cooling(first.family)
    if cool:
        st = {"status": "cooling_down", "detail": f"Rate-limited — waiting {cool}s before using {label} again."}
    return {"state": st["status"], "detail": st["detail"], "checked_at": st.get("checked_at"),
            "connection": first.connection, "label": label, "model": first.model,
            "fallbacks": [{"label": r.label(), "model": r.model} for r in chain[1:]]}


@router.get("/agents/{aid}/status")
def agent_status(aid: str):
    spec = _spec(aid)
    return {"agent": spec.id, "name": spec.name, "working": spec.id in chat.active_agents(),
            "model": model_state(spec.id), "inherits_ev_model": routing.assignment(spec.id)["connection"] == "",
            "prompt": prompts.load(spec.prompt_file).public() if spec.prompt_file else None,
            "market_data": market_data.status(), "voice": agent_voice.profile(spec.id),
            "limits": {"max_level": spec.max_level.value if spec.max_level else None, "max_steps": spec.max_steps,
                       "timeout_s": spec.timeout_s},
            "tools": spec.tools, "paper_trading": {"available": False, "detail": "Paper trading isn't built yet."}}


@router.get("/agents/{aid}/voice")
def agent_voice_profile(aid: str):
    if aid != "ev":
        _spec(aid)
    return agent_voice.profile(aid)


# ------------------------------------------------------------------ reports
@router.get("/agents/{aid}/reports")
def reports(aid: str, symbol: str | None = None, limit: int = 20):
    _spec(aid)
    return specialists.list_reports(aid, symbol, limit)


@router.get("/agents/{aid}/reports/{rid}")
def report(aid: str, rid: int):
    _spec(aid)
    r = specialists.get_report(aid, rid)
    if r is None:
        raise HTTPException(404, "Report not found.")
    return r


class AnalyzeIn(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=10)
    question: str = Field("", max_length=1000)


@router.post("/agents/{aid}/analyze")
async def analyze(aid: str, body: AnalyzeIn):
    """Streams a report run (same runner as E.V.'s delegation). Stop: POST /api/runs/{run_id}/cancel."""
    spec = _spec(aid)
    try:
        symbol = SymbolArgs(symbol=body.symbol).symbol
    except ValidationError as e:
        raise HTTPException(400, "Use a ticker symbol like MSFT or BRK.B.") from e
    if any(r.agent == spec.id and r.state.get("analysis") for r in chat.RUNS.values()):
        raise HTTPException(409, f"{spec.name} is already running an analysis. Stop it or wait.")
    task = (f"Analyze {symbol}: business, recent price action, valuation, bull/base/bear scenarios with explicit "
            "assumptions, key risks, and a verification-focused action plan.")
    if body.question.strip():
        task += f"\nThe user also asks: {body.question.strip()}"

    run = chat.Run(id=uuid.uuid4().hex, conversation_id=0, agent=spec.id)
    run.state["analysis"] = True
    chat.RUNS[run.id] = run
    queue: asyncio.Queue = asyncio.Queue()

    async def events():
        job = asyncio.ensure_future(specialists.run_task(spec, task=task, requested_by="user", conversation_id=None,
                                                         cancel=run.cancel, emit=queue.put_nowait))
        try:
            yield _sse({"type": "run", "run_id": run.id, "agent": spec.id, "symbol": symbol})
            while not job.done():
                try:
                    yield _sse(await asyncio.wait_for(queue.get(), timeout=0.25))
                except asyncio.TimeoutError:
                    if run.cancel.is_set():
                        job.cancel()
                        break
            while not queue.empty():
                yield _sse(queue.get_nowait())
            if job.cancelled() or (run.cancel.is_set() and not job.done()):
                yield _sse({"type": "cancelled"})
                return
            try:
                result = job.result()
            except ToolError as e:
                yield _sse({"type": "cancelled"} if e.status == "cancelled" else {"type": "error", "kind": e.status, "message": e.message})
                return
            yield _sse({"type": "report", "report": result})
            yield _sse({"type": "done"})
        finally:
            if not job.done():
                job.cancel()
            chat.RUNS.pop(run.id, None)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _sse(ev: dict[str, Any]) -> str:
    return f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"


# ------------------------------------------------------------------ market data for the workspace (labelled)
_MARKET = {"quote": (SymbolArgs, market_quote), "overview": (SymbolArgs, market_company_overview),
           "history": (HistoryArgs, market_price_history), "news": (NewsArgs, market_news)}


@router.get("/market/{kind}/{symbol}")
async def market(kind: str, symbol: str, range: str = "1m", limit: int = 5):
    """The same read-only tools F.R.I.D.A.Y. uses, called directly for the workspace's chart, cards, and watchlist.
    Every response carries the code-built provenance (source, as_of, retrieved_at, data_status)."""
    if kind not in _MARKET:
        raise HTTPException(404, "Unknown market-data request.")
    issue = market_data.setup_issue()
    if issue:
        raise HTTPException(409, issue)
    model, handler = _MARKET[kind]
    raw = {"symbol": symbol, **({"range": range} if kind == "history" else {}), **({"limit": limit} if kind == "news" else {})}
    try:
        args = model.model_validate(raw)
    except ValidationError as e:
        raise HTTPException(400, "Invalid symbol or option (symbols look like MSFT; ranges are 1m, 3m, 6m, 1y, 5y).") from e
    try:
        return await handler(args, ToolContext(actor="user", agent="user"))
    except ToolError as e:
        raise HTTPException(409, e.message) from e


class WatchlistIn(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=30)


@router.get("/market/watchlist")
def get_watchlist():
    return {"symbols": settings.get("market.watchlist") or []}


@router.put("/market/watchlist")
def put_watchlist(body: WatchlistIn):
    try:
        symbols = list(dict.fromkeys(SymbolArgs(symbol=s).symbol for s in body.symbols))
        settings.put("market.watchlist", symbols)
    except (ValidationError, ValueError) as e:
        raise HTTPException(400, "Watchlist entries must be ticker symbols like MSFT or BRK.B (up to 30).") from e
    return {"symbols": symbols}

