"""Plain data operations shared by the REST API (you) and the tools (E.V.)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .. import db

PRIORITIES = ("low", "normal", "high")


def local_now() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def parse_when(value: str | None) -> str | None:
    """Accept ISO date or datetime (with or without offset); store as local ISO with offset."""
    if not value:
        return None
    value = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"'{value}' is not an ISO date/time like 2026-10-01 or 2026-10-01T15:30") from e
    if len(value) == 10:  # date only → end of that day is a sensible due time
        dt = dt.replace(hour=17, minute=0)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # interpret naive values in the laptop's local time zone
    return dt.astimezone().replace(microsecond=0).isoformat()


# -- Tasks -------------------------------------------------------------------
def list_tasks(status: str | None = "open", limit: int = 100) -> list[dict[str, Any]]:
    sql = "SELECT * FROM tasks"
    params: tuple = ()
    if status in ("open", "done"):
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY status, CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, due_at IS NULL, due_at, id LIMIT ?"
    return db.rows(sql, params + (limit,))


def get_task(task_id: int) -> dict[str, Any] | None:
    return db.row("SELECT * FROM tasks WHERE id = ?", (task_id,))


def create_task(title: str, notes: str = "", priority: str = "normal", due_at: str | None = None) -> dict[str, Any]:
    if priority not in PRIORITIES:
        raise ValueError("priority must be low, normal, or high")
    now = db.now_iso()
    tid = db.execute(
        "INSERT INTO tasks(title, notes, priority, due_at, created_at, updated_at) VALUES(?,?,?,?,?,?)",
        (title.strip(), notes, priority, parse_when(due_at), now, now),
    )
    return get_task(tid)  # type: ignore[return-value]


def update_task(task_id: int, **fields: Any) -> dict[str, Any]:
    current = get_task(task_id)
    if not current:
        raise LookupError(f"Task {task_id} not found")
    allowed = {"title", "notes", "priority", "due_at", "status"}
    changes = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "priority" in changes and changes["priority"] not in PRIORITIES:
        raise ValueError("priority must be low, normal, or high")
    if "status" in changes:
        if changes["status"] not in ("open", "done"):
            raise ValueError("status must be open or done")
        changes["completed_at"] = db.now_iso() if changes["status"] == "done" else None
    if "due_at" in changes:
        changes["due_at"] = parse_when(changes["due_at"]) if changes["due_at"] else None
    if not changes:
        return current
    changes["updated_at"] = db.now_iso()
    cols = ", ".join(f"{k} = ?" for k in changes)
    db.execute(f"UPDATE tasks SET {cols} WHERE id = ?", tuple(changes.values()) + (task_id,))
    return get_task(task_id)  # type: ignore[return-value]


def delete_task(task_id: int) -> dict[str, Any]:
    current = get_task(task_id)
    if not current:
        raise LookupError(f"Task {task_id} not found")
    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return current


def overdue_tasks() -> list[dict[str, Any]]:
    now = local_now()
    out = []
    for t in list_tasks("open"):
        if t["due_at"] and datetime.fromisoformat(t["due_at"]) < now:
            out.append(t)
    return out


# -- Calendar (local) --------------------------------------------------------
def list_events(start: str | None = None, end: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    start_dt = datetime.fromisoformat(parse_when(start)) if start else local_now().replace(hour=0, minute=0, second=0)
    end_dt = datetime.fromisoformat(parse_when(end)) if end else start_dt + timedelta(days=7)
    events = db.rows("SELECT * FROM events ORDER BY starts_at LIMIT 2000")
    out = []
    for e in events:
        s = datetime.fromisoformat(e["starts_at"])
        if start_dt <= s < end_dt or (e["ends_at"] and s < start_dt < datetime.fromisoformat(e["ends_at"])):
            out.append(e)
    return out[:limit]


def create_event(title: str, starts_at: str, ends_at: str | None = None, location: str = "", notes: str = "") -> dict[str, Any]:
    s = parse_when(starts_at)
    e = parse_when(ends_at) if ends_at else None
    if e and datetime.fromisoformat(e) < datetime.fromisoformat(s):  # type: ignore[arg-type]
        raise ValueError("ends_at is before starts_at")
    eid = db.execute(
        "INSERT INTO events(title, starts_at, ends_at, location, notes, created_at) VALUES(?,?,?,?,?,?)",
        (title.strip(), s, e, location, notes, db.now_iso()),
    )
    return db.row("SELECT * FROM events WHERE id = ?", (eid,))  # type: ignore[return-value]


def delete_event(event_id: int) -> dict[str, Any]:
    current = db.row("SELECT * FROM events WHERE id = ?", (event_id,))
    if not current:
        raise LookupError(f"Event {event_id} not found")
    db.execute("DELETE FROM events WHERE id = ?", (event_id,))
    return current


def today_timeline() -> list[dict[str, Any]]:
    start = local_now().replace(hour=0, minute=0, second=0)
    items = []
    for e in list_events(start.isoformat(), (start + timedelta(days=1)).isoformat()):
        items.append({"kind": "event", "id": e["id"], "title": e["title"], "at": e["starts_at"], "ends_at": e["ends_at"]})
    for t in list_tasks("open"):
        if t["due_at"] and start <= datetime.fromisoformat(t["due_at"]) < start + timedelta(days=1):
            items.append({"kind": "task", "id": t["id"], "title": t["title"], "at": t["due_at"], "ends_at": None})
    return sorted(items, key=lambda i: i["at"])


# -- Memories ----------------------------------------------------------------
def list_memories(query: str | None = None) -> list[dict[str, Any]]:
    if query:
        like = f"%{query.strip()}%"
        return db.rows("SELECT * FROM memories WHERE content LIKE ? OR category LIKE ? ORDER BY id DESC", (like, like))
    return db.rows("SELECT * FROM memories ORDER BY category, id")


def add_memory(content: str, category: str = "general", source: str = "user") -> dict[str, Any]:
    content = content.strip()
    if not content:
        raise ValueError("Memory content is empty")
    if len(content) > 1000:
        raise ValueError("Keep each memory under 1000 characters")
    now = db.now_iso()
    mid = db.execute(
        "INSERT INTO memories(content, category, source, created_at, updated_at) VALUES(?,?,?,?,?)",
        (content, (category or "general").strip().lower()[:40], source, now, now),
    )
    return db.row("SELECT * FROM memories WHERE id = ?", (mid,))  # type: ignore[return-value]


def update_memory(memory_id: int, content: str | None = None, category: str | None = None) -> dict[str, Any]:
    current = db.row("SELECT * FROM memories WHERE id = ?", (memory_id,))
    if not current:
        raise LookupError(f"Memory {memory_id} not found")
    db.execute(
        "UPDATE memories SET content = ?, category = ?, updated_at = ? WHERE id = ?",
        ((content or current["content"]).strip(), (category or current["category"]).strip().lower()[:40], db.now_iso(), memory_id),
    )
    return db.row("SELECT * FROM memories WHERE id = ?", (memory_id,))  # type: ignore[return-value]


def delete_memory(memory_id: int) -> dict[str, Any]:
    current = db.row("SELECT * FROM memories WHERE id = ?", (memory_id,))
    if not current:
        raise LookupError(f"Memory {memory_id} not found")
    db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    return current


# -- Notifications (Live Intelligence Feed) ----------------------------------
def notify(title: str, body: str = "", kind: str = "info", source: str = "system",
           dedupe_key: str | None = None, link: str | None = None) -> int | None:
    if dedupe_key and db.row("SELECT id FROM notifications WHERE dedupe_key = ?", (dedupe_key,)):
        return None
    return db.execute(
        "INSERT INTO notifications(kind, title, body, source, link, dedupe_key, created_at) VALUES(?,?,?,?,?,?,?)",
        (kind, title[:200], body[:2000], source, link, dedupe_key, db.now_iso()),
    )


def list_notifications(limit: int = 30, unread_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM notifications"
    if unread_only:
        sql += " WHERE read = 0"
    return db.rows(sql + " ORDER BY id DESC LIMIT ?", (limit,))


# -- Drafts ------------------------------------------------------------------
def create_draft(kind: str, recipient: str, subject: str, body: str) -> dict[str, Any]:
    did = db.execute(
        "INSERT INTO drafts(kind, recipient, subject, body, created_at) VALUES(?,?,?,?,?)",
        (kind, recipient, subject, body, db.now_iso()),
    )
    return db.row("SELECT * FROM drafts WHERE id = ?", (did,))  # type: ignore[return-value]
