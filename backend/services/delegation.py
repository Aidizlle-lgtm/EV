"""Hub-and-spoke delegation: E.V. hands a task to a specialist and gets a report back.

Enforced here, in code:
  * Only E.V.'s own interactive turn may delegate (depth 0). Specialists can't
    delegate (their tool lists exclude this tool, and the depth check refuses it).
    Background workflows can't delegate either.
  * At most agents.MAX_DELEGATIONS_PER_REPLY delegations per reply.
  * The specialist runs through services/specialists.run_task — the same code path
    as its own workspace — with ITS tool list, capped at ITS max action level (READ
    for F.R.I.D.A.Y.), a step limit, and a wall-clock timeout. Anything needing
    approval is refused inside a specialist — it can only propose actions.
  * Stop cancels the specialist mid-run.
  * Data provenance in the report is collected from the actual tool results by code.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..tools.registry import Level, ToolContext, ToolError, tool
from . import agents, specialists
from .specialists import Report, collect_provenance, parse_report  # noqa: F401  (re-exported)


class DelegateArgs(BaseModel):
    specialist: str = Field(..., max_length=30, description="Specialist id, e.g. 'friday'")
    task: str = Field(..., min_length=5, max_length=2000,
                      description="What you need, stated completely (the specialist can't see this conversation)")
    context: str = Field("", max_length=4000, description="Relevant facts from the conversation the specialist needs")


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
    run_specs = ctx.run_state.setdefault("_active_specialists", set())
    run_specs.add(spec.id)
    try:
        return await specialists.run_task(spec, task=a.task, context=a.context, requested_by="ev",
                                          conversation_id=ctx.conversation_id, cancel=ctx.cancel, emit=ctx.emit)
    finally:
        run_specs.discard(spec.id)
