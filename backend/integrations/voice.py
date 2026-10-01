"""Voice: ElevenLabs text-to-speech and Groq Whisper speech-to-text.
Both fall back to the browser's built-in speech features when not configured."""
from __future__ import annotations

import re
from typing import AsyncIterator

import httpx

from .. import config
from ..services import settings, status

ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_STT_MODEL = "whisper-large-v3-turbo"


class VoiceError(Exception):
    pass


def tts_engine() -> str:
    """Which engine will actually speak: elevenlabs | browser | off."""
    choice = settings.get("voice.tts") or "auto"
    if choice == "off":
        return "off"
    if choice in ("auto", "elevenlabs") and config.get_secret("ELEVENLABS_API_KEY") and settings.get("voice.elevenlabs_voice_id"):
        return "elevenlabs"
    return "browser"


def stt_engine() -> str:
    choice = settings.get("voice.stt") or "auto"
    if choice in ("auto", "groq") and config.get_secret("GROQ_API_KEY"):
        return "groq"
    return "browser"


def speakable(text: str) -> str:
    """Strip markdown/code so the voice reads naturally."""
    text = re.sub(r"```.*?```", " (code omitted) ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_#>|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    limit = int(settings.get("voice.max_tts_chars") or 1200)
    if len(text) > limit:
        cut = text.rfind(". ", 0, limit)
        text = text[: cut + 1 if cut > limit // 2 else limit]
    return text


async def elevenlabs_stream(text: str) -> AsyncIterator[bytes]:
    key = config.get_secret("ELEVENLABS_API_KEY")
    voice_id = settings.get("voice.elevenlabs_voice_id")
    if not key or not voice_id:
        raise VoiceError("ElevenLabs is not set up: add ELEVENLABS_API_KEY and a voice ID in Settings.")
    url = ELEVEN_URL.format(voice_id=voice_id)
    body = {"text": text, "model_id": settings.get("voice.elevenlabs_model") or "eleven_multilingual_v2"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=60, write=10, pool=10)) as client:
        async with client.stream("POST", url, params={"output_format": "mp3_44100_128"}, json=body,
                                 headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"}) as r:
            if r.status_code != 200:
                detail = (await r.aread()).decode(errors="replace")[:200]
                msg = {401: "ElevenLabs rejected the API key (expired or wrong?).",
                       429: "ElevenLabs rate limit or character quota reached."}.get(r.status_code, f"ElevenLabs error HTTP {r.status_code}: {detail}")
                status.record("elevenlabs", False, msg)
                raise VoiceError(msg)
            status.record("elevenlabs", True, "Voice synthesis working")
            async for chunk in r.aiter_bytes():
                yield chunk


async def transcribe(audio: bytes, filename: str, content_type: str) -> str:
    key = config.get_secret("GROQ_API_KEY")
    if not key:
        raise VoiceError("Server speech-to-text needs GROQ_API_KEY. The browser's own recognizer can be used instead.")
    if len(audio) > 24 * 1024 * 1024:
        raise VoiceError("Recording is too long (over 24 MB).")
    files = {"file": (filename or "speech.webm", audio, content_type or "audio/webm")}
    data = {"model": GROQ_STT_MODEL, "response_format": "json"}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(GROQ_STT_URL, headers={"Authorization": f"Bearer {key}"}, files=files, data=data)
    except httpx.HTTPError as e:
        status.record("stt", False, "Couldn't reach Groq for transcription")
        raise VoiceError("Couldn't reach Groq for transcription. Check your connection.") from e
    if r.status_code != 200:
        msg = f"Transcription failed (HTTP {r.status_code}): {r.text[:200]}"
        status.record("stt", False, msg)
        raise VoiceError(msg)
    status.record("stt", True, "Transcription working")
    return (r.json().get("text") or "").strip()
