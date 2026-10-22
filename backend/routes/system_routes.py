from __future__ import annotations

import asyncio
import time
import uuid
from datetime import timezone
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import config, db, providers
from ..integrations import email_smtp, firetv, github, market_data, system_monitor, voice, weather
from ..providers import presets
from ..providers.base import ProviderError
from ..services import agents, briefing, chat, connections, data, files, prompts, routing, settings, status
from ..tools import registry

router = APIRouter(prefix="/api", tags=["system"])
VERSION = "1.0.0"
_models_cache: dict[str, list[str]] = {}
SECRET_CHECKS = {
    "GROQ_API_KEY": ["llm:groq", "stt"], "OPENAI_API_KEY": ["llm:openai"], "ANTHROPIC_API_KEY": ["llm:anthropic"],
    "GEMINI_API_KEY": ["llm:gemini"], "OPENROUTER_API_KEY": ["llm:openrouter"], "ELEVENLABS_API_KEY": ["elevenlabs"],
    "GITHUB_TOKEN": ["github"], "SMTP_PASSWORD": ["email"],
}

PLANNED = [
    {"id": "google_calendar", "name": "Google Calendar sync", "category": "Calendar"},
    {"id": "gmail", "name": "Gmail inbox (read & label)", "category": "Communication"},
    {"id": "web_search", "name": "Web search", "category": "Research"},
    {"id": "browser", "name": "Browser automation", "category": "Agents"},
    {"id": "phone", "name": "Phone calls & SMS", "category": "Communication"},
    {"id": "commerce", "name": "Shopping carts & bookings", "category": "Commerce"},
]


@router.get("/health")
def health():
    return {"ok": True, "app": "E.V.", "version": VERSION}


# ------------------------------------------------------------- providers
def provider_status(pid: str) -> dict[str, Any]:
    p = providers.get(pid)
    configured = p.configured()
    setup = f"Paste your {p.needs_key} in AI Core (or Settings), then Test." if p.needs_key else "Install and start Ollama, then Test."
    st = status.state(f"llm:{pid}", configured, setup)
    model = settings.model_for(pid)
    if st["status"] in ("configured", "connected") and not model:
        st = {"status": "setup_required", "detail": "Choose a model (Test → pick from the list)."}
    return {"id": pid, "label": p.label, "needs_key": p.needs_key, "key_hint": config.secret_hint(p.needs_key) if p.needs_key else None,
            "model": model, "models": _models_cache.get(pid, []), "active": settings.get("llm.provider") == pid,
            "fallback": settings.get("llm.fallback_provider") == pid, **st}


@router.get("/providers")
def list_providers():
    return [provider_status(pid) for pid in providers.all_providers()]


@router.post("/providers/{pid}/test")
async def test_provider(pid: str):
    try:
        p = providers.get(pid)
        models = await p.list_models()
    except ProviderError as e:
        status.record(f"llm:{pid}", False, e.message)
        return {**provider_status(pid), "error": e.message}
    _models_cache[pid] = models
    current = settings.model_for(pid)
    note = f"{len(models)} models available"
    if current and models and current not in models:
        note += f" — '{current}' is not in the list; pick another"
        status.record(f"llm:{pid}", False, f"Model '{current}' isn't available on {p.label}. Pick one from the list.")
    else:
        status.record(f"llm:{pid}", True, note)
    return provider_status(pid)


class ProviderSelect(BaseModel):
    model: str | None = Field(None, max_length=200)
    active: bool | None = None
    fallback: bool | None = None


@router.post("/providers/{pid}")
def configure_provider(pid: str, body: ProviderSelect):
    providers.get(pid)
    if body.model is not None:
        settings.put(f"llm.model.{pid}", body.model.strip())
    if body.active:
        settings.put("llm.provider", pid)
        if settings.get("llm.fallback_provider") == pid:
            settings.put("llm.fallback_provider", "")
    if body.fallback is not None:
        settings.put("llm.fallback_provider", pid if body.fallback and settings.get("llm.provider") != pid else "")
    return provider_status(pid)


# ------------------------------------------------------------- named AI connections
@router.get("/connections")
def list_connections():
    return {"connections": connections.list_all(), "presets": [p.public() for p in presets.all_presets()]}


class ConnectionIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    preset: str = Field(..., max_length=40)
    base_url: str | None = Field(None, max_length=300)
    api_key: str | None = Field(None, max_length=512)
    default_model: str = Field("", max_length=200)
    temperature: float | None = Field(None, ge=0, le=2)


class ConnectionPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    base_url: str | None = Field(None, max_length=300)
    api_key: str | None = Field(None, max_length=512)   # "" removes the key
    default_model: str | None = Field(None, max_length=200)
    temperature: float | None = Field(None, ge=0, le=2)
    clear_temperature: bool = False


def _conn_guard(fn):
    try:
        return fn()
    except LookupError as e:
        raise HTTPException(404, str(e).strip("'\"")) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/connections")
def create_connection(body: ConnectionIn):
    c = _conn_guard(lambda: connections.create(name=body.name, preset=body.preset, base_url=body.base_url,
                                               api_key=(body.api_key or "").strip() or None,
                                               default_model=body.default_model, temperature=body.temperature))
    registry.audit(actor="user", tool_name="create_connection", level="reversible_write",
                   args={"id": c["id"], "preset": body.preset, "base_url": c["base_url"], "key_saved": bool(body.api_key)}, status="ok")
    return c


@router.patch("/connections/{cid}")
def update_connection(cid: str, body: ConnectionPatch):
    c = _conn_guard(lambda: connections.update(cid, name=body.name, base_url=body.base_url, default_model=body.default_model,
                                               temperature=body.temperature, clear_temperature=body.clear_temperature,
                                               api_key=None if body.api_key is None else body.api_key.strip()))
    changed = [k for k, v in body.model_dump(exclude_unset=True).items() if k != "api_key"]
    registry.audit(actor="user", tool_name="update_connection", level="reversible_write",
                   args={"id": cid, "changed": changed, "key_changed": body.api_key is not None}, status="ok")
    return c


@router.delete("/connections/{cid}")
def delete_connection(cid: str):
    _conn_guard(lambda: connections.delete(cid))
    registry.audit(actor="user", tool_name="delete_connection", level="external", args={"id": cid}, status="ok")
    return {"ok": True}


@router.post("/connections/{cid}/test")
async def test_connection(cid: str):
    if not connections.exists(cid):
        raise HTTPException(404, f"Connection '{cid}' not found.")
    if connections.get(cid) is None:          # built-in: same as the provider card's Test button
        return {"connection": connections.public(cid), "result": await test_provider(cid)}
    try:
        models = await connections.provider_for(cid).list_models()
    except ProviderError as e:
        status.record(f"llm:{cid}", False, e.message)
        return {"connection": connections.public(cid), "error": e.message}
    connections.set_models(cid, models)
    status.record(f"llm:{cid}", True, f"{len(models)} models available")
    return {"connection": connections.public(cid), "detail": f"{len(models)} models available"}


# ------------------------------------------------------------- per-agent routing
class FallbackIn(BaseModel):
    connection: str = Field(..., max_length=60)
    model: str = Field("", max_length=200)


class RoutingIn(BaseModel):
    connection: str = Field("", max_length=60)      # "" = inherit (E.V.: the active provider in AI Core)
    model: str = Field("", max_length=200)
    fallbacks: list[FallbackIn] = Field(default_factory=list, max_length=4)


def _routable(aid: str) -> agents.Agent:
    a = agents.AGENTS.get(aid)
    if a is None or not a.implemented or a.kind == "mode":
        raise HTTPException(404, "Only E.V. and specialists have their own model assignment (modes use E.V.'s).")
    return a


@router.get("/agents/{aid}/routing")
def get_routing(aid: str):
    _routable(aid)
    return routing.describe(aid)


@router.put("/agents/{aid}/routing")
def put_routing(aid: str, body: RoutingIn):
    _routable(aid)
    for cid in [body.connection] + [f.connection for f in body.fallbacks]:
        if cid and not connections.exists(cid):
            raise HTTPException(400, f"Unknown connection '{cid}'.")
    if not body.connection and body.fallbacks:
        raise HTTPException(400, "Choose a main connection before adding fallbacks.")
    settings.put(f"agents.{aid}.connection", body.connection)
    settings.put(f"agents.{aid}.model", body.model.strip())
    settings.put(f"agents.{aid}.fallbacks", [f.model_dump() for f in body.fallbacks])
    registry.audit(actor="user", tool_name="set_agent_routing", level="reversible_write",
                   args={"agent": aid, **body.model_dump()}, status="ok")
    return routing.describe(aid)


# ------------------------------------------------------------- market data (separate from AI connections)
@router.get("/market-data")
def get_market_data():
    return market_data.status()


# ------------------------------------------------------------- settings & secrets
@router.get("/settings")
def get_settings():
    return {"settings": settings.all_settings(),
            "secrets": [{"name": k, "label": v, "configured": bool(config.get_secret(k)), "hint": config.secret_hint(k)}
                        for k, v in config.SECRET_NAMES.items()],
            "level_info": registry.LEVEL_INFO, "fts": db.FTS_AVAILABLE}


@router.patch("/settings")
def patch_settings(body: dict[str, Any]):
    errors = {}
    for key, value in body.items():
        try:
            settings.put(key, value)
        except ValueError as e:
            errors[key] = str(e)
    if errors:
        raise HTTPException(400, "; ".join(f"{k}: {v}" for k, v in errors.items()))
    registry.audit(actor="user", tool_name="update_settings", level="reversible_write", args=body, status="ok")
    return settings.all_settings()


class SecretBody(BaseModel):
    value: str = Field(..., min_length=1, max_length=512)


@router.put("/secrets/{name}")
def put_secret(name: str, body: SecretBody):
    try:
        config.set_secret(name, body.value)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    status.clear(SECRET_CHECKS.get(name, []))
    registry.audit(actor="user", tool_name="set_secret", level="reversible_write", args={"name": name}, status="ok")
    return {"name": name, "configured": True, "hint": config.secret_hint(name)}


@router.delete("/secrets/{name}")
def delete_secret(name: str):
    try:
        config.set_secret(name, None)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    status.clear(SECRET_CHECKS.get(name, []))
    registry.audit(actor="user", tool_name="remove_secret", level="reversible_write", args={"name": name}, status="ok")
    return {"name": name, "configured": False}


# ------------------------------------------------------------- integrations
def integrations() -> list[dict[str, Any]]:
    out = []

    def add(iid: str, name: str, category: str, st: dict[str, Any], **extra: Any) -> None:
        out.append({"id": iid, "name": name, "category": category, **st, **extra})

    for p in list_providers():
        add(f"llm:{p['id']}", p["label"], "AI models", {"status": p["status"], "detail": p["detail"]},
            active=p["active"], model=p["model"])
    tts = voice.tts_engine()
    add("elevenlabs", "ElevenLabs voice", "Voice",
        status.state("elevenlabs", bool(config.get_secret("ELEVENLABS_API_KEY")),
                     "Add ELEVENLABS_API_KEY. Until then E.V. uses your browser's built-in voice."),
        engine=tts)
    add("stt", "Speech recognition (Groq Whisper)", "Voice",
        status.state("stt", bool(config.get_secret("GROQ_API_KEY")),
                     "Uses the browser's recognizer until GROQ_API_KEY is set."), engine=voice.stt_engine())
    add("weather", "Weather (Open-Meteo, no key)", "Live data",
        status.state("weather", weather.configured(), "Set your city in Settings → Integrations."),
        location=settings.get("weather.label"))
    add("github", "GitHub review requests", "Live data",
        status.state("github", github.configured(), "Add GITHUB_TOKEN (read-only fine-grained token)."))
    add("email", "Email sending (SMTP)", "Communication",
        status.state("email", email_smtp.configured(), "Add SMTP host, user, and SMTP_PASSWORD."))
    add("firetv", "Fire TV remote (home network)", "Home",
        status.state("firetv", firetv.configured(), firetv.setup_issue() or ""), host=settings.get("firetv.host"))
    folders = files.list_folders()
    add("folders", "Laptop folders", "Files",
        {"status": "connected" if folders else "setup_required",
         "detail": f"{len(folders)} folder(s) authorized" if folders else "Authorize at least one folder."})
    add("calendar_local", "Local calendar & tasks", "Calendar", {"status": "connected", "detail": "Built in, stored on this laptop."})
    add("knowledge", "Knowledge Base", "Files", {"status": "connected",
        "detail": f"{db.scalar('SELECT COUNT(*) FROM documents')} document(s) · {'full-text' if db.FTS_AVAILABLE else 'basic'} search"})
    for p in PLANNED:
        add(p["id"], p["name"], p["category"], {"status": "not_implemented", "detail": "Planned — not built yet."})
    return out


@router.get("/integrations")
def get_integrations():
    return integrations()


@router.post("/integrations/{iid}/test")
async def test_integration(iid: str):
    try:
        if iid == "weather":
            return {"ok": True, "result": await weather.current(force=True)}
        if iid == "github":
            return {"ok": True, "result": await github.review_requests(force=True)}
        if iid == "elevenlabs":
            size = 0
            async for chunk in voice.elevenlabs_stream("E.V. voice check."):
                size += len(chunk)
            return {"ok": True, "result": f"Received {size} bytes of audio"}
        if iid == "firetv":
            return {"ok": True, "result": await asyncio.to_thread(firetv.tv_status)}
        if iid == "email":
            await asyncio.to_thread(email_smtp.check_login)
            return {"ok": True, "result": "SMTP login OK — nothing was sent."}
    except Exception as e:
        if iid == "email":
            status.record("email", False, f"{type(e).__name__}: {e}")
        return {"ok": False, "error": getattr(e, "message", None) or str(e) or type(e).__name__}
    raise HTTPException(404, "No test available for that integration")


class LocationBody(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)


@router.post("/weather/location")
async def set_location(body: LocationBody):
    try:
        place = await weather.set_location(body.name)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(502, f"Couldn't reach the geocoding service: {type(e).__name__}") from e
    return place


@router.get("/weather")
async def get_weather():
    try:
        return await weather.current()
    except LookupError as e:
        return {"setup_required": True, "message": str(e)}
    except Exception as e:
        return {"error": True, "message": f"Weather unavailable ({type(e).__name__})"}


# ------------------------------------------------------------- tools & agents
@router.get("/tools")
def get_tools():
    return {"tools": registry.describe_all(), "levels": registry.LEVEL_INFO,
            "reversible_write_policy": settings.get("permissions.reversible_write")}


class ToolToggle(BaseModel):
    enabled: bool | None = None
    auto_approve: bool | None = None


@router.patch("/tools/{name}")
def toggle_tool(name: str, body: ToolToggle):
    t = registry.TOOLS.get(name)
    if t is None:
        raise HTTPException(404, "Unknown tool")
    if body.enabled is not None:
        disabled = set(settings.get("tools.disabled") or [])
        disabled.discard(name) if body.enabled else disabled.add(name)
        settings.put("tools.disabled", sorted(disabled))
    if body.auto_approve is not None:
        if body.auto_approve and not registry.can_auto_approve(t):
            raise HTTPException(400, f"{name} always asks for approval ({registry.LEVEL_INFO[t.level.value]['label']}); that can't be turned off.")
        auto = set(settings.get("tools.auto_approve") or [])
        auto.add(name) if body.auto_approve else auto.discard(name)
        settings.put("tools.auto_approve", sorted(auto))
    registry.audit(actor="user", tool_name="toggle_tool", level="reversible_write",
                   args={"tool": name, **body.model_dump(exclude_none=True)}, status="ok")
    return {"name": name, "enabled": name not in (settings.get("tools.disabled") or []),
            "auto_approve": name in (settings.get("tools.auto_approve") or []), "requires_approval": registry.needs_approval(t)}


@router.get("/agents")
def get_agents():
    running = chat.active_agents()
    out = []
    for a in agents.AGENTS.values():
        r = agents.readiness(a)
        if a.id in running:
            r = {"status": "running", "detail": "Working now"}
        item: dict[str, Any] = {"id": a.id, "name": a.name, "tagline": a.tagline, "icon": a.icon, "tools": a.tools,
                                "kind": a.kind, **r}
        if a.prompt_file:
            item["prompt"] = prompts.load(a.prompt_file).public()
        if a.kind in ("orchestrator", "specialist") and a.implemented:
            item["routing"] = routing.describe(a.id)
        if a.kind == "specialist":
            item["policy"] = {
                "max_level": (a.max_level.value if a.max_level else None), "max_steps": a.max_steps, "timeout_s": a.timeout_s,
                "can_delegate": False, "domain": a.domain,
                "memory": ("own notes + shared categories: " + ", ".join(sorted(a.memory.shared_categories or [])))
                          if not a.memory.all_scopes else "all memories"}
            if any(t.startswith("market_") for t in (a.tools or [])):
                item["market_data"] = market_data.status()
        out.append(item)
    return out


# ------------------------------------------------------------- dashboard
@router.get("/system")
def system():
    snap = system_monitor.snapshot()
    snap["online"] = system_monitor.online()
    snap["lan"] = system_monitor.lan_addresses()
    return snap


@router.get("/dashboard")
def dashboard():
    provs = list_providers()
    active = next((p for p in provs if p["active"]), None)
    connected = [p for p in provs if p["status"] in ("connected", "configured")]
    pending = db.scalar("SELECT COUNT(*) FROM approvals WHERE status = 'pending'")
    if not active or active["status"] in ("setup_required", "error"):
        sys_status = "SETUP REQUIRED" if not active or active["status"] == "setup_required" else "DEGRADED"
    else:
        sys_status = "OPTIMAL"
    # start of the local day, expressed in UTC to match stored timestamps
    today = data.local_now().replace(hour=0, minute=0, second=0).astimezone(timezone.utc).isoformat()
    ints = integrations()
    return {
        "time": data.local_now().isoformat(),
        "status": sys_status,
        "operator": {"name": settings.get("profile.name") or "", "title": settings.get("profile.title") or "Operator"},
        "core": {
            "provider": active,
            "memory_count": db.scalar("SELECT COUNT(*) FROM memories"),
            "voice": {"tts": voice.tts_engine(), "stt": voice.stt_engine()},
            "agents_running": len(chat.RUNS),
            "llms_connected": len(connected),
            "pending_approvals": pending,
        },
        "counts": {
            "tasks_open": db.scalar("SELECT COUNT(*) FROM tasks WHERE status = 'open'"),
            "tasks_overdue": len(data.overdue_tasks()),
            "conversations": db.scalar("SELECT COUNT(*) FROM conversations"),
            "tools": len(registry.TOOLS),
            "documents": db.scalar("SELECT COUNT(*) FROM documents"),
            "unread": db.scalar("SELECT COUNT(*) FROM notifications WHERE read = 0"),
        },
        "memory_insights": {
            "memories": db.scalar("SELECT COUNT(*) FROM memories"),
            "turns_today": db.scalar("SELECT COUNT(*) FROM messages WHERE role = 'user' AND created_at >= ?", (today,)),
            "tool_calls_today": db.scalar("SELECT COUNT(*) FROM audit_log WHERE actor IN ('assistant','job') AND status = 'ok' AND ts >= ?", (today,)),
            "categories": db.rows("SELECT category, COUNT(*) AS n FROM memories GROUP BY category ORDER BY n DESC LIMIT 8"),
        },
        "timeline": data.today_timeline(),
        "feed": data.list_notifications(8),
        "monitor": system_monitor.snapshot(),
        "network": {"online": system_monitor.online(), "lan": system_monitor.lan_addresses()},
        "providers": provs,
        "agents": get_agents(),
        "weather_configured": weather.configured(),
        "setup": [i for i in ints if i["status"] == "setup_required"
                  and (not i["id"].startswith("llm:") or i.get("active"))][:3],
    }


@router.post("/briefing")
async def make_briefing():
    text = await briefing.generate()
    data.notify("Executive briefing", text, kind="info", source="briefing")
    return {"text": text}


# ------------------------------------------------------------- voice
_tts_jobs: dict[str, tuple[str, float]] = {}


class TTSBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=20000)


@router.get("/voice/config")
def voice_config():
    return {"tts": voice.tts_engine(), "stt": voice.stt_engine(),
            "speak_replies": bool(settings.get("voice.speak_replies")),
            "voice_id": settings.get("voice.elevenlabs_voice_id"), "model": settings.get("voice.elevenlabs_model")}


@router.post("/voice/stt")
async def speech_to_text(audio: UploadFile = File(...)):
    blob = await audio.read(25 * 1024 * 1024)
    try:
        text = await voice.transcribe(blob, audio.filename or "speech.webm", audio.content_type or "audio/webm")
    except voice.VoiceError as e:
        raise HTTPException(503, str(e)) from e
    return {"text": text}


@router.post("/voice/tts")
def text_to_speech(body: TTSBody):
    if voice.tts_engine() != "elevenlabs":
        raise HTTPException(409, "ElevenLabs isn't configured; the browser voice will be used.")
    now = time.time()
    for k, (_, t) in list(_tts_jobs.items()):
        if now - t > 300:
            _tts_jobs.pop(k, None)
    text = voice.speakable(body.text)
    if not text:
        raise HTTPException(400, "Nothing speakable in that text.")
    tid = uuid.uuid4().hex
    _tts_jobs[tid] = (text, now)
    return {"id": tid, "url": f"/api/voice/tts/{tid}", "chars": len(text)}


@router.get("/voice/tts/{tid}")
async def tts_audio(tid: str):
    job = _tts_jobs.pop(tid, None)
    if not job:
        raise HTTPException(404, "Audio request expired")
    stream = voice.elevenlabs_stream(job[0])
    try:
        first = await stream.__anext__()
    except voice.VoiceError as e:
        raise HTTPException(502, str(e)) from e
    except StopAsyncIteration:
        raise HTTPException(502, "ElevenLabs returned no audio")

    async def body():
        yield first
        async for chunk in stream:
            yield chunk

    return StreamingResponse(body(), media_type="audio/mpeg", headers={"Cache-Control": "no-store"})
