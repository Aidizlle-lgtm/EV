"""Pending approvals: the chat loop pauses on one of these until you decide."""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from .. import db
from ..tools import registry

_waiters: dict[str, asyncio.Future] = {}


def create(prepared: registry.Prepared, ctx: registry.ToolContext) -> dict[str, Any]:
    aid = uuid.uuid4().hex
    db.execute(
        "INSERT INTO approvals(id, conversation_id, tool, level, args, summary, status, created_at) VALUES(?,?,?,?,?,?, 'pending', ?)",
        (aid, ctx.conversation_id, prepared.tool.name, prepared.tool.level.value, prepared.args_json, prepared.summary, db.now_iso()),
    )
    _waiters[aid] = asyncio.get_running_loop().create_future()
    registry.audit(actor=ctx.actor, tool_name=prepared.tool.name, level=prepared.tool.level.value,
                   args=prepared.args.model_dump(mode="json"), status="awaiting_approval",
                   detail=prepared.summary, conversation_id=ctx.conversation_id)
    return public(get(aid))  # type: ignore[arg-type]


def get(aid: str) -> dict[str, Any] | None:
    return db.row("SELECT * FROM approvals WHERE id = ?", (aid,))


def public(rec: dict[str, Any]) -> dict[str, Any]:
    info = registry.LEVEL_INFO.get(rec["level"], {})
    return {"id": rec["id"], "tool": rec["tool"], "level": rec["level"], "level_label": info.get("label", rec["level"]),
            "summary": rec["summary"], "args": db.loads(rec["args"], {}), "status": rec["status"],
            "conversation_id": rec["conversation_id"], "created_at": rec["created_at"]}


def list_pending() -> list[dict[str, Any]]:
    return [public(r) for r in db.rows("SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at")]


def decide(aid: str, approve: bool) -> dict[str, Any]:
    rec = get(aid)
    if not rec:
        raise LookupError("Approval not found.")
    if rec["status"] != "pending":
        raise ValueError(f"This request was already {rec['status']}.")
    waiter = _waiters.get(aid)
    if waiter is None or waiter.done():
        _set(aid, "expired")
        raise ValueError("That request is no longer waiting (the conversation was closed or E.V. restarted).")
    status = "approved" if approve else "denied"
    _set(aid, status)
    registry.audit(actor="user", tool_name=rec["tool"], level=rec["level"], args=db.loads(rec["args"]),
                   status=f"user_{status}", detail=rec["summary"], conversation_id=rec["conversation_id"])
    loop = waiter.get_loop()
    try:
        same_thread = asyncio.get_running_loop() is loop
    except RuntimeError:
        same_thread = False
    if same_thread:
        waiter.set_result(status)
    else:  # called from a worker thread: hand the result to the loop safely
        loop.call_soon_threadsafe(lambda: waiter.done() or waiter.set_result(status))
    return public(get(aid))  # type: ignore[arg-type]


async def wait(aid: str, seconds: float, cancel: asyncio.Event) -> str | None:
    """Wait up to `seconds` for a decision. Returns approved/denied/cancelled, or None if still pending."""
    fut = _waiters[aid]
    cancel_task = asyncio.ensure_future(cancel.wait())
    try:
        await asyncio.wait({fut, cancel_task}, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        cancel_task.cancel()
    if fut.done():
        _waiters.pop(aid, None)
        return fut.result()
    if cancel.is_set():
        expire(aid)
        return "cancelled"
    return None


def expire(aid: str) -> None:
    fut = _waiters.pop(aid, None)
    if fut and not fut.done():
        fut.cancel()
    rec = get(aid)
    if rec and rec["status"] == "pending":
        _set(aid, "expired")


def finish(aid: str, status: str, result: Any) -> None:
    db.execute("UPDATE approvals SET status = ?, result = ? WHERE id = ?", (status, db.dumps(result)[:4000], aid))


def _set(aid: str, status: str) -> None:
    db.execute("UPDATE approvals SET status = ?, decided_at = ? WHERE id = ?", (status, db.now_iso(), aid))
