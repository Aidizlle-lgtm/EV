"""Editable prompt files: the personality, role, and behaviour you write for each agent.

Files live in prompts/ next to start_ev.bat, one per agent:
    prompts/EV.prompt.md        E.V. (central intelligence)
    prompts/FRIDAY.prompt.md    F.R.I.D.A.Y. (market intelligence)
    prompts/TEMPLATE.prompt.md  copy this when writing a new one

They're re-read automatically when you save them (no restart needed).

A status marker on any line tells E.V. how finished a prompt is:
    <!-- EV-PROMPT-STATUS: TEMPORARY -->   placeholder written for development
    <!-- EV-PROMPT-STATUS: STARTER -->     a starting point copied from the old built-in prompt
    (no marker, or CUSTOM)                 yours
The interface shows this status so a placeholder is never mistaken for your prompt.

What these files can NOT do: grant tools, raise permission levels, widen memory
access, or change limits. Those are enforced in code (services/agents.py,
tools/registry.py) and the code's rules are appended after your text.
HTML comments (<!-- … -->) are stripped before the prompt is sent to the model.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config

MAX_CHARS = 20000
STATUS_RE = re.compile(r"<!--\s*EV-PROMPT-STATUS:\s*(TEMPORARY|STARTER|CUSTOM)\s*-->", re.I)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)

_cache: dict[str, tuple[float, "PromptFile"]] = {}


def prompts_dir() -> Path:
    return Path(os.environ.get("EV_PROMPTS_DIR", config.BASE_DIR / "prompts"))


@dataclass
class PromptFile:
    stem: str
    path: Path
    exists: bool
    status: str        # temporary | starter | custom | missing | empty | too_long | error
    text: str
    detail: str = ""

    def public(self) -> dict[str, Any]:
        return {"file": f"prompts/{self.path.name}", "exists": self.exists, "status": self.status,
                "chars": len(self.text), "detail": self.detail}


def load(stem: str) -> PromptFile:
    if not re.fullmatch(r"[A-Z0-9_]{1,40}", stem):
        raise ValueError("Prompt file names are fixed in code (e.g. 'EV', 'FRIDAY').")
    path = prompts_dir() / f"{stem}.prompt.md"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return PromptFile(stem, path, False, "missing", "", f"prompts/{path.name} not found — using a minimal fallback.")
    cached = _cache.get(stem)
    if cached and cached[0] == mtime and cached[1].path == path:
        return cached[1]
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return PromptFile(stem, path, True, "error", "", f"Couldn't read prompts/{path.name}: {e}")
    m = STATUS_RE.search(raw)
    status = m.group(1).lower() if m else "custom"
    text = COMMENT_RE.sub("", raw).strip()
    detail = {"temporary": "Temporary development prompt — replace it with your own.",
              "starter": "Starter prompt copied from E.V.'s previous built-in instructions — edit freely.",
              "custom": "Your prompt."}[status]
    if not text:
        status, detail = "empty", f"prompts/{path.name} is empty — using a minimal fallback."
    elif len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
        status, detail = "too_long", f"Only the first {MAX_CHARS} characters are used."
    pf = PromptFile(stem, path, True, status, text, detail)
    _cache[stem] = (mtime, pf)
    return pf
