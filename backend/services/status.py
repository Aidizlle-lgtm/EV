"""Remembers the outcome of the most recent check/call for each integration so the
UI can show Connected / Setup required / Error honestly."""
from __future__ import annotations

from typing import Any

from .. import db

_checks: dict[str, dict[str, Any]] = {}


def record(integration_id: str, ok: bool, message: str = "") -> None:
    _checks[integration_id] = {"ok": ok, "message": message[:300], "at": db.now_iso()}


def clear(ids: list[str]) -> None:
    for i in ids:
        _checks.pop(i, None)


def last(integration_id: str) -> dict[str, Any] | None:
    return _checks.get(integration_id)


def state(integration_id: str, configured: bool, setup_message: str) -> dict[str, Any]:
    """Map configuration + last check into a status the UI understands."""
    if not configured:
        return {"status": "setup_required", "detail": setup_message}
    chk = last(integration_id)
    if chk is None:
        return {"status": "configured", "detail": "Configured — not tested yet this session."}
    if chk["ok"]:
        return {"status": "connected", "detail": chk["message"] or "Working", "checked_at": chk["at"]}
    return {"status": "error", "detail": chk["message"], "checked_at": chk["at"]}
