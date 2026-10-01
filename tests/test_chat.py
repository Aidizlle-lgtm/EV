import asyncio

from backend import db
from backend.providers.base import ProviderError
from backend.services import approvals, chat, settings
from tests.conftest import call


async def collect(conv_id, text, on_event=None, agent=None):
    events = []
    async for ev in chat.stream_chat(conv_id, text, agent):
        events.append(ev)
        if on_event:
            await on_event(ev)
    return events


def test_plain_reply_streams_and_persists(fake_provider):
    fake_provider([("Hello! I'm E.V. How can I help?", [])])
    conv = chat.create_conversation()
    events = asyncio.run(collect(conv["id"], "hi"))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "run" and kinds[-1] == "done"
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "Hello! I'm E.V. How can I help?"
    msgs = db.rows("SELECT role, content FROM messages WHERE conversation_id = ?", (conv["id"],))
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert db.row("SELECT title FROM conversations WHERE id = ?", (conv["id"],))["title"] == "hi"


def test_tool_call_waits_for_approval_then_runs(fake_provider):
    p = fake_provider([
        ("Adding that now.", [call("create_task", {"title": "Call the dentist", "priority": "high"})]),
        ("Done — the task is on your board.", []),
    ])
    conv = chat.create_conversation()

    async def approve(ev):
        if ev["type"] == "approval_required":
            assert "Call the dentist" in ev["approval"]["summary"]
            approvals.decide(ev["approval"]["id"], True)

    events = asyncio.run(collect(conv["id"], "remind me to call the dentist", approve))
    kinds = [e["type"] for e in events]
    assert "approval_required" in kinds and kinds[-1] == "done"
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["status"] == "ok"
    assert db.scalar("SELECT title FROM tasks") == "Call the dentist"
    # the model saw the tool result on the second call, and the per-turn context
    second = p.calls[1]["messages"]
    assert second[0]["role"] == "user" and second[0]["content"].startswith("[Context: local time")
    assert second[-1]["role"] == "tool" and '"status": "ok"' in second[-1]["content"]


def test_denied_action_is_not_executed(fake_provider):
    fake_provider([
        ("", [call("create_task", {"title": "Should not exist"})]),
        ("Understood, I won't add it.", []),
    ])
    conv = chat.create_conversation()

    async def deny(ev):
        if ev["type"] == "approval_required":
            approvals.decide(ev["approval"]["id"], False)

    events = asyncio.run(collect(conv["id"], "add a task", deny))
    assert next(e for e in events if e["type"] == "tool_result")["status"] == "denied"
    assert db.scalar("SELECT COUNT(*) FROM tasks") == 0
    assert db.scalar("SELECT status FROM approvals") == "denied"


def test_auto_mode_runs_reversible_writes_without_prompt(fake_provider):
    settings.put("permissions.reversible_write", "auto")
    fake_provider([("", [call("create_task", {"title": "Auto task"})]), ("Added.", [])])
    conv = chat.create_conversation()
    events = asyncio.run(collect(conv["id"], "add it"))
    assert "approval_required" not in [e["type"] for e in events]
    assert db.scalar("SELECT COUNT(*) FROM tasks") == 1


def test_cancel_while_waiting_for_approval(fake_provider):
    fake_provider([("", [call("create_task", {"title": "Never"}, "c1"), call("list_tasks", {}, "c2")])])
    conv = chat.create_conversation()
    seen = {}

    async def cancel_on_prompt(ev):
        if ev["type"] == "run":
            seen["run"] = ev["run_id"]
        if ev["type"] == "approval_required":
            chat.cancel(seen["run"])

    events = asyncio.run(collect(conv["id"], "do things", cancel_on_prompt))
    assert events[-1]["type"] == "cancelled"
    assert db.scalar("SELECT COUNT(*) FROM tasks") == 0
    # every tool call has a recorded result, so the history stays valid for the next turn
    tool_msgs = db.rows("SELECT tool_call_id FROM messages WHERE role = 'tool' AND conversation_id = ?", (conv["id"],))
    assert {m["tool_call_id"] for m in tool_msgs} == {"c1", "c2"}
    assert db.scalar("SELECT status FROM approvals") == "expired"


def test_provider_error_is_readable_and_falls_back(fake_provider):
    fake_provider([ProviderError("rate_limit", "Fake AI rate limit or quota reached (HTTP 429).")])
    backup = fake_provider([("Answer from the backup.", [])], pid="fake2", label="Backup AI", active=False)
    settings.put("llm.fallback_provider", "fake2")
    conv = chat.create_conversation()
    events = asyncio.run(collect(conv["id"], "hello"))
    notice = next(e for e in events if e["type"] == "notice")
    assert "Backup AI" in notice["message"]
    assert events[-1]["type"] == "done" and backup.calls


def test_provider_error_without_fallback(fake_provider):
    fake_provider([ProviderError("auth", "Fake AI rejected the credentials (HTTP 401).")])
    conv = chat.create_conversation()
    events = asyncio.run(collect(conv["id"], "hello"))
    assert events[-1]["type"] == "error" and "rejected the credentials" in events[-1]["message"]
    # the error message is shown in the UI but excluded from future model history
    assert [m["role"] for m in chat.history(conv["id"])] == ["user"]


def test_agent_limits_tools(fake_provider):
    p = fake_provider([("ok", [])])
    conv = chat.create_conversation("memory")
    asyncio.run(collect(conv["id"], "what do you know about me?", agent="memory"))
    assert set(p.calls[0]["tools"]) == {"search_memories", "save_memory", "update_memory", "delete_memory"}
    assert "Memory mode" in p.calls[0]["system"]


def test_headless_run_never_requests_approval(fake_provider):
    fake_provider([("", [call("create_task", {"title": "sneaky"})]), ("Report: nothing to do.", [])])
    out = asyncio.run(chat.run_headless("Check my tasks"))
    assert out == "Report: nothing to do."
    assert db.scalar("SELECT COUNT(*) FROM tasks") == 0
