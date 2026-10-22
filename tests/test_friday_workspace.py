"""Milestone 2: F.R.I.D.A.Y.'s own workspace — direct chat with the same identity and
policy as delegation, separate history, Analyze reports, labelled market data,
real status, and voice scaffolding that stays off."""
from __future__ import annotations

import asyncio
import json

import pytest

from backend import db, providers
from backend.services import agents, chat, data, persona, settings, specialists
from backend.services.specialists import parse_report
from backend.tools.registry import ToolError
from tests.conftest import FakeProvider, call
from tests.test_multi_agent import FRIDAY_TOOLS, GOOD_REPORT, SlowProvider, delegate

USER_PROMPT_MARK = "finance and market-research specialist"


@pytest.fixture
def friday(fake_provider):
    """F.R.I.D.A.Y. on her own connection 'fake2'; E.V. on 'fake'."""
    def make(friday_script, ev_script=None):
        ev = fake_provider(ev_script or [("E.V. here.", [])], pid="fake")
        fr = fake_provider(friday_script, pid="fake2", label="Fake Friday AI", active=False)
        settings.put("agents.friday.connection", "fake2")
        settings.put("agents.friday.model", "fake-friday-model")
        return ev, fr
    return make


async def collect(conv_id, text, on_event=None, agent=None):
    out = []
    async for ev in chat.stream_chat(conv_id, text, agent):
        out.append(ev)
        if on_event:
            await on_event(ev)
    return out


def sse(resp):
    return [json.loads(line[5:]) for line in resp.text.splitlines() if line.startswith("data:")]


# ------------------------------------------------------------------ direct chat = same F.R.I.D.A.Y.
def test_direct_chat_runs_as_friday_with_her_configuration(friday):
    ev, fr = friday([("", [call("market_quote", {"symbol": "MSFT"}, "q1")]),
                     ("Takeaway: test data only. MSFT synthetic quote shown.", [])])
    conv = chat.create_conversation("friday")
    events = asyncio.run(collect(conv["id"], "What's MSFT trading at?"))
    assert events[0]["type"] == "run" and events[0]["agent"] == "friday" and events[0]["provider"] == "fake2"
    assert events[-1]["type"] == "done" and ev.calls == []          # E.V. was not involved
    assert set(fr.calls[0]["tools"]) == FRIDAY_TOOLS
    system = fr.calls[0]["system"]
    assert USER_PROMPT_MARK in system and "talking directly with the user" in system
    assert "Finish with ONLY a JSON object" not in system and "Style settings" not in system
    [res] = [e for e in events if e["type"] == "tool_result"]
    assert res["status"] == "ok"
    assert db.row("SELECT actor FROM audit_log WHERE tool = 'market_quote'")["actor"] == "agent:friday"


def test_both_entry_points_share_prompt_tools_and_route(friday):
    fr_spec = agents.AGENTS["friday"]
    names = chat._tool_names(fr_spec)
    direct = persona.specialist_system_prompt(fr_spec, names, mode="direct")
    delegated = persona.specialist_system_prompt(fr_spec, names, mode="report", requested_by="ev")
    prefix = direct.split("## Operating rules")[0]
    assert prefix == delegated.split("## Operating rules")[0] and USER_PROMPT_MARK in prefix
    for text in (direct, delegated):
        assert ", ".join(names) in text
    ev, fr = friday([(GOOD_REPORT, [])], ev_script=[("", [delegate()]), ("ok", [])])
    asyncio.run(collect(chat.create_conversation()["id"], "research msft"))
    asyncio.run(collect(chat.create_conversation("friday")["id"], "hi"))
    assert len(fr.calls) == 2 and fr.calls[0]["tools"] == fr.calls[1]["tools"]


def test_friday_conversations_are_separate_from_evs(friday, authed):
    friday([("Noted.", [])])
    fr_conv = authed.post("/api/conversations", json={"agent": "friday"}).json()
    ev_conv = authed.post("/api/conversations", json={"agent": "ev"}).json()
    assert fr_conv["agent"] == "friday" and ev_conv["agent"] == "ev"
    asyncio.run(collect(fr_conv["id"], "my secret thesis about ZETA", agent="ev"))   # asking for E.V. can't switch it
    assert db.row("SELECT agent FROM conversations WHERE id = ?", (fr_conv["id"],))["agent"] == "friday"
    ev_list = {c["id"] for c in authed.get("/api/conversations").json()}
    fr_list = {c["id"] for c in authed.get("/api/conversations?agent=friday").json()}
    assert fr_conv["id"] not in ev_list and ev_conv["id"] in ev_list
    assert fr_list == {fr_conv["id"]}
    assert authed.get("/api/conversations?agent=ultron").status_code == 404


def test_ev_never_sees_friday_conversation_content(friday):
    ev, fr = friday([("Got it.", [])], ev_script=[("Hello.", [])])
    asyncio.run(collect(chat.create_conversation("friday")["id"], "PRIVATE-ZETA-THESIS"))
    asyncio.run(collect(chat.create_conversation()["id"], "hi E.V."))
    assert "PRIVATE-ZETA-THESIS" not in json.dumps(ev.calls)


def test_direct_chat_enforces_tool_list_and_read_only(friday):
    friday([("", [call("create_task", {"title": "sneaky"}, "a"),
                  call("delegate_to_specialist", {"specialist": "friday", "task": "recurse forever"}, "b"),
                  call("save_memory", {"content": "remember this", "category": "finance"}, "c")]),
            ("I can't do those.", [])])
    events = asyncio.run(collect(chat.create_conversation("friday")["id"], "do things"))
    assert [e["status"] for e in events if e["type"] == "tool_result"] == ["denied", "denied", "denied"]
    assert db.scalar("SELECT COUNT(*) FROM tasks") == 0 and db.scalar("SELECT COUNT(*) FROM memories") == 0
    assert not [e for e in events if e["type"] == "approval_required"]


def test_direct_chat_memory_isolation(friday):
    ev, fr = friday([("ok", [])])
    data.add_memory("Has a knee injury", "health")
    data.add_memory("Long-term horizon for index funds", "investing")
    asyncio.run(collect(chat.create_conversation("friday")["id"], "hi"))
    assert "Long-term horizon" in fr.calls[0]["system"] and "knee injury" not in fr.calls[0]["system"]


def test_direct_chat_step_limit(friday, monkeypatch):
    monkeypatch.setattr(agents.AGENTS["friday"], "max_steps", 2)
    friday([("", [call("market_quote", {"symbol": "A"}, "a")]), ("", [call("market_quote", {"symbol": "B"}, "b")])])
    events = asyncio.run(collect(chat.create_conversation("friday")["id"], "x"))
    assert any(e["type"] == "notice" and "F.R.I.D.A.Y. stopped after 2" in e["message"] for e in events)


def test_direct_chat_time_limit(fake_provider, monkeypatch):
    monkeypatch.setattr(agents.AGENTS["friday"], "timeout_s", 0.3)
    fake_provider([("unused", [])])
    slow = SlowProvider()
    providers.register(slow)
    settings.put("agents.friday.connection", "slow")
    try:
        events = asyncio.run(collect(chat.create_conversation("friday")["id"], "x"))
    finally:
        providers._REGISTRY.pop("slow", None)
    assert events[-1]["type"] == "cancelled" and events[-1].get("reason") == "timeout" and slow.cancelled
    assert any(e["type"] == "notice" and "limit" in e["message"] for e in events)


def test_stop_cancels_direct_chat(fake_provider):
    fake_provider([("unused", [])])
    slow = SlowProvider()
    providers.register(slow)
    settings.put("agents.friday.connection", "slow")

    async def stop(ev):
        if ev["type"] == "run":
            assert "friday" in chat.active_agents()
            chat.cancel(ev["run_id"])

    try:
        events = asyncio.run(collect(chat.create_conversation("friday")["id"], "x", stop))
    finally:
        providers._REGISTRY.pop("slow", None)
    assert events[-1]["type"] == "cancelled" and "reason" not in events[-1] and slow.cancelled


def test_direct_chat_uses_ev_connection_when_unassigned(fake_provider):
    p = fake_provider([("hi", [])])
    events = asyncio.run(collect(chat.create_conversation("friday")["id"], "x"))
    assert events[0]["provider"] == "fake" and events[0]["agent"] == "friday"
    assert USER_PROMPT_MARK in p.calls[0]["system"]


# ------------------------------------------------------------------ Analyze reports
REPORT_WITH_SCENARIOS = json.dumps({
    "headline": "TEST DATA: illustrative only", "summary": "Synthetic numbers; not real.", "findings": [],
    "scenarios": [{"case": "Bull", "description": "b", "assumptions": ["x"], "illustrative_change_percent": 18},
                  {"case": "base", "description": "m", "assumptions": [], "illustrative_change_percent": "six"},
                  {"case": "bear", "description": "w", "assumptions": ["y"], "illustrative_change_percent": -15},
                  {"case": "moon", "description": "ignored"}],
    "risks": ["r"], "action_plan": ["Verify with a live source"], "confidence": "low"})


def test_report_parsing_scenarios():
    rep, ok = parse_report(REPORT_WITH_SCENARIOS)
    assert ok and [s.case for s in rep.scenarios] == ["bull", "base", "bear"]
    assert rep.scenarios[1].illustrative_change_percent is None and rep.action_plan == ["Verify with a live source"]


def test_analyze_streams_progress_and_saves_report(friday, authed):
    friday([("", [call("market_quote", {"symbol": "MSFT"}, "q"), call("market_price_history", {"symbol": "MSFT", "range": "6m"}, "h")]),
            (REPORT_WITH_SCENARIOS, [])])
    events = sse(authed.post("/api/agents/friday/analyze", json={"symbol": "msft"}))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "run" and "agent_progress" in kinds and kinds[-2:] == ["report", "done"]
    rep = events[-2]["report"]
    assert rep["requested_by"] == "user" and rep["symbol"] == "MSFT" and rep["data_status"] == "TEST_DATA"
    assert rep["report"]["scenarios"][0]["illustrative_change_percent"] == 18
    listed = authed.get("/api/agents/friday/reports?symbol=MSFT").json()
    assert listed[0]["id"] == rep["report_id"] and listed[0]["requested_by"] == "user"
    full = authed.get(f"/api/agents/friday/reports/{rep['report_id']}").json()
    assert full["provenance"] and all(p["is_live"] is False for p in full["provenance"])
    assert authed.post("/api/agents/friday/analyze", json={"symbol": "bad sym!"}).status_code == 400
    assert authed.post("/api/agents/jarvis/analyze", json={"symbol": "MSFT"}).status_code == 404


def test_delegated_reports_are_stored_for_the_workspace(friday, authed):
    friday([(GOOD_REPORT, [])], ev_script=[("", [delegate()]), ("ok", [])])
    asyncio.run(collect(chat.create_conversation()["id"], "msft?"))
    [r] = authed.get("/api/agents/friday/reports").json()
    assert r["requested_by"] == "ev" and r["data_status"] == "NONE"


def test_run_task_cancellation(fake_provider):
    fake_provider([("unused", [])])
    slow = SlowProvider()
    providers.register(slow)
    settings.put("agents.friday.connection", "slow")

    async def go():
        cancel = asyncio.Event()
        job = asyncio.ensure_future(specialists.run_task(agents.AGENTS["friday"], task="analyze X", requested_by="user",
                                                          conversation_id=None, cancel=cancel))
        await asyncio.sleep(0.1)
        cancel.set()
        with pytest.raises(ToolError) as e:
            await job
        return e.value.status

    try:
        assert asyncio.run(go()) == "cancelled"
    finally:
        providers._REGISTRY.pop("slow", None)
    assert slow.cancelled


# ------------------------------------------------------------------ market data, watchlist, status, voice
def test_market_endpoints_are_labelled(authed):
    q = authed.get("/api/market/quote/msft").json()
    assert q["symbol"] == "MSFT" and q["provenance"]["data_status"] == "TEST_DATA" and q["provenance"]["is_live"] is False
    h = authed.get("/api/market/history/MSFT?range=3m").json()
    assert h["data"]["range"] == "3m" and h["data"]["closes"]
    assert authed.get("/api/market/history/MSFT?range=10y").status_code == 400
    assert authed.get("/api/market/quote/$$$").status_code == 400
    assert authed.get("/api/market/bogus/MSFT").status_code == 404
    settings.put("market_data.source", "none")
    assert authed.get("/api/market/quote/MSFT").status_code == 409


def test_watchlist(authed):
    assert authed.get("/api/market/watchlist").json() == {"symbols": []}
    r = authed.put("/api/market/watchlist", json={"symbols": ["msft", "brk.b", "MSFT"]})
    assert r.json() == {"symbols": ["MSFT", "BRK.B"]}
    assert authed.put("/api/market/watchlist", json={"symbols": ["DROP TABLE"]}).status_code == 400


def test_status_reflects_real_state(friday, authed):
    friday([("hello", [])])
    st = authed.get("/api/agents/friday/status").json()
    assert st["model"]["state"] == "configured" and "not tested" in st["model"]["detail"].lower()
    assert st["working"] is False and st["voice"]["available"] is False and st["voice"]["microphone"] is False
    assert st["paper_trading"]["available"] is False and st["market_data"]["data_status"] == "TEST_DATA"
    assert st["prompt"]["status"] == "custom" and st["limits"]["max_level"] == "read"
    asyncio.run(collect(chat.create_conversation("friday")["id"], "hi"))
    assert authed.get("/api/agents/friday/status").json()["model"]["state"] == "connected"
    settings.put("agents.friday.model", "")
    st = authed.get("/api/agents/friday/status").json()
    assert st["model"]["state"] == "setup_required"


def test_voice_stays_off_without_a_provider(authed):
    settings.put("agents.friday.voice", {"provider": "elevenlabs", "voice_id": "abc123"})
    v = authed.get("/api/agents/friday/voice").json()
    assert v["available"] is False and v["microphone"] is False and v["voice_id_set"] is True
    assert "Irish" in v["intended_style"]
    with pytest.raises(ValueError):
        settings.put("agents.friday.voice", {"provider": "x", "api_key": "secret"})


def test_status_and_reports_never_contain_keys(friday, authed):
    key = "testkey-friday-00000000000004444"
    c = authed.post("/api/connections", json={"name": "Friday key", "preset": "openai", "api_key": key, "default_model": "m"}).json()
    authed.put("/api/agents/friday/routing", json={"connection": c["id"]})
    for path in ("/api/agents/friday/status", "/api/agents/friday/reports", "/api/conversations?agent=friday",
                 "/api/agents/friday/voice"):
        assert key not in authed.get(path).text


def test_search_marks_conversation_owner(friday, authed):
    friday([("ok", [])])
    conv = chat.create_conversation("friday")
    asyncio.run(collect(conv["id"], "searchable-zeta-phrase"))
    hits = authed.get("/api/search?q=searchable-zeta").json()["conversations"]
    assert hits and hits[0]["agent"] == "friday"
