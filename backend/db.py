"""SQLite storage. One short-lived connection per call keeps things simple and
safe across the async server, the background scheduler, and worker threads."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    user_agent TEXT,
    ip TEXT
);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT 'New conversation',
    agent TEXT NOT NULL DEFAULT 'ev',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,              -- user | assistant | tool
    content TEXT NOT NULL DEFAULT '',
    tool_calls TEXT,                 -- JSON list for assistant messages
    tool_call_id TEXT,               -- for tool messages
    name TEXT,                       -- tool name for tool messages
    provider TEXT,
    model TEXT,
    raw TEXT,                        -- provider-native payload (JSON), replayed verbatim
    meta TEXT,                       -- JSON: per-turn context, citations, etc.
    status TEXT NOT NULL DEFAULT 'complete',  -- complete | cancelled | error
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    source TEXT NOT NULL DEFAULT 'user',      -- user | assistant (approved)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',      -- open | done
    priority TEXT NOT NULL DEFAULT 'normal',  -- low | normal | high
    due_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    location TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_path TEXT,
    stored_path TEXT,
    mime TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    chars INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    page INTEGER,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id, idx);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                       -- reminder | briefing | prompt
    schedule TEXT NOT NULL,                   -- JSON
    payload TEXT NOT NULL DEFAULT '{}',       -- JSON
    enabled INTEGER NOT NULL DEFAULT 1,
    approved_at TEXT,
    created_by TEXT NOT NULL DEFAULT 'user',
    last_run_at TEXT,
    next_run_at TEXT,
    last_status TEXT,
    last_result TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    conversation_id INTEGER,
    tool TEXT NOT NULL,
    level TEXT NOT NULL,
    args TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | denied | expired | executed | failed
    result TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,                      -- assistant | user | job | system
    tool TEXT NOT NULL,
    level TEXT NOT NULL,
    args TEXT,
    status TEXT NOT NULL,                     -- ok | error | denied | invalid | timeout | awaiting_approval
    detail TEXT,
    duration_ms INTEGER,
    conversation_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'info',        -- info | warn | tip | live
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'system',
    link TEXT,
    dedupe_key TEXT UNIQUE,
    read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    writable INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'email',
    recipient TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',     -- draft | sent
    created_at TEXT NOT NULL,
    sent_at TEXT
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
"""

FTS_AVAILABLE = False


def now_iso() -> str:
    """UTC timestamp, second precision, ISO-8601 with offset."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    global FTS_AVAILABLE
    config.ensure_dirs()
    with tx() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        try:
            conn.executescript(FTS_SCHEMA)
            FTS_AVAILABLE = True
        except sqlite3.OperationalError:
            FTS_AVAILABLE = False  # falls back to LIKE search
        # Approvals can't survive a restart: the request that was waiting is gone.
        conn.execute(
            "UPDATE approvals SET status='expired', decided_at=? WHERE status='pending'",
            (now_iso(),),
        )


def rows(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    with tx() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def row(sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    with tx() as conn:
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else None


def execute(sql: str, params: tuple | dict = ()) -> int:
    """Run a write; returns lastrowid (inserts) or rowcount (updates/deletes)."""
    with tx() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount


def scalar(sql: str, params: tuple | dict = ()) -> Any:
    with tx() as conn:
        r = conn.execute(sql, params).fetchone()
        return r[0] if r else None


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
