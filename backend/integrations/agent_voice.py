"""Per-agent voice profiles — prepared, not active for specialists yet.

Three things are kept separate on purpose:
  * AI connections (services/connections.py)      — who the agent thinks with
  * market-data sources (integrations/market_data) — what F.R.I.D.A.Y. can look up
  * voice                                           — how an agent sounds (this file)

E.V.'s existing voice (ElevenLabs / browser, integrations/voice.py) is unchanged.
Specialists have no voice yet: no provider is chosen, nothing is purchased, and the
microphone is never enabled for them. To add one later:
  1. register a VoiceProvider in PROVIDERS (its API key goes in config.SECRET_NAMES),
  2. store the agent's choice in the setting agents.<id>.voice = {provider, voice_id, model},
  3. have profile() report available=True once the provider and key are present,
  4. let the interface's Stop also stop playback (the workspace already calls stopSpeech()).
"""
from __future__ import annotations

from typing import Any

from ..services import settings
from . import voice

# How each specialist should eventually sound. A description for choosing a licensed or
# original voice later — not an instruction to imitate any particular person.
INTENDED_STYLE = {
    "friday": "Calm, warm, confident, and precise, lightly Irish in character. Use a licensed or original voice; "
              "an exact actor imitation is not wanted.",
}

PROVIDERS: dict[str, Any] = {}   # voice providers available to specialists: none yet


def profile(agent_id: str) -> dict[str, Any]:
    if agent_id == "ev":
        engine = voice.tts_engine()
        return {"agent": "ev", "available": engine != "off", "engine": engine, "microphone": voice.stt_engine(),
                "managed_in": "Settings → Voice", "reason": None}
    cfg = settings.get(f"agents.{agent_id}.voice") or {}
    provider = cfg.get("provider") if isinstance(cfg, dict) else None
    available = bool(provider and provider in PROVIDERS)          # always False until a provider exists
    return {"agent": agent_id, "available": available, "engine": "off", "microphone": False,
            "provider": provider or None, "voice_id_set": bool(isinstance(cfg, dict) and cfg.get("voice_id")),
            "reason": None if available else "No voice is connected for this agent yet. Text chat works without one.",
            "intended_style": INTENDED_STYLE.get(agent_id)}
