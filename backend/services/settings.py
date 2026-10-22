"""Non-secret preferences stored in SQLite (secrets live in .env — see config.py)."""
from __future__ import annotations

import re
from typing import Any

from .. import db

DEFAULTS: dict[str, Any] = {
    # Who E.V. is talking to
    "profile.name": "",
    "profile.title": "Operator",
    # Personality
    "persona.wit": "light",              # off | light | playful
    "persona.verbosity": "balanced",     # brief | balanced | detailed
    "persona.custom": "",                # extra instructions written by the user
    # Language model
    "llm.provider": "groq",
    "llm.fallback_provider": "",
    "llm.temperature": 0.6,
    "llm.max_steps": 8,
    "anthropic.effort": "medium",        # low | medium | high | xhigh | max
    "providers.ollama.base_url": "http://localhost:11434/v1",
    # Permissions
    "permissions.reversible_write": "ask",   # ask | auto  (external/destructive is ALWAYS ask)
    "permissions.approval_timeout_s": 600,
    "tools.disabled": [],
    "tools.auto_approve": [],            # reversible-write tools you chose to run without asking
    "memory.require_approval": True,
    # Voice
    "voice.tts": "auto",                 # auto | elevenlabs | browser | off
    "voice.stt": "auto",                 # auto | groq | browser
    "voice.elevenlabs_voice_id": "mmr1IUaAlIQE2pSB8y0J",
    "voice.elevenlabs_model": "eleven_multilingual_v2",
    "voice.speak_replies": False,
    "voice.max_tts_chars": 1200,
    # Weather (Open-Meteo, no key needed)
    "weather.location": "",
    "weather.lat": None,
    "weather.lon": None,
    "weather.label": "",
    "weather.units": "celsius",          # celsius | fahrenheit
    # Email (SMTP) — password is a secret in .env
    "smtp.host": "",
    "smtp.port": 587,
    "smtp.user": "",
    "smtp.from": "",
    # Fire TV (ADB on your home network)
    "firetv.host": "",
    "firetv.adb_path": "",
    # Live feed / background insights
    "feed.enabled": True,
    "feed.cpu_alert_percent": 90,
    # Market data for F.R.I.D.A.Y. — separate from AI connections. "test" = clearly
    # labelled synthetic data (no live source is connected yet); "none" = off.
    "market_data.source": "test",
}

# Per-agent model routing: agents.<agent_id>.connection | .model | .fallbacks
AGENT_KEY_RE = re.compile(r"^agents\.[a-z0-9_]{1,30}\.(connection|model|fallbacks)$")

# Model chosen per provider is stored under "llm.model.<provider>".
DEFAULT_MODELS = {
    "groq": "openai/gpt-oss-120b",
    "anthropic": "claude-opus-5-5",
    "openai": "",
    "gemini": "",
    "openrouter": "",
    "ollama": "",
}

CHOICES = {
    "persona.wit": {"off", "light", "playful"},
    "persona.verbosity": {"brief", "balanced", "detailed"},
    "anthropic.effort": {"low", "medium", "high", "xhigh", "max"},
    "permissions.reversible_write": {"ask", "auto"},
    "voice.tts": {"auto", "elevenlabs", "browser", "off"},
    "voice.stt": {"auto", "groq", "browser"},
    "weather.units": {"celsius", "fahrenheit"},
    "market_data.source": {"test", "none"},
}


def _valid_key(key: str) -> bool:
    return key in DEFAULTS or (key.startswith("llm.model.") and key[10:] in DEFAULT_MODELS) or bool(AGENT_KEY_RE.match(key))


def get(key: str) -> Any:
    stored = db.row("SELECT value FROM settings WHERE key = ?", (key,))
    if stored is not None:
        return db.loads(stored["value"])
    if key.startswith("llm.model."):
        return DEFAULT_MODELS.get(key[10:], "")
    if AGENT_KEY_RE.match(key):
        return [] if key.endswith(".fallbacks") else ""
    return DEFAULTS.get(key)


def put(key: str, value: Any) -> None:
    if not _valid_key(key):
        raise ValueError(f"Unknown setting: {key}")
    if key == "firetv.host" and value and not re.match(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}(:\d{1,5})?$", str(value).strip()):
        raise ValueError("Fire TV address must be an IP like 192.168.1.40 (optionally :5555)")
    if key in ("tools.disabled", "tools.auto_approve") and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
        raise ValueError(f"{key} must be a list of tool names")
    if AGENT_KEY_RE.match(key):
        if key.endswith(".fallbacks"):
            ok = isinstance(value, list) and len(value) <= 4 and all(
                isinstance(f, dict) and set(f) <= {"connection", "model"} and isinstance(f.get("connection"), str)
                and isinstance(f.get("model", ""), str) and len(f.get("model", "")) <= 200 for f in value)
            if not ok:
                raise ValueError("Fallbacks must be a list of up to 4 {connection, model} entries")
        elif not isinstance(value, str) or len(value) > 200:
            raise ValueError(f"{key} must be text")
    if key in CHOICES and value not in CHOICES[key]:
        raise ValueError(f"{key} must be one of {sorted(CHOICES[key])}")
    default = DEFAULTS.get(key)
    if isinstance(default, bool) and not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false")
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{key} must be a number")
    db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, db.dumps(value)),
    )


def all_settings() -> dict[str, Any]:
    out = dict(DEFAULTS)
    for p, m in DEFAULT_MODELS.items():
        out[f"llm.model.{p}"] = m
    for r in db.rows("SELECT key, value FROM settings"):
        if _valid_key(r["key"]):
            out[r["key"]] = db.loads(r["value"])
    return out


def model_for(provider: str) -> str:
    return get(f"llm.model.{provider}") or ""
