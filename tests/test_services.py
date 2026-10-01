import asyncio
import io
import subprocess
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from backend import db
from backend.services import data, documents, jobs


def make_docx(paragraphs):
    xml = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body>{xml}</w:body></w:document>')
    return buf.getvalue()


def test_documents_ingest_and_cited_search():
    doc = documents.ingest("budget-notes.md", b"# Budget\n\nEmergency fund target is six months of expenses.\n\nGroceries cap: 400 per month.")
    assert doc["chunk_count"] >= 1
    hits = documents.search("emergency fund target")
    assert hits and "Emergency fund" in hits[0]["text"]
    assert hits[0]["ref"].startswith("[budget-notes.md §1 · D")
    docx = documents.ingest("plan.docx", make_docx(["Protein goal is 140 grams daily.", "Train four days a week."]))
    assert documents.search("protein goal")[0]["document_id"] == docx["id"]
    documents.delete(docx["id"])
    assert documents.search("protein goal") == []


def test_unsupported_type_is_explained():
    try:
        documents.ingest("photo.jpg", b"\xff\xd8")
    except documents.IngestError as e:
        assert "Unsupported" in str(e)
    else:
        raise AssertionError("expected IngestError")


def test_schedules():
    now = datetime(2026, 10, 1, 9, 0).astimezone()
    daily = jobs.validate_schedule({"type": "daily", "time": "08:00"})
    assert jobs.next_run(daily, now).day == 2
    weekdays = jobs.validate_schedule({"type": "daily", "time": "10:00", "weekdays": [0]})  # Mondays
    assert jobs.next_run(weekdays, now).weekday() == 0
    interval = jobs.validate_schedule({"type": "interval", "minutes": 30})
    assert jobs.next_run(interval, now) == now + timedelta(minutes=30)
    for bad in ({"type": "daily", "time": "25:00"}, {"type": "interval", "minutes": 1}, {"type": "hourly"}):
        try:
            jobs.validate_schedule(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")


def test_due_reminder_runs_and_posts_to_feed():
    when = (data.local_now() + timedelta(minutes=10)).isoformat()
    job = jobs.create_job("Stretch", "reminder", {"type": "once", "at": when}, {"message": "Stand up and stretch"})
    db.execute("UPDATE jobs SET next_run_at = ? WHERE id = ?", ((data.local_now() - timedelta(seconds=1)).isoformat(), job["id"]))
    asyncio.run(jobs.tick())
    j = jobs.get_job(job["id"])
    assert j["last_status"] == "ok" and j["enabled"] == 0  # one-time job retires itself
    assert db.scalar("SELECT title FROM notifications") == "Reminder: Stand up and stretch"


def test_tasks_and_overdue():
    data.create_task("Old bill", due_at=(data.local_now() - timedelta(days=1)).isoformat())
    data.create_task("Future", due_at=(data.local_now() + timedelta(days=3)).isoformat())
    assert [t["title"] for t in data.overdue_tasks()] == ["Old bill"]


def test_memory_api_roundtrip(authed):
    m = authed.post("/api/memories", json={"content": "Prefers metric units", "category": "preferences"}).json()
    authed.patch(f"/api/memories/{m['id']}", json={"content": "Prefers metric units and 24h time"})
    export = authed.get("/api/memories/export")
    assert export.headers["content-disposition"].startswith("attachment")
    assert export.json()["memories"][0]["content"] == "Prefers metric units and 24h time"
    authed.delete(f"/api/memories/{m['id']}")
    assert authed.get("/api/memories").json() == []
    assert authed.post("/api/memories/import", json={"memories": [{"content": "Vegetarian", "category": "food"}]}).json()["imported"] == 1


def test_dashboard_reports_setup_required_without_keys(authed):
    d = authed.get("/api/dashboard").json()
    assert d["status"] == "SETUP REQUIRED"
    groq = next(p for p in d["providers"] if p["id"] == "groq")
    assert groq["status"] == "setup_required"
    ints = authed.get("/api/integrations").json()
    assert next(i for i in ints if i["id"] == "elevenlabs")["status"] == "setup_required"
    assert next(i for i in ints if i["id"] == "web_search")["status"] == "not_implemented"


def test_secret_is_stored_but_never_returned(authed, monkeypatch):
    r = authed.put("/api/secrets/GITHUB_TOKEN", json={"value": "github_pat_abcdefghijklmnop1234"})
    assert r.json()["hint"] == "••••1234"
    body = authed.get("/api/settings").text
    assert "github_pat_abcdefghijklmnop1234" not in body
    authed.delete("/api/secrets/GITHUB_TOKEN")
    assert authed.put("/api/secrets/NOT_A_SECRET", json={"value": "x"}).status_code == 400


def test_no_secrets_committed():
    """Guard against ever committing an API key (patterns for the providers E.V. supports)."""
    root = Path(__file__).resolve().parent.parent
    try:
        tracked = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return
    import re
    pattern = re.compile(r"(gsk_[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AIza[0-9A-Za-z_-]{30,})")
    for name in tracked:
        p = root / name
        if p.suffix in {".webp", ".png", ".jpg", ".ico"} or not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        assert not pattern.search(text), f"possible API key in {name}"
