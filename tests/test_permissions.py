import asyncio

import pytest

from backend import db
from backend.services import approvals, files, settings
from backend.tools import registry
from backend.tools.registry import Level, ToolContext, ToolError

CTX = ToolContext(actor="assistant", conversation_id=None)


def test_external_always_needs_approval_even_in_auto_mode():
    settings.put("permissions.reversible_write", "auto")
    p = registry.prepare("delete_task", {"task_id": 1}, CTX)
    assert p.needs_approval is True
    w = registry.prepare("create_task", {"title": "Buy milk"}, CTX)
    assert w.needs_approval is False


def test_reversible_write_asks_by_default_and_read_does_not():
    assert registry.prepare("create_task", {"title": "Buy milk"}, CTX).needs_approval is True
    assert registry.prepare("list_tasks", {}, CTX).needs_approval is False


def test_memory_saves_always_confirm_by_default():
    settings.put("permissions.reversible_write", "auto")
    assert registry.prepare("save_memory", {"content": "Prefers metric units"}, CTX).needs_approval is True


def test_run_refuses_without_matching_approval():
    async def go():
        p = registry.prepare("create_task", {"title": "Buy milk"}, CTX)
        with pytest.raises(ToolError) as e:
            await registry.run(p, CTX)
        assert e.value.status == "denied"
        # an approval for different arguments must not be accepted
        other = registry.prepare("create_task", {"title": "Something else"}, CTX)
        rec = approvals.create(other, CTX)
        approvals.decide(rec["id"], True)
        with pytest.raises(ToolError):
            await registry.run(p, CTX, approval_id=rec["id"])
        # the matching approval works
        rec2 = approvals.create(p, CTX)
        approvals.decide(rec2["id"], True)
        result = await registry.run(p, CTX, approval_id=rec2["id"])
        assert result["created"]["title"] == "Buy milk"
    asyncio.run(go())
    assert db.scalar("SELECT COUNT(*) FROM tasks") == 1


def test_invalid_args_and_unknown_tool_are_rejected_and_audited():
    with pytest.raises(ToolError) as e:
        registry.prepare("create_task", {"priority": "urgent"}, CTX)
    assert e.value.status == "invalid"
    with pytest.raises(ToolError):
        registry.prepare("format_hard_drive", {}, CTX)
    statuses = [r["status"] for r in db.rows("SELECT status FROM audit_log")]
    assert statuses.count("invalid") == 2


def test_disabled_tool_denied():
    settings.put("tools.disabled", ["list_tasks"])
    with pytest.raises(ToolError) as e:
        registry.prepare("list_tasks", {}, CTX)
    assert e.value.status == "denied"


def test_background_jobs_capped_at_prepare():
    job = ToolContext(actor="job", max_level=Level.PREPARE)
    with pytest.raises(ToolError) as e:
        registry.prepare("create_task", {"title": "x"}, job)
    assert e.value.status == "denied"
    assert registry.prepare("list_tasks", {}, job)


def test_setup_required_tool_reports_honestly():
    with pytest.raises(ToolError) as e:
        registry.prepare("send_email", {"to": "a@b.co", "subject": "Hi", "body": "Hello"}, CTX)
    assert e.value.status == "unavailable" and "Setup required" in e.value.message


def test_tool_timeout(monkeypatch):
    async def slow(a, ctx):
        await asyncio.sleep(5)
    t = registry.TOOLS["system_status"]
    monkeypatch.setattr(t, "handler", slow)
    monkeypatch.setattr(t, "timeout", 0.05)
    p = registry.prepare("system_status", {}, CTX)
    with pytest.raises(ToolError) as e:
        asyncio.run(registry.run(p, CTX))
    assert e.value.status == "timeout"
    assert db.scalar("SELECT status FROM audit_log ORDER BY id DESC LIMIT 1") == "timeout"


# ---------------------------------------------------------------- files
def test_file_access_is_confined(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "notes.md").write_text("hello world")
    (root / ".env").write_text("SECRET=1")
    (tmp_path / "outside.txt").write_text("nope")
    files.add_folder(str(root), "Project", writable=False)

    assert files.read_text("notes.md")["content"] == "hello world"
    with pytest.raises(files.FileAccessError):
        files.read_text("../outside.txt")
    with pytest.raises(files.FileAccessError):
        files.read_text(str(tmp_path / "outside.txt"))
    with pytest.raises(files.FileAccessError, match="protected"):
        files.read_text(".env")
    with pytest.raises(files.FileAccessError, match="read-only"):
        files.write_text("new.md", "x")
    names = [e["name"] for e in files.list_dir(str(root))["entries"]]
    assert ".env" not in names and "notes.md" in names


def test_overwrite_keeps_backup(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    files.add_folder(str(root), "W", writable=True)
    files.write_text("a.txt", "v1")
    with pytest.raises(files.FileAccessError, match="already exists"):
        files.write_text("a.txt", "v2")
    out = files.write_text("a.txt", "v2", overwrite=True)
    assert (root / "a.txt").read_text() == "v2"
    from pathlib import Path
    assert Path(out["backup"]).read_text() == "v1"
