"""Knowledge Base: ingest documents, chunk them, and retrieve passages with
source references (document name, chunk number, and page for PDFs)."""
from __future__ import annotations

import html
import io
import re
import zipfile
from pathlib import Path
from typing import Any

from .. import config, db

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
TEXT_TYPES = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log", ".py", ".js", ".ts",
              ".html", ".htm", ".css", ".xml", ".ini", ".toml", ".rst", ".tex", ".sql", ".java", ".c", ".cpp", ".cs", ".go"}
SUPPORTED = TEXT_TYPES | {".pdf", ".docx"}


class IngestError(Exception):
    pass


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(re.sub(r"[ \t]+", " ", text))


def _docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
    paragraphs = []
    for para in re.findall(r"(?s)<w:p[ >].*?</w:p>", xml):
        runs = re.findall(r"(?s)<w:t[^>]*>(.*?)</w:t>", para)
        paragraphs.append(html.unescape("".join(runs)))
    return "\n".join(p for p in paragraphs if p.strip())


def extract(name: str, data: bytes) -> list[tuple[int | None, str]]:
    """Return [(page_or_None, text)] segments."""
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED:
        raise IngestError(f"Unsupported file type '{ext or name}'. Supported: PDF, DOCX, and text/code files.")
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:  # pragma: no cover
            raise IngestError("PDF support needs the 'pypdf' package (pip install -r requirements.txt).") from e
        try:
            reader = PdfReader(io.BytesIO(data))
            pages = [(i + 1, (page.extract_text() or "")) for i, page in enumerate(reader.pages)]
        except Exception as e:
            raise IngestError(f"Couldn't read that PDF: {e}") from e
        if not any(t.strip() for _, t in pages):
            raise IngestError("That PDF has no extractable text (it may be scanned images). OCR isn't set up.")
        return pages
    if ext == ".docx":
        try:
            return [(None, _docx_text(data))]
        except (KeyError, zipfile.BadZipFile) as e:
            raise IngestError("That .docx file looks damaged or isn't a Word document.") from e
    text = data.decode("utf-8", errors="replace")
    if ext in (".html", ".htm"):
        text = _strip_html(text)
    return [(None, text)]


def chunk(segments: list[tuple[int | None, str]]) -> list[tuple[int | None, str]]:
    out: list[tuple[int | None, str]] = []
    for page, text in segments:
        text = re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip()
        start = 0
        while start < len(text):
            end = min(len(text), start + CHUNK_CHARS)
            if end < len(text):
                brk = max(text.rfind("\n\n", start + CHUNK_CHARS // 2, end), text.rfind(". ", start + CHUNK_CHARS // 2, end))
                if brk > start:
                    end = brk + 1
            piece = text[start:end].strip()
            if piece:
                out.append((page, piece))
            if end >= len(text):
                break
            start = max(end - CHUNK_OVERLAP, start + 1)
    return out


def ingest(name: str, data: bytes, source_path: str | None = None) -> dict[str, Any]:
    if len(data) > MAX_UPLOAD_BYTES:
        raise IngestError("File is larger than 25 MB.")
    safe_name = re.sub(r"[^\w.\- ]", "_", Path(name).name)[:120] or "document"
    pieces = chunk(extract(safe_name, data))
    if not pieces:
        raise IngestError("No text found in that file.")
    chars = sum(len(t) for _, t in pieces)
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO documents(name, source_path, mime, size, chars, chunk_count, created_at) VALUES(?,?,?,?,?,?,?)",
            (safe_name, source_path, Path(safe_name).suffix.lower(), len(data), chars, len(pieces), db.now_iso()),
        )
        doc_id = cur.lastrowid
        stored = config.UPLOAD_DIR / f"{doc_id}_{safe_name}"
        stored.write_bytes(data)
        conn.execute("UPDATE documents SET stored_path = ? WHERE id = ?", (str(stored), doc_id))
        for idx, (page, text) in enumerate(pieces, start=1):
            c = conn.execute("INSERT INTO chunks(document_id, idx, page, text) VALUES(?,?,?,?)", (doc_id, idx, page, text))
            if db.FTS_AVAILABLE:
                conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?, ?)", (c.lastrowid, text))
    return db.row("SELECT * FROM documents WHERE id = ?", (doc_id,))  # type: ignore[return-value]


def delete(doc_id: int) -> None:
    doc = db.row("SELECT * FROM documents WHERE id = ?", (doc_id,))
    if not doc:
        raise LookupError(f"Document {doc_id} not found")
    with db.tx() as conn:
        if db.FTS_AVAILABLE:
            for r in conn.execute("SELECT id, text FROM chunks WHERE document_id = ?", (doc_id,)).fetchall():
                conn.execute("INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', ?, ?)", (r["id"], r["text"]))
        conn.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    if doc.get("stored_path"):
        Path(doc["stored_path"]).unlink(missing_ok=True)


def list_documents() -> list[dict[str, Any]]:
    return db.rows("SELECT id, name, source_path, mime, size, chars, chunk_count, created_at FROM documents ORDER BY id DESC")


def reference(doc_name: str, doc_id: int, idx: int, page: int | None) -> str:
    where = f"p.{page}" if page else f"§{idx}"
    return f"[{doc_name} {where} · D{doc_id}]"


def search(query: str, k: int = 5, document_id: int | None = None) -> list[dict[str, Any]]:
    words = re.findall(r"\w+", query.lower())
    if not words:
        return []
    params: list[Any]
    if db.FTS_AVAILABLE:
        match = " OR ".join(f'"{w}"' for w in words[:20])
        sql = ("SELECT c.id, c.document_id, c.idx, c.page, c.text, d.name, bm25(chunks_fts) AS score "
               "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid JOIN documents d ON d.id = c.document_id "
               "WHERE chunks_fts MATCH ?")
        params = [match]
        if document_id:
            sql += " AND c.document_id = ?"
            params.append(document_id)
        sql += " ORDER BY score LIMIT ?"
        params.append(k)
        hits = db.rows(sql, tuple(params))
    else:
        clauses = " OR ".join("lower(c.text) LIKE ?" for _ in words[:10])
        sql = (f"SELECT c.id, c.document_id, c.idx, c.page, c.text, d.name, 0 AS score FROM chunks c "
               f"JOIN documents d ON d.id = c.document_id WHERE ({clauses})")
        params = [f"%{w}%" for w in words[:10]]
        if document_id:
            sql += " AND c.document_id = ?"
            params.append(document_id)
        hits = db.rows(sql + " LIMIT 200", tuple(params))
        hits.sort(key=lambda h: -sum(h["text"].lower().count(w) for w in words))
        hits = hits[:k]
    return [{
        "ref": reference(h["name"], h["document_id"], h["idx"], h["page"]),
        "document_id": h["document_id"], "document": h["name"], "chunk": h["idx"], "page": h["page"],
        "text": h["text"],
    } for h in hits]
