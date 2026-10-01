"""Paths, environment loading, and secret handling.

Secrets (API keys, tokens, passwords) live only in the local `.env` file and in
the backend process environment. They are never sent to the browser; the API
only reports whether a secret is configured plus its last four characters.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv, set_key, unset_key

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
ENV_FILE = Path(os.environ.get("EV_ENV_FILE", BASE_DIR / ".env"))

load_dotenv(ENV_FILE, override=False)

DATA_DIR = Path(os.environ.get("EV_DATA_DIR", BASE_DIR / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
BACKUP_DIR = DATA_DIR / "backups"
EXPORT_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "ev.db"

HOST = os.environ.get("EV_HOST", "127.0.0.1")
PORT = int(os.environ.get("EV_PORT", "8000"))
SSL_CERTFILE = os.environ.get("EV_SSL_CERTFILE") or None
SSL_KEYFILE = os.environ.get("EV_SSL_KEYFILE") or None

# Every secret the app knows about. Anything else is rejected by set_secret().
SECRET_NAMES = {
    "GROQ_API_KEY": "Groq API key",
    "OPENAI_API_KEY": "OpenAI API key",
    "ANTHROPIC_API_KEY": "Anthropic (Claude) API key",
    "GEMINI_API_KEY": "Google Gemini API key",
    "OPENROUTER_API_KEY": "OpenRouter API key",
    "ELEVENLABS_API_KEY": "ElevenLabs API key",
    "GITHUB_TOKEN": "GitHub personal access token",
    "SMTP_PASSWORD": "SMTP (email) password / app password",
}


def ensure_dirs() -> None:
    for d in (DATA_DIR, UPLOAD_DIR, BACKUP_DIR, EXPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def get_secret(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def secret_hint(name: str) -> str | None:
    """Return a masked hint like '••••abcd' so the UI can show which key is set."""
    value = get_secret(name)
    if not value:
        return None
    return "••••" + value[-4:] if len(value) > 8 else "••••"


_SECRET_VALUE_RE = re.compile(r"^[\x21-\x7e]{1,512}$")  # printable ASCII, no spaces


def set_secret(name: str, value: str | None) -> None:
    if name not in SECRET_NAMES:
        raise ValueError(f"Unknown secret name: {name}")
    if value is None or value.strip() == "":
        os.environ.pop(name, None)
        if ENV_FILE.exists():
            unset_key(str(ENV_FILE), name)
        return
    value = value.strip()
    if not _SECRET_VALUE_RE.match(value):
        raise ValueError("Secret contains unsupported characters (spaces or non-ASCII).")
    ENV_FILE.touch(exist_ok=True)
    set_key(str(ENV_FILE), name, value, quote_mode="never")
    os.environ[name] = value
