"""Named AI connections: several providers, and several keys per provider.

A connection = a provider preset + your name for it + its own API key + model
settings (+ an optional base URL). E.V. and each specialist are assigned a
connection and model separately (see services/routing.py), and one connection
can be shared by any number of agents.

Two kinds of connection appear in one list:
  * built-in — the providers E.V. already had (Groq, Claude, OpenAI, …), using the
    keys you saved before (GROQ_API_KEY, …). Nothing about them changed.
  * custom   — connections you add in AI Core → Connections. Each one keeps its key
    in .env as CONNECTION_KEY_<ID>.

Secrets: keys are written only to .env. They are never stored in the database,
put in prompts, written to logs or the audit log, or returned to the browser
(only a masked hint like ••••abcd).
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any, Callable
from urllib.parse import urlparse

from .. import config, db, providers
from ..providers import presets
from ..providers.anthropic_provider import AnthropicProvider
from ..providers.base import Provider, ProviderError
from ..providers.openai_compat import OpenAICompatProvider
from . import settings

KEY_HINT = "paste a key for this connection in AI Core → Connections"


def _openai_compat(conn: dict[str, Any], preset: presets.Preset, key_env: str | None) -> Provider:
    return OpenAICompatProvider(conn["id"], conn["name"], conn.get("base_url") or preset.base_url, key_env,
                                extra_headers=dict(preset.extra_headers), setup_hint=KEY_HINT)


def _anthropic(conn: dict[str, Any], preset: presets.Preset, key_env: str | None) -> Provider:
    return AnthropicProvider(conn["id"], conn["name"], key_env or "", base_url=conn.get("base_url") or None,
                             setup_hint=KEY_HINT)


# adapter id → factory. Register a new wire format here (see providers/presets.py).
ADAPTERS: dict[str, Callable[[dict[str, Any], presets.Preset, str | None], Provider]] = {
    "openai_compat": _openai_compat,
    "anthropic": _anthropic,
}

_cache: dict[str, tuple[str, Provider]] = {}


# ------------------------------------------------------------------ helpers
def key_env(conn_id: str) -> str:
    return "CONNECTION_KEY_" + re.sub(r"[^A-Z0-9]", "_", conn_id.upper())[:48]


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    return s or "connection"


def _unique_id(name: str) -> str:
    base = _slug(name)
    taken = {r["id"] for r in db.rows("SELECT id FROM connections")} | set(providers.all_providers())
    cid, n = base, 2
    while cid in taken:
        cid = f"{base}-{n}"
        n += 1
    return cid


def _is_private_host(host: str) -> bool:
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".ts.net"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")


def validate_base_url(url: str | None, preset: presets.Preset) -> str | None:
    url = (url or "").strip().rstrip("/")
    if not url:
        if preset.base_url is None and preset.adapter == "openai_compat":
            raise ValueError(f"{preset.label} needs a base URL (for example https://api.example.com/v1).")
        return None
    if not preset.custom_base_url and preset.adapter != "anthropic":
        raise ValueError(f"{preset.label} uses a fixed address; leave the base URL empty.")
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("Base URL must look like https://host/path (no credentials in the URL).")
    if p.scheme == "http" and not _is_private_host(p.hostname):
        raise ValueError("Use https:// for services on the internet. Plain http:// is only allowed for your own computer or home network.")
    if len(url) > 300:
        raise ValueError("Base URL is too long.")
    return url


# ------------------------------------------------------------------ read
def get(conn_id: str) -> dict[str, Any] | None:
    r = db.row("SELECT * FROM connections WHERE id = ?", (conn_id,))
    if r:
        r["models"] = db.loads(r["models"], []) or []
    return r


def exists(conn_id: str) -> bool:
    return bool(conn_id) and (get(conn_id) is not None or conn_id in providers.all_providers())


def provider_for(conn_id: str) -> Provider:
    """The adapter instance for a connection. Built-in ids resolve to the existing providers."""
    conn = get(conn_id)
    if conn is None:
        return providers.get(conn_id)          # raises a readable 'setup' error if unknown
    preset = presets.get(conn["preset"])
    if preset is None or not preset.supported or preset.adapter not in ADAPTERS:
        raise ProviderError("setup", f"Connection '{conn['name']}' uses a provider E.V. can't talk to yet.", provider=conn_id)
    cached = _cache.get(conn_id)
    if cached and cached[0] == conn["updated_at"]:
        return cached[1]
    prov = ADAPTERS[preset.adapter](conn, preset, key_env(conn_id) if preset.key_required or config.get_secret(key_env(conn_id)) else None)
    _cache[conn_id] = (conn["updated_at"], prov)
    return prov


def default_model(conn_id: str) -> str:
    conn = get(conn_id)
    if conn is not None:
        return conn["default_model"] or ""
    return settings.model_for(conn_id)


def temperature(conn_id: str) -> float | None:
    conn = get(conn_id)
    if conn is not None and conn.get("temperature") is not None:
        return float(conn["temperature"])
    t = settings.get("llm.temperature")
    return float(t) if t is not None else None


def family(conn_id: str) -> str:
    """Which *provider account pool* a connection belongs to. Two keys for the same
    provider share a family, so a rate limit on one is never 'escaped' by switching
    to the other (see routing.py)."""
    conn = get(conn_id)
    if conn is None:
        return conn_id
    preset = presets.get(conn["preset"])
    if preset and (preset.custom_base_url or preset.local) and conn.get("base_url"):
        return "host:" + (urlparse(conn["base_url"]).hostname or conn["base_url"])
    return conn["preset"]


def public(conn_id: str) -> dict[str, Any]:
    conn = get(conn_id)
    if conn is None:
        p = providers.get(conn_id)
        return {"id": conn_id, "name": p.label, "preset": conn_id, "builtin": True, "base_url": None,
                "default_model": settings.model_for(conn_id), "models": [], "temperature": None,
                "key_hint": config.secret_hint(p.needs_key) if p.needs_key else None,
                "key_required": bool(p.needs_key), "configured": p.configured(), "family": family(conn_id)}
    preset = presets.get(conn["preset"])
    env = key_env(conn_id)
    return {"id": conn_id, "name": conn["name"], "preset": conn["preset"],
            "preset_label": preset.label if preset else conn["preset"], "builtin": False,
            "base_url": conn.get("base_url"), "default_model": conn["default_model"], "models": conn["models"],
            "temperature": conn.get("temperature"), "key_hint": config.secret_hint(env),
            "key_required": bool(preset and preset.key_required),
            "configured": bool(preset and preset.supported and (not preset.key_required or config.get_secret(env))),
            "verification": preset.verification if preset else "unknown", "family": family(conn_id)}


def list_all() -> list[dict[str, Any]]:
    builtin = [public(pid) for pid in providers.all_providers()]
    custom = [public(r["id"]) for r in db.rows("SELECT id FROM connections ORDER BY created_at, id")]
    return builtin + custom


# ------------------------------------------------------------------ write
def create(*, name: str, preset: str, base_url: str | None = None, api_key: str | None = None,
           default_model: str = "", temperature: float | None = None) -> dict[str, Any]:
    name = (name or "").strip()
    if not 1 <= len(name) <= 60:
        raise ValueError("Give the connection a name (up to 60 characters).")
    p = presets.get(preset)
    if p is None:
        raise ValueError(f"Unknown provider '{preset}'.")
    if not p.supported:
        raise ValueError(f"{p.label} isn't supported yet: {p.notes}")
    url = validate_base_url(base_url, p)
    if temperature is not None and not (0 <= temperature <= 2):
        raise ValueError("Temperature must be between 0 and 2.")
    cid = _unique_id(name)
    now = db.now_iso()
    if api_key:
        config.set_secret(key_env(cid), api_key)   # validates characters; raises before the row exists
    db.execute("INSERT INTO connections(id, name, preset, base_url, default_model, models, temperature, created_at, updated_at) "
               "VALUES(?,?,?,?,?,?,?,?,?)",
               (cid, name, preset, url, (default_model or "").strip()[:200], db.dumps([]), temperature, now, now))
    return public(cid)


def update(conn_id: str, *, name: str | None = None, base_url: str | None = None, default_model: str | None = None,
           temperature: float | None = None, clear_temperature: bool = False, api_key: str | None = None) -> dict[str, Any]:
    conn = get(conn_id)
    if conn is None:
        raise LookupError("Built-in connections are managed with their own cards in AI Core." if conn_id in providers.all_providers()
                          else f"Connection '{conn_id}' not found.")
    p = presets.get(conn["preset"])
    assert p is not None
    fields: dict[str, Any] = {}
    if name is not None:
        if not 1 <= len(name.strip()) <= 60:
            raise ValueError("Name must be 1–60 characters.")
        fields["name"] = name.strip()
    if base_url is not None:
        fields["base_url"] = validate_base_url(base_url, p)
    if default_model is not None:
        fields["default_model"] = default_model.strip()[:200]
    if clear_temperature:
        fields["temperature"] = None
    elif temperature is not None:
        if not 0 <= temperature <= 2:
            raise ValueError("Temperature must be between 0 and 2.")
        fields["temperature"] = temperature
    if api_key is not None:
        config.set_secret(key_env(conn_id), api_key or None)
    fields["updated_at"] = db.now_iso()
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE connections SET {sets} WHERE id = ?", (*fields.values(), conn_id))
    _cache.pop(conn_id, None)
    return public(conn_id)


def set_models(conn_id: str, models: list[str]) -> None:
    if get(conn_id) is not None:
        db.execute("UPDATE connections SET models = ? WHERE id = ?", (db.dumps(models[:500]), conn_id))


def delete(conn_id: str) -> None:
    if get(conn_id) is None:
        raise LookupError(f"Connection '{conn_id}' not found.")
    config.set_secret(key_env(conn_id), None)
    db.execute("DELETE FROM connections WHERE id = ?", (conn_id,))
    _cache.pop(conn_id, None)
    # Agents that used it fall back to inheriting (never silently to some other key).
    for r in db.rows("SELECT key, value FROM settings WHERE key LIKE 'agents.%'"):
        value = db.loads(r["value"])
        if r["key"].endswith(".connection") and value == conn_id:
            settings.put(r["key"], "")
        elif r["key"].endswith(".fallbacks") and isinstance(value, list):
            settings.put(r["key"], [f for f in value if isinstance(f, dict) and f.get("connection") != conn_id])
