"""CRUD endpoints for things you manage directly. Your own clicks are explicit
actions, so they don't go through the AI approval flow — but they are audited."""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import db
from ..services import data, documents, files, jobs
from ..tools import registry

router = APIRouter(prefix="/api", tags=["data"])


def _audit(tool: str, level: str, args: Any, status: str = "ok", detail: str = "") -> None:
    registry.audit(actor="user", tool_name=tool, level=level, args=args, status=status, detail=detail)


def _guard(fn):
    try:
        return fn()
    except LookupError as e:
        raise HTTPException(404, str(e).strip("'\"")) from e
    except (ValueError, files.FileAccessError, documents.IngestError) as e:
        raise HTTPException(400, str(e).strip("'\"")) from e


# ------------------------------------------------------------- tasks
class TaskIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    notes: str = Field("", max_length=4000)
    priority: Literal["low", "normal", "high"] = "normal"
    due_at: Optional[str] = None


class TaskPatch(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    notes: Optional[str] = Field(None, max_length=4000)
    priority: Optional[Literal["low", "normal", "high"]] = None
    due_at: Optional[str] = None
    status: Optional[Literal["open", "done"]] = None


@router.get("/tasks")
def get_tasks(status: str = "all"):
    return data.list_tasks(None if status == "all" else status, limit=500)


@router.post("/tasks")
def post_task(body: TaskIn):
    t = _guard(lambda: data.create_task(body.title, body.notes, body.priority, body.due_at))
    _audit("create_task", "reversible_write", body.model_dump())
    return t


@router.patch("/tasks/{tid}")
def patch_task(tid: int, body: TaskPatch):
    t = _guard(lambda: data.update_task(tid, **body.model_dump(exclude_unset=True)))
    _audit("update_task", "reversible_write", {"task_id": tid, **body.model_dump(exclude_unset=True)})
    return t


@router.delete("/tasks/{tid}")
def del_task(tid: int):
    t = _guard(lambda: data.delete_task(tid))
    _audit("delete_task", "external", {"task_id": tid}, detail=t["title"])
    return {"ok": True}


# ------------------------------------------------------------- calendar
class EventIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    starts_at: str
    ends_at: Optional[str] = None
    location: str = Field("", max_length=200)
    notes: str = Field("", max_length=2000)


@router.get("/events")
def get_events(start: Optional[str] = None, end: Optional[str] = None):
    return _guard(lambda: data.list_events(start, end, limit=1000))


@router.post("/events")
def post_event(body: EventIn):
    e = _guard(lambda: data.create_event(body.title, body.starts_at, body.ends_at, body.location, body.notes))
    _audit("create_event", "reversible_write", body.model_dump())
    return e


@router.delete("/events/{eid}")
def del_event(eid: int):
    _guard(lambda: data.delete_event(eid))
    _audit("delete_event", "external", {"event_id": eid})
    return {"ok": True}


# ------------------------------------------------------------- memory
class MemoryIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=1000)
    category: str = Field("general", max_length=40)
    scope: str = Field("shared", max_length=30)   # 'shared' or a specialist id (e.g. 'friday')


class MemoryPatch(BaseModel):
    content: Optional[str] = Field(None, min_length=1, max_length=1000)
    category: Optional[str] = Field(None, max_length=40)
    scope: Optional[str] = Field(None, max_length=30)


class MemoryImport(BaseModel):
    memories: list[MemoryIn] = Field(..., max_length=2000)


@router.get("/memories")
def get_memories(q: Optional[str] = None):
    return data.list_memories(q)


@router.post("/memories")
def post_memory(body: MemoryIn):
    m = _guard(lambda: data.add_memory(body.content, body.category, source="user", scope=body.scope))
    _audit("save_memory", "reversible_write", body.model_dump())
    return m


@router.patch("/memories/{mid}")
def patch_memory(mid: int, body: MemoryPatch):
    m = _guard(lambda: data.update_memory(mid, body.content, body.category))
    if body.scope is not None:
        m = _guard(lambda: data.set_memory_scope(mid, body.scope))
    _audit("update_memory", "reversible_write", {"memory_id": mid, **body.model_dump(exclude_unset=True)})
    return m


@router.delete("/memories/{mid}")
def del_memory(mid: int):
    _guard(lambda: data.delete_memory(mid))
    _audit("delete_memory", "external", {"memory_id": mid})
    return {"ok": True}


@router.get("/memories/export")
def export_memories():
    payload = {"exported_at": db.now_iso(), "app": "E.V.", "memories": [
        {k: m.get(k) for k in ("id", "content", "category", "scope", "source", "created_at", "updated_at")} for m in data.list_memories()]}
    _audit("export_memories", "read", {"count": len(payload["memories"])})
    return JSONResponse(payload, headers={"Content-Disposition": 'attachment; filename="ev-memories.json"'})


@router.post("/memories/import")
def import_memories(body: MemoryImport):
    added = [_guard(lambda m=m: data.add_memory(m.content, m.category, source="user", scope=m.scope)) for m in body.memories]
    _audit("import_memories", "reversible_write", {"count": len(added)})
    return {"imported": len(added)}


# ------------------------------------------------------------- knowledge base
class IngestPath(BaseModel):
    path: str = Field(..., min_length=1, max_length=1000)


@router.get("/documents")
def get_documents():
    return documents.list_documents()


@router.post("/documents")
async def upload_document(file: UploadFile = File(...)):
    blob = await file.read(documents.MAX_UPLOAD_BYTES + 1)
    doc = _guard(lambda: documents.ingest(file.filename or "upload.txt", blob))
    _audit("ingest_document", "reversible_write", {"name": doc["name"], "chunks": doc["chunk_count"]})
    return doc


@router.post("/documents/ingest-path")
def ingest_path(body: IngestPath):
    def go():
        p, _ = files.resolve(body.path)
        if not p.is_file():
            raise ValueError("That path is not a file.")
        if p.stat().st_size > documents.MAX_UPLOAD_BYTES:
            raise ValueError("File is larger than 25 MB.")
        return documents.ingest(p.name, p.read_bytes(), source_path=str(p))
    doc = _guard(go)
    _audit("ingest_document", "reversible_write", {"path": body.path})
    return doc


@router.delete("/documents/{did}")
def del_document(did: int):
    _guard(lambda: documents.delete(did))
    _audit("delete_document", "external", {"document_id": did})
    return {"ok": True}


@router.get("/documents/search")
def search_documents(q: str, k: int = 5):
    return documents.search(q, max(1, min(k, 20)))


# ------------------------------------------------------------- workflows (jobs)
class JobIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    kind: Literal["reminder", "briefing", "prompt"]
    schedule: dict[str, Any]
    payload: dict[str, Any] = {}


class JobPatch(BaseModel):
    enabled: bool


@router.get("/jobs")
def get_jobs():
    return jobs.list_jobs()


@router.post("/jobs")
def post_job(body: JobIn):
    j = _guard(lambda: jobs.create_job(body.name, body.kind, body.schedule, body.payload, created_by="user"))
    _audit("schedule_workflow", "reversible_write", body.model_dump())
    return j


@router.patch("/jobs/{jid}")
def patch_job(jid: int, body: JobPatch):
    return _guard(lambda: jobs.set_enabled(jid, body.enabled))


@router.delete("/jobs/{jid}")
def del_job(jid: int):
    jobs.delete_job(jid)
    _audit("delete_workflow", "external", {"job_id": jid})
    return {"ok": True}


@router.post("/jobs/{jid}/run")
async def run_job(jid: int):
    try:
        return await jobs.run_now(jid)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


# ------------------------------------------------------------- feed
@router.get("/notifications")
def get_notifications(limit: int = 30, unread: bool = False):
    return data.list_notifications(min(limit, 200), unread)


@router.post("/notifications/read-all")
def read_all():
    db.execute("UPDATE notifications SET read = 1 WHERE read = 0")
    return {"ok": True}


@router.post("/notifications/{nid}/read")
def read_one(nid: int):
    db.execute("UPDATE notifications SET read = 1 WHERE id = ?", (nid,))
    return {"ok": True}


@router.delete("/notifications")
def clear_notifications():
    db.execute("DELETE FROM notifications WHERE read = 1")
    return {"ok": True}


# ------------------------------------------------------------- folders
class FolderIn(BaseModel):
    path: str = Field(..., min_length=1, max_length=1000)
    label: str = Field("", max_length=60)
    writable: bool = False


@router.get("/folders")
def get_folders():
    return files.list_folders()


@router.post("/folders")
def post_folder(body: FolderIn):
    f = _guard(lambda: files.add_folder(body.path, body.label, body.writable))
    _audit("authorize_folder", "reversible_write", body.model_dump())
    return f


@router.delete("/folders/{fid}")
def del_folder(fid: int):
    files.remove_folder(fid)
    _audit("revoke_folder", "reversible_write", {"folder_id": fid})
    return {"ok": True}


# ------------------------------------------------------------- drafts, audit, search
@router.get("/drafts")
def get_drafts():
    return db.rows("SELECT * FROM drafts ORDER BY id DESC LIMIT 100")


@router.delete("/drafts/{did}")
def del_draft(did: int):
    db.execute("DELETE FROM drafts WHERE id = ? AND status = 'draft'", (did,))
    return {"ok": True}


@router.get("/audit")
def get_audit(limit: int = 100, tool: Optional[str] = None):
    if tool:
        return db.rows("SELECT * FROM audit_log WHERE tool = ? ORDER BY id DESC LIMIT ?", (tool, min(limit, 1000)))
    return db.rows("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (min(limit, 1000),))


@router.get("/search")
def global_search(q: str):
    q = q.strip()
    if len(q) < 2:
        return {"tasks": [], "memories": [], "documents": [], "conversations": []}
    like = f"%{q}%"
    return {
        "tasks": db.rows("SELECT id, title, status, due_at FROM tasks WHERE title LIKE ? OR notes LIKE ? LIMIT 10", (like, like)),
        "memories": db.rows("SELECT id, content, category FROM memories WHERE content LIKE ? LIMIT 10", (like,)),
        "documents": [{k: h[k] for k in ("ref", "document_id", "text")} for h in documents.search(q, 5)],
        "conversations": db.rows(
            "SELECT DISTINCT c.id, c.title FROM conversations c JOIN messages m ON m.conversation_id = c.id "
            "WHERE (c.title LIKE ? OR (m.role IN ('user','assistant') AND m.content LIKE ?)) LIMIT 10", (like, like)),
    }
