from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import db
from ..services import agents, approvals, chat

router = APIRouter(prefix="/api", tags=["chat"])


class NewConversation(BaseModel):
    agent: str = "ev"


class RenameConversation(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)


class SendMessage(BaseModel):
    text: str = Field(..., min_length=1, max_length=20000)
    agent: str | None = None


class Decision(BaseModel):
    decision: str = Field(..., pattern="^(approve|deny)$")


def _specialist_ids() -> list[str]:
    return [a.id for a in agents.specialists()]


@router.get("/conversations")
def list_conversations(limit: int = 50, agent: str | None = None):
    """Without `agent`: E.V.'s history (her modes included). Specialists' own conversations
    are listed only when asked for by agent id, e.g. ?agent=friday."""
    base = ("SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id AND m.role = 'user') AS turns "
            "FROM conversations c ")
    if agent:
        if agent not in _specialist_ids():
            raise HTTPException(404, "Unknown specialist.")
        return db.rows(base + "WHERE c.agent = ? ORDER BY updated_at DESC LIMIT ?", (agent, min(limit, 500)))
    spec = _specialist_ids()
    marks = ",".join("?" * len(spec)) or "''"
    return db.rows(base + f"WHERE c.agent NOT IN ({marks}) ORDER BY updated_at DESC LIMIT ?", (*spec, min(limit, 500)))


@router.post("/conversations")
def create_conversation(body: NewConversation):
    # A specialist's id creates a conversation owned by that specialist (its workspace chat).
    if body.agent in _specialist_ids():
        return chat.create_conversation(body.agent, title="New conversation")
    return chat.create_conversation(agents.get(body.agent).id)


@router.get("/conversations/{cid}")
def get_conversation(cid: int):
    conv = db.row("SELECT * FROM conversations WHERE id = ?", (cid,))
    if not conv:
        raise HTTPException(404, "Conversation not found")
    out = []
    for m in db.rows("SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (cid,)):
        item = {"id": m["id"], "role": m["role"], "content": m["content"], "status": m["status"],
                "created_at": m["created_at"], "provider": m["provider"], "model": m["model"]}
        if m["role"] == "assistant":
            item["tool_calls"] = db.loads(m["tool_calls"], []) or []
        if m["role"] == "tool":
            payload = db.loads(m["content"], {}) or {}
            item.update(name=m["name"], tool_status=payload.get("status", "ok"),
                        summary=payload.get("error") or payload.get("message") or chat._short(payload.get("result")))
            item["content"] = ""
        out.append(item)
    conv["messages"] = out
    return conv


@router.patch("/conversations/{cid}")
def rename_conversation(cid: int, body: RenameConversation):
    if not db.execute("UPDATE conversations SET title = ? WHERE id = ?", (body.title.strip(), cid)):
        raise HTTPException(404, "Conversation not found")
    return {"ok": True}


@router.delete("/conversations/{cid}")
def delete_conversation(cid: int):
    if any(r.conversation_id == cid for r in chat.RUNS.values()):
        raise HTTPException(409, "Stop the running reply before deleting this conversation.")
    db.execute("DELETE FROM messages WHERE conversation_id = ?", (cid,))
    db.execute("DELETE FROM conversations WHERE id = ?", (cid,))
    return {"ok": True}


@router.post("/conversations/{cid}/messages")
async def send_message(cid: int, body: SendMessage):
    if not db.row("SELECT id FROM conversations WHERE id = ?", (cid,)):
        raise HTTPException(404, "Conversation not found")
    if any(r.conversation_id == cid for r in chat.RUNS.values()):
        raise HTTPException(409, "E.V. is still replying in this conversation. Stop it or wait.")

    async def events():
        async for ev in chat.stream_chat(cid, body.text.strip(), body.agent):
            yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# async on purpose: these touch asyncio objects, so they must run on the event loop thread.
@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    return {"cancelled": chat.cancel(run_id)}


@router.get("/approvals/pending")
def pending_approvals():
    return approvals.list_pending()


@router.post("/approvals/{aid}")
async def decide(aid: str, body: Decision):
    try:
        return approvals.decide(aid, body.decision == "approve")
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
