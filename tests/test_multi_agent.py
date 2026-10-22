"""Milestone 1: hub-and-spoke delegation to F.R.I.D.A.Y., per-agent routing,
multiple keys per provider, fallbacks, memory isolation, and secret handling."""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from backend import config, db, providers
from backend.providers import presets
from backend.providers.base import ProviderError
from backend.services import agents, chat, connections, data, persona, prompts, routing, settings
from backend.services.delegation import collect_provenance, parse_report
from backend.tools import registry
from backend.tools.registry import TOOLS, Level, ToolContext, ToolError
from tests.conftest import FakeProvider, call

FRIDAY_TOOLS = {"market_quote", "market_company_overview", "market_price_history", "market_news"}
GOOD_REPORT = json.dumps({
    "summary": "MSFT test-data quote is shown; numbers are synthetic.", "findings": [
        {"point": "Price is in the test data", "basis": "tool_data", "source_ref": "market_quote MSFT"}],
    "data_used": [], "assumptions": ["Test data only"], "risks": ["Not real data"], "confidence": "low",
    "open_questions": [], "proposed_actions": []})


async def collect(conv_id, text, on_event=None):
    events = []
    async for ev in chat.stream_chat(conv_id, text):
        events.append(ev)
        if on_event:
            await on_event(ev)
    return events


@pytest.fixture
def two_agents(fake_provider):
    """E.V. thinks with 'fake'; F.R.I.D.A.Y. is assigned 'fake2'."""
    def make(ev_script, friday_script):
        ev = fake_provider(ev_script, pid="fake")
        fr = fake_provider(friday_script, pid="fake2", label="Fake Friday AI", active=False)
        settings.put("agents.friday.connection", "fake2")
        settings.put("agents.friday.model", "fake-friday-model")
        return ev, fr
    return make


def delegate(task="Research MSFT: quote and company overview.", cid="d1"):
    return call("delegate_to_specialist", {"specialist": "friday", "task": task}, cid)


def results(events, name="delegate_to_specialist"):
    return [e for e in events if e["type"] == "tool_result" and e["name"] == name]


# ------------------------------------------------------------------ delegation flow
def test_ev_delegates_to_friday_and_receives_labelled_report(two_agents):
    ev, fr = two_agents(
        [("Let me ask F.R.I.D.A.Y.", [delegate()]), ("F.R.I.D.A.Y.'s report uses TEST DATA only…", [])],
        [("", [call("market_quote", {"symbol": "msft"}, "q1"), call("market_company_overview", {"symbol": "MSFT"}, "q2")]),
         (GOOD_REPORT, [])])
    conv = chat.create_conversation()
    events = asyncio.run(collect(conv["id"], "How is Microsoft stock doing?"))
    assert events[-1]["type"] == "done"

    [res] = results(events)
    assert res["status"] == "ok"
    card = res["report"]
    assert card["agent"] == "friday" and card["status"] == "completed"
    assert card["data_status"] == "TEST_DATA" and "TEST DATA" in card["data_notice"]
    assert card["sources"] and all(s["is_live"] is False for s in card["sources"])

    progress = [e for e in events if e["type"] == "agent_progress"]
    assert progress[0]["stage"] == "started" and progress[-1]["stage"] == "finished"
    assert {e["name"] for e in progress if e.get("event") == "tool"} == {"market_quote", "market_company_overview"}

    # F.R.I.D.A.Y. ran on her own connection with only her tools, and her prompt file + code rules.
    assert set(fr.calls[0]["tools"]) == FRIDAY_TOOLS
    sysmsg = fr.calls[0]["system"]
    assert "F.R.I.D.A.Y." in sysmsg and "Operating rules" in sysmsg and "read-only" in sysmsg
    # E.V. never gets market tools directly, but does get the delegation tool.
    assert "delegate_to_specialist" in ev.calls[0]["tools"]
    assert not FRIDAY_TOOLS & set(ev.calls[0]["tools"])
    # E.V.'s second call sees the code-built provenance.
    tool_msg = [m for m in ev.calls[1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert '"data_status": "TEST_DATA"' in tool_msg and "synthetic" in tool_msg.lower()

    rows = db.rows("SELECT actor, tool, status FROM audit_log WHERE actor = 'agent:friday'")
    assert {(r["tool"], r["status"]) for r in rows} == {("market_quote", "ok"), ("market_company_overview", "ok")}


def test_test_data_is_never_labelled_live():
    from backend.integrations import market_data
    src = market_data.active()
    assert src.id == "test" and src.is_live is False
    res = asyncio.run(TOOLS["market_quote"].handler(TOOLS["market_quote"].args_model(symbol="AAPL"), ToolContext()))
    assert res["provenance"]["data_status"] == "TEST_DATA" and res["provenance"]["is_live"] is False
    ov = asyncio.run(TOOLS["market_company_overview"].handler(TOOLS["market_company_overview"].args_model(symbol="AAPL"), ToolContext()))
    assert "TEST DATA" in ov["data"]["name"] and "not a real company" in ov["data"]["name"]
    news = asyncio.run(TOOLS["market_news"].handler(TOOLS["market_news"].args_model(symbol="AAPL", limit=3), ToolContext()))
    assert all(a["headline"].startswith("[TEST DATA]") and a["url"] is None for a in news["data"]["articles"])


def test_report_without_market_data_is_flagged_as_general_knowledge(two_agents):
    two_agents([("", [delegate()]), ("ok", [])], [(GOOD_REPORT, [])])
    events = asyncio.run(collect(chat.create_conversation()["id"], "msft?"))
    card = results(events)[0]["report"]
    assert card["data_status"] == "NONE" and "general knowledge" in card["data_notice"]


def test_market_source_off_means_setup_required(two_agents):
    settings.put("market_data.source", "none")
    two_agents([("", [delegate()]), ("ok", [])],
               [("", [call("market_quote", {"symbol": "MSFT"}, "q1")]), (GOOD_REPORT, [])])
    events = asyncio.run(collect(chat.create_conversation()["id"], "msft?"))
    assert results(events)[0]["report"]["data_status"] == "NONE"
    row = db.row("SELECT status, detail FROM audit_log WHERE tool = 'market_quote'")
    assert row["status"] == "unavailable"


# ------------------------------------------------------------------ permissions
def test_specialist_cannot_use_unlisted_tools_or_delegate(two_agents):
    two_agents([("", [delegate()]), ("ok", [])],
               [("", [call("create_task", {"title": "sneaky"}, "x1"),
                      call("delegate_to_specialist", {"specialist": "friday", "task": "loop forever"}, "x2"),
                      call("send_email", {"to": "a@b.c", "subject": "s", "body": "b"}, "x3")]),
                (GOOD_REPORT, [])])
    events = asyncio.run(collect(chat.create_conversation()["id"], "go"))
    [res] = results(events)
    assert res["status"] == "ok"
    tool_msg = db.rows("SELECT content FROM messages WHERE role = 'tool'")[-1]["content"]
    used = json.loads(tool_msg)["result"]["tools_used"]
    assert [u["status"] for u in used] == ["denied", "denied", "denied"]
    assert db.scalar("SELECT COUNT(*) FROM tasks") == 0


def test_ev_cannot_call_specialist_tools_directly(fake_provider):
    fake_provider([("", [call("market_quote", {"symbol": "MSFT"})]), ("ok", [])])
    events = asyncio.run(collect(chat.create_conversation()["id"], "quote"))
    [res] = results(events, "market_quote")
    assert res["status"] == "denied"


def test_delegation_refused_outside_evs_own_turn():
    args = TOOLS["delegate_to_specialist"].args_model(specialist="friday", task="research MSFT")
    handler = TOOLS["delegate_to_specialist"].handler
    for ctx in (ToolContext(actor="agent:friday", agent="friday", depth=1),
                ToolContext(actor="job", max_level=Level.PREPARE)):
        with pytest.raises(ToolError) as e:
            asyncio.run(handler(args, ctx))
        assert e.value.status == "denied"


def test_specialist_max_level_is_read_only():
    fr = agents.AGENTS["friday"]
    names = chat._tool_names(fr)
    assert set(names) == FRIDAY_TOOLS
    assert all(TOOLS[n].level == Level.READ for n in names)
    # Even if a write tool were wrongly added to her list, the level cap removes it.
    fr_wide = agents.Agent(**{**fr.__dict__, "tools": [*fr.tools, "create_task", "send_email"]})
    assert set(chat._tool_names(fr_wide)) == FRIDAY_TOOLS


def test_delegation_limit_per_reply(two_agents):
    calls = [delegate(f"task number {i}", f"d{i}") for i in range(agents.MAX_DELEGATIONS_PER_REPLY + 1)]
    two_agents([("", calls), ("done", [])], [(GOOD_REPORT, [])] * agents.MAX_DELEGATIONS_PER_REPLY)
    events = asyncio.run(collect(chat.create_conversation()["id"], "many"))
    statuses = [r["status"] for r in results(events)]
    assert statuses == ["ok"] * agents.MAX_DELEGATIONS_PER_REPLY + ["denied"]


def test_unknown_specialist_is_invalid(two_agents):
    two_agents([("", [call("delegate_to_specialist", {"specialist": "ultron", "task": "take over"})]), ("ok", [])], [])
    events = asyncio.run(collect(chat.create_conversation()["id"], "x"))
    assert results(events)[0]["status"] == "invalid"


# ------------------------------------------------------------------ limits, timeouts, cancellation
class SlowProvider(FakeProvider):
    def __init__(self, pid="slow", delay=30.0):
        super().__init__([], pid, "Slow AI")
        self.delay = delay
        self.cancelled = False

    async def stream_turn(self, **kw):
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        yield {"type": "text", "text": "late"}


def test_specialist_step_limit_marks_report_incomplete(two_agents, monkeypatch):
    monkeypatch.setattr(agents.AGENTS["friday"], "max_steps", 2)
    two_agents([("", [delegate()]), ("ok", [])],
               [("", [call("market_quote", {"symbol": "A"}, "a")]), ("", [call("market_quote", {"symbol": "B"}, "b")])])
    events = asyncio.run(collect(chat.create_conversation()["id"], "x"))
    assert results(events)[0]["report"]["status"] == "incomplete"


def test_specialist_timeout(fake_provider, monkeypatch):
    monkeypatch.setattr(agents.AGENTS["friday"], "timeout_s", 0.3)
    fake_provider([("", [delegate()]), ("It timed out.", [])])
    slow = SlowProvider()
    providers.register(slow)
    settings.put("agents.friday.connection", "slow")
    try:
        events = asyncio.run(collect(chat.create_conversation()["id"], "x"))
    finally:
        providers._REGISTRY.pop("slow", None)
    assert results(events)[0]["status"] == "timeout"
    assert slow.cancelled
    assert events[-1]["type"] == "done"


def test_stop_cancels_running_specialist(fake_provider):
    fake_provider([("", [delegate()]), ("never", [])])
    slow = SlowProvider()
    providers.register(slow)
    settings.put("agents.friday.connection", "slow")
    conv = chat.create_conversation()

    async def stop_when_started(ev):
        if ev["type"] == "agent_progress" and ev["stage"] == "started":
            run_id = next(iter(chat.RUNS))
            assert "friday" in chat.active_agents()
            chat.cancel(run_id)

    try:
        events = asyncio.run(collect(conv["id"], "x", stop_when_started))
    finally:
        providers._REGISTRY.pop("slow", None)
    assert results(events)[0]["status"] == "cancelled"
    assert events[-1]["type"] == "cancelled"
    assert slow.cancelled
    assert db.row("SELECT status FROM audit_log WHERE tool = 'delegate_to_specialist' ORDER BY id DESC")["status"] == "cancelled"


# ------------------------------------------------------------------ report parsing & provenance
def test_parse_report_variants():
    rep, ok = parse_report("```json\n" + GOOD_REPORT + "\n```")
    assert ok and rep.confidence == "low" and rep.findings[0].basis == "tool_data"
    rep, ok = parse_report('Here you go: {"summary": "x", "confidence": "HIGH", "findings": ["bad", {"point": "p"}]}')
    assert ok and rep.confidence == "high" and len(rep.findings) == 1
    rep, ok = parse_report("plain text, no json")
    assert not ok and rep.summary == "plain text, no json"


def test_provenance_comes_from_tool_results_not_the_model():
    trace = [{"tool": "market_quote", "status": "ok", "result": {"symbol": "X", "provenance": {
        "source_id": "test", "source": "gen", "data_status": "TEST_DATA", "is_live": False, "as_of": "t"}}},
             {"tool": "market_news", "status": "denied", "error": "no"}]
    prov, status, notice = collect_provenance(trace)
    assert status == "TEST_DATA" and len(prov) == 1 and "not real" in notice
    live = [{"tool": "market_quote", "status": "ok", "result": {"symbol": "X", "provenance": {
        "source_id": "live", "source": "Some Feed", "data_status": "LIVE", "is_live": True, "as_of": "2026-10-21T15:00:00Z"}}}]
    assert collect_provenance(live)[1] == "LIVE" and "2026-10-21T15:00:00Z" in collect_provenance(live)[2]


# ------------------------------------------------------------------ memory isolation
def test_specialist_sees_only_permitted_memories():
    health = data.add_memory("Has a knee injury", "health")
    fin = data.add_memory("Saving for a down payment", "finance")
    own = data.add_memory("Prefers dividend stocks", "investing", scope="friday")
    general = data.add_memory("Likes metric units", "preferences")
    fr = agents.AGENTS["friday"]
    sp = persona.specialist_system_prompt(fr, chat._tool_names(fr))
    assert fin["content"] in sp and own["content"] in sp
    assert health["content"] not in sp and general["content"] not in sp
    ev_prompt = persona.system_prompt("ev", [])
    assert all(m["content"] in ev_prompt for m in (health, fin, own, general))
    # Tool-level filtering too.
    out = TOOLS["search_memories"].handler(TOOLS["search_memories"].args_model(query="e"), ToolContext(agent="friday"))
    assert {m["id"] for m in out["memories"]} == {fin["id"], own["id"]}


def test_memory_scope_must_be_shared_or_a_specialist(authed):
    assert authed.post("/api/memories", json={"content": "x", "scope": "friday"}).status_code == 200
    r = authed.post("/api/memories", json={"content": "y", "scope": "ultron"})
    assert r.status_code == 400


# ------------------------------------------------------------------ connections & keys
def test_multiple_keys_for_one_provider(authed, tmp_path):
    k1, k2 = "testkey-alpha-0000000000001111", "testkey-bravo-0000000000002222"
    a = authed.post("/api/connections", json={"name": "OpenAI personal", "preset": "openai", "api_key": k1, "default_model": "m1"})
    b = authed.post("/api/connections", json={"name": "OpenAI school", "preset": "openai", "api_key": k2, "default_model": "m2"})
    assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
    ca, cb = a.json(), b.json()
    assert ca["id"] != cb["id"] and ca["key_hint"] == "••••1111" and cb["key_hint"] == "••••2222"
    pa, pb = connections.provider_for(ca["id"]), connections.provider_for(cb["id"])
    assert pa._headers()["Authorization"] == f"Bearer {k1}" and pb._headers()["Authorization"] == f"Bearer {k2}"
    assert connections.family(ca["id"]) == connections.family(cb["id"]) == "openai"
    # Keys are in .env under per-connection names, and nowhere else.
    env = config.ENV_FILE.read_text()
    assert f"{connections.key_env(ca['id'])}={k1}" in env and f"{connections.key_env(cb['id'])}={k2}" in env
    for resp in (authed.get("/api/connections"), authed.get("/api/settings"), authed.get("/api/agents"),
                 authed.get("/api/audit"), authed.get("/api/agents/friday/routing")):
        assert k1 not in resp.text and k2 not in resp.text
    raw_db = b"".join(p.read_bytes() for p in tmp_path.glob("ev.db*"))
    assert k1.encode() not in raw_db and k2.encode() not in raw_db
    audit = json.dumps(db.rows("SELECT * FROM audit_log"))
    assert k1 not in audit and k2 not in audit


def test_one_connection_shared_by_ev_and_friday(authed):
    c = authed.post("/api/connections", json={"name": "Groq main", "preset": "groq", "api_key": "testkey-groq-000000000000009999",
                                              "default_model": "llama-x"}).json()
    for aid in ("ev", "friday"):
        r = authed.put(f"/api/agents/{aid}/routing", json={"connection": c["id"], "model": ""})
        assert r.status_code == 200, r.text
    assert routing.chain_for("ev")[0].connection == routing.chain_for("friday")[0].connection == c["id"]
    assert routing.chain_for("friday")[0].model == "llama-x"          # connection default
    authed.put("/api/agents/friday/routing", json={"connection": c["id"], "model": "other-model"})
    assert routing.chain_for("friday")[0].model == "other-model"    # per-agent override


def test_routing_validation(authed):
    assert authed.put("/api/agents/friday/routing", json={"connection": "nope"}).status_code == 400
    assert authed.put("/api/agents/coding/routing", json={"connection": "groq"}).status_code == 404
    assert authed.put("/api/agents/friday/routing", json={"connection": "", "fallbacks": [{"connection": "groq"}]}).status_code == 400


def test_unassigned_specialist_inherits_ev_and_ev_keeps_legacy_setup():
    settings.put("llm.provider", "anthropic")
    settings.put("llm.fallback_provider", "groq")
    assert [r.connection for r in routing.chain_for("ev")] == ["anthropic", "groq"]
    assert [r.connection for r in routing.chain_for("friday")] == ["anthropic", "groq"]


def test_deleting_connection_removes_key_and_assignments(authed):
    c = authed.post("/api/connections", json={"name": "Temp", "preset": "openai", "api_key": "testkey-temp-0000000000003333"}).json()
    authed.put("/api/agents/friday/routing", json={"connection": c["id"]})
    assert authed.delete(f"/api/connections/{c['id']}").status_code == 200
    assert config.get_secret(connections.key_env(c["id"])) is None
    assert "testkey-temp" not in config.ENV_FILE.read_text()
    assert settings.get("agents.friday.connection") == ""


def test_connection_validation(authed):
    bad = [
        {"name": "x", "preset": "azure_openai"},                                   # needs an adapter
        {"name": "x", "preset": "nope"},
        {"name": "x", "preset": "custom_openai"},                                  # base URL required
        {"name": "x", "preset": "custom_openai", "base_url": "http://api.example.com/v1"},  # http on the internet
        {"name": "x", "preset": "openai", "base_url": "https://evil.example.com"},  # fixed-address preset
        {"name": "x", "preset": "openai", "api_key": "has spaces in it"},
    ]
    for body in bad:
        assert authed.post("/api/connections", json=body).status_code == 400, body
    ok = authed.post("/api/connections", json={"name": "Home LLM", "preset": "custom_openai", "base_url": "http://192.168.1.50:8080/v1"})
    assert ok.status_code == 200 and ok.json()["family"] == "host:192.168.1.50"
    assert authed.get("/api/connections").json()["presets"]


def test_secrets_stay_out_of_prompts_and_logs(two_agents, authed, caplog):
    key = "testkey-secret-0000000000007777"
    caplog.set_level(logging.DEBUG)
    authed.post("/api/connections", json={"name": "Leak check", "preset": "openai", "api_key": key})
    ev, fr = two_agents([("", [delegate()]), ("ok", [])], [(GOOD_REPORT, [])])
    asyncio.run(collect(chat.create_conversation()["id"], "x"))
    everything = json.dumps(ev.calls + fr.calls) + caplog.text
    assert key not in everything
    assert key not in json.dumps(db.rows("SELECT * FROM messages"))


# ------------------------------------------------------------------ fallbacks
@pytest.fixture
def scripted_connections(monkeypatch):
    """Custom connections whose adapter is a scripted fake, keyed by connection id."""
    fakes: dict[str, FakeProvider] = {}
    presets.register(presets.Preset("fake_a", "Fake vendor A", "scripted", "https://a.example/v1"))
    presets.register(presets.Preset("fake_b", "Fake vendor B", "scripted", "https://b.example/v1"))
    monkeypatch.setitem(connections.ADAPTERS, "scripted", lambda conn, preset, key: fakes[conn["id"]])

    def make(name, preset, script):
        c = connections.create(name=name, preset=preset, api_key="testkey-x-000000000000000001", default_model="m")
        fakes[c["id"]] = FakeProvider(script, c["id"], name)
        return c["id"], fakes[c["id"]]
    yield make
    for pid in ("fake_a", "fake_b"):
        presets._PRESETS.pop(pid, None)


def test_rate_limit_never_falls_back_to_another_key_of_same_provider(scripted_connections):
    a, pa = scripted_connections("A key 1", "fake_a", [ProviderError("rate_limit", "429", retry_after=5)])
    b, pb = scripted_connections("A key 2", "fake_a", [("from second key", [])])
    c, pc = scripted_connections("B key", "fake_b", [("from other vendor", []), ("again", [])])
    settings.put("agents.ev.connection", a)
    settings.put("agents.ev.fallbacks", [{"connection": b, "model": ""}, {"connection": c, "model": ""}])
    events = asyncio.run(collect(chat.create_conversation()["id"], "hi"))
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert text == "from other vendor" and pb.calls == []
    assert any(e["type"] == "notice" and "B key" in e["message"] for e in events)
    # Cooldown: the next reply skips the whole rate-limited family up front.
    asyncio.run(collect(chat.create_conversation()["id"], "hi again"))
    assert len(pa.calls) == 1 and pb.calls == [] and len(pc.calls) == 2


def test_outage_may_fall_back_within_same_provider(scripted_connections):
    a, _ = scripted_connections("A key 1", "fake_a", [ProviderError("server", "HTTP 503")])
    b, pb = scripted_connections("A key 2", "fake_a", [("second key ok", [])])
    settings.put("agents.ev.connection", a)
    settings.put("agents.ev.fallbacks", [{"connection": b, "model": ""}])
    events = asyncio.run(collect(chat.create_conversation()["id"], "hi"))
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "second key ok"


def test_all_connections_cooling_down_gives_clear_error(scripted_connections):
    a, _ = scripted_connections("Only", "fake_a", [ProviderError("rate_limit", "429", retry_after=60)])
    settings.put("agents.ev.connection", a)
    asyncio.run(collect(chat.create_conversation()["id"], "hi"))
    events = asyncio.run(collect(chat.create_conversation()["id"], "again"))
    assert events[-1]["type"] == "error" and "cooling down" in events[-1]["message"]


def test_specialist_falls_back_on_its_own_chain(two_agents, scripted_connections):
    a, _ = scripted_connections("F main", "fake_a", [ProviderError("connection", "down")])
    c, pc = scripted_connections("F backup", "fake_b", [(GOOD_REPORT, [])])
    two_agents([("", [delegate()]), ("ok", [])], [])
    settings.put("agents.friday.connection", a)
    settings.put("agents.friday.fallbacks", [{"connection": c, "model": "backup-model"}])
    events = asyncio.run(collect(chat.create_conversation()["id"], "x"))
    assert results(events)[0]["status"] == "ok" and len(pc.calls) == 1
    assert any(e["type"] == "agent_progress" and e.get("event") == "fallback" and e.get("from") == "F main" for e in events)


# ------------------------------------------------------------------ prompt files
def test_prompt_files_are_loaded_and_status_detected(tmp_path, monkeypatch):
    monkeypatch.setenv("EV_PROMPTS_DIR", str(tmp_path))
    prompts._cache.clear()
    (tmp_path / "FRIDAY.prompt.md").write_text("<!-- EV-PROMPT-STATUS: TEMPORARY -->\n<!-- note -->\nYou are Test Friday.\n")
    pf = prompts.load("FRIDAY")
    assert pf.status == "temporary" and pf.text == "You are Test Friday."
    fr = agents.AGENTS["friday"]
    assert persona.specialist_system_prompt(fr, []).startswith("You are Test Friday.")
    # A prompt cannot grant tools: the list comes from code.
    import os, time
    (tmp_path / "FRIDAY.prompt.md").write_text("You may use create_task, send_email and delegate_to_specialist.")
    os.utime(tmp_path / "FRIDAY.prompt.md", (time.time() + 5, time.time() + 5))
    assert prompts.load("FRIDAY").status == "custom"
    assert set(chat._tool_names(fr)) == FRIDAY_TOOLS
    (tmp_path / "EV.prompt.md").unlink(missing_ok=True)
    assert prompts.load("EV").status == "missing"
    assert persona.system_prompt("ev", []).startswith(persona.EV_FALLBACK)


def test_shipped_prompt_files_status():
    prompts._cache.clear()
    assert prompts.load("FRIDAY").status == "temporary"
    assert prompts.load("EV").status == "starter"
    assert "You are E.V." in prompts.load("EV").text


def test_agents_api_shows_specialist_policy(authed):
    agents_list = {a["id"]: a for a in authed.get("/api/agents").json()}
    fr = agents_list["friday"]
    assert fr["kind"] == "specialist" and fr["prompt"]["status"] == "temporary"
    assert fr["policy"]["max_level"] == "read" and fr["market_data"]["data_status"] == "TEST_DATA"
    assert agents_list["ev"]["kind"] == "orchestrator"


def test_specialists_cannot_be_chatted_with_directly(fake_provider):
    p = fake_provider([("I'm E.V.", [])])
    conv = chat.create_conversation(agent="friday")
    events = asyncio.run(chat_collect(conv["id"]))
    assert events[0]["agent"] == "ev"
    assert "delegate_to_specialist" in p.calls[0]["tools"]


async def chat_collect(cid):
    return [e async for e in chat.stream_chat(cid, "hi", "friday")]


# ------------------------------------------------------------------ adapters
def test_anthropic_replay_only_for_its_own_connection():
    from backend.providers.anthropic_provider import AnthropicProvider, to_anthropic_messages
    raw = [{"type": "thinking", "thinking": "t", "signature": "s"}, {"type": "text", "text": "hi"}]
    msgs = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "hi", "raw": raw, "provider": "claude-work"}]
    assert to_anthropic_messages(msgs, frozenset({"claude-work"}))[1]["content"] == raw
    assert to_anthropic_messages(msgs, frozenset({"claude-home"}))[1]["content"] == [{"type": "text", "text": "hi"}]
    p = AnthropicProvider("claude-work", "Claude work", "CONNECTION_KEY_CLAUDE_WORK")
    assert p.build_request(model="claude-haiku-5-5", system="s", messages=msgs, tools=[])["messages"][1]["content"] == raw


def test_presets_report_honest_support_levels():
    by_id = {p.id: p for p in presets.all_presets()}
    assert by_id["openai"].verification == "mock_tested"
    assert by_id["xai"].verification == "docs_checked"
    assert not by_id["bedrock"].supported and not by_id["azure_openai"].supported
