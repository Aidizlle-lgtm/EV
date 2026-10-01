"""Laptop file access, limited to folders you explicitly authorize.

* Read tools work only inside authorized folders.
* Writing works only inside folders you marked writable, and an overwritten file
  is first copied to data/backups/ so the change can be undone.
* Credentials-like files (.env, keys, E.V.'s own data) are never readable.
"""
from __future__ import annotations

import fnmatch
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import config, db

MAX_READ_BYTES = 1_000_000
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".log",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".htm", ".css", ".scss", ".java", ".kt", ".c", ".h", ".cpp",
    ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".ps1", ".bat", ".sql", ".xml", ".svg", ".r", ".m", ".swift",
    ".tex", ".rst",
}
BLOCKED_NAMES = {".env", "id_rsa", "id_ed25519", "id_ecdsa", "credentials", "credentials.json", ".npmrc", ".pypirc", ".netrc"}
BLOCKED_PATTERNS = ("*.pem", "*.key", "*.pfx", "*.p12", "*.kdbx", ".env.*", "*.sqlite", "*.db")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", "dist", "build"}


class FileAccessError(Exception):
    pass


def list_folders() -> list[dict[str, Any]]:
    out = []
    for f in db.rows("SELECT * FROM folders ORDER BY path"):
        f["exists"] = Path(f["path"]).is_dir()
        out.append(f)
    return out


def add_folder(path: str, label: str = "", writable: bool = False) -> dict[str, Any]:
    p = Path(os.path.expanduser(path.strip().strip('"'))).resolve()
    if not p.is_dir():
        raise FileAccessError(f"'{p}' is not an existing folder on this computer.")
    if p == Path(p.anchor):
        raise FileAccessError("Authorizing a whole drive is too broad. Pick a specific folder.")
    db.execute(
        "INSERT INTO folders(path, label, writable, created_at) VALUES(?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET label = excluded.label, writable = excluded.writable",
        (str(p), label or p.name, 1 if writable else 0, db.now_iso()),
    )
    return db.row("SELECT * FROM folders WHERE path = ?", (str(p),))  # type: ignore[return-value]


def remove_folder(folder_id: int) -> None:
    db.execute("DELETE FROM folders WHERE id = ?", (folder_id,))


def is_blocked(p: Path) -> bool:
    name = p.name.lower()
    if name in BLOCKED_NAMES or any(fnmatch.fnmatch(name, pat) for pat in BLOCKED_PATTERNS):
        return True
    try:
        if p == config.ENV_FILE.resolve() or p.is_relative_to(config.DATA_DIR):
            return True
    except (OSError, ValueError):
        return True
    return False


def resolve(path: str, *, write: bool = False) -> tuple[Path, dict[str, Any]]:
    """Resolve a user/model-supplied path and prove it sits inside an authorized folder."""
    if not path or "\x00" in path:
        raise FileAccessError("Empty or invalid path.")
    folders = list_folders()
    if not folders:
        raise FileAccessError("No folders are authorized yet. Add one in Settings → Folders.")
    raw = Path(os.path.expanduser(path.strip().strip('"')))
    candidates = [raw] if raw.is_absolute() else [Path(f["path"]) / raw for f in folders]
    for cand in candidates:
        try:
            p = cand.resolve()
        except (OSError, RuntimeError):
            continue
        for f in folders:
            root = Path(f["path"]).resolve()
            if p == root or p.is_relative_to(root):
                if is_blocked(p):
                    raise FileAccessError(f"'{p.name}' is protected (credentials or E.V. data) and can't be accessed.")
                if write and not f["writable"]:
                    raise FileAccessError(f"Folder '{f['label']}' is read-only for E.V. Mark it writable in Settings → Folders.")
                return p, f
    raise FileAccessError(f"'{path}' is outside the folders you've authorized.")


def list_dir(path: str, limit: int = 200) -> dict[str, Any]:
    p, folder = resolve(path)
    if not p.is_dir():
        raise FileAccessError(f"'{p}' is not a folder.")
    entries = []
    for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))[:limit]:
        if child.name in SKIP_DIRS or is_blocked(child):
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        entries.append({"name": child.name, "type": "folder" if child.is_dir() else "file",
                        "size": st.st_size if child.is_file() else None,
                        "modified": datetime.fromtimestamp(st.st_mtime).replace(microsecond=0).isoformat()})
    return {"path": str(p), "folder": folder["label"], "entries": entries}


def read_text(path: str, max_chars: int = 20000) -> dict[str, Any]:
    p, _ = resolve(path)
    if not p.is_file():
        raise FileAccessError(f"'{p}' is not a file.")
    if p.stat().st_size > MAX_READ_BYTES:
        raise FileAccessError(f"'{p.name}' is larger than 1 MB. Add it to the Knowledge Base instead.")
    if p.suffix.lower() not in TEXT_EXTENSIONS:
        raise FileAccessError(f"'{p.name}' isn't a plain-text file. For PDFs/Word files use the Knowledge Base.")
    text = p.read_text(encoding="utf-8", errors="replace")
    return {"path": str(p), "chars": len(text), "truncated": len(text) > max_chars, "content": text[:max_chars]}


def search(query: str, folder: str | None = None, content: bool = True, limit: int = 30) -> list[dict[str, Any]]:
    roots = [resolve(folder)[0]] if folder else [Path(f["path"]) for f in list_folders() if Path(f["path"]).is_dir()]
    q = query.lower().strip()
    if not q:
        raise FileAccessError("Search query is empty.")
    hits: list[dict[str, Any]] = []
    scanned = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                scanned += 1
                if scanned > 20000 or len(hits) >= limit:
                    return hits
                fp = Path(dirpath) / fn
                if is_blocked(fp):
                    continue
                if q in fn.lower():
                    hits.append({"path": str(fp), "match": "name"})
                    continue
                if content and fp.suffix.lower() in TEXT_EXTENSIONS:
                    try:
                        if fp.stat().st_size > 300_000:
                            continue
                        text = fp.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    i = text.lower().find(q)
                    if i >= 0:
                        line_no = text.count("\n", 0, i) + 1
                        snippet = text[max(0, i - 80): i + 120].replace("\n", " ")
                        hits.append({"path": str(fp), "match": "content", "line": line_no, "snippet": snippet})
    return hits


def write_text(path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    p, folder = resolve(path, write=True)
    if p.suffix.lower() not in TEXT_EXTENSIONS:
        raise FileAccessError("E.V. only writes plain-text/code files.")
    backup = None
    if p.exists():
        if not overwrite:
            raise FileAccessError(f"'{p.name}' already exists. Ask again with overwrite=true to replace it (a backup is kept).")
        if not p.is_file():
            raise FileAccessError(f"'{p}' is not a file.")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = config.BACKUP_DIR / f"{stamp}_{p.name}"
        shutil.copy2(p, backup)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"path": str(p), "bytes": len(content.encode()), "backup": str(backup) if backup else None,
            "folder": folder["label"], "created": backup is None}
