"""Passcode login with server-side sessions.

* The passcode is created once, and only from the laptop itself (loopback), so
  someone else on the Wi-Fi can't claim the app before you do.
* Sessions are random tokens in an HttpOnly, SameSite=Strict cookie; only a
  SHA-256 hash of each token is stored.
* State-changing API calls must also send the `X-EV-Request: 1` header, which a
  cross-site page cannot add without a CORS preflight we never grant (CSRF guard).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import Request

from . import db
from .services import settings

COOKIE_NAME = "ev_session"
SESSION_DAYS = 30
PBKDF2_ITERATIONS = 600_000
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

_failures: dict[str, tuple[int, float]] = {}  # ip -> (count, locked_until)


def _hash_passcode(passcode: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", passcode.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_passcode(passcode: str, stored: str) -> bool:
    try:
        _, iterations, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", passcode.encode(), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _passcode_hash() -> str | None:
    r = db.row("SELECT value FROM settings WHERE key = 'auth.passcode_hash'")
    return db.loads(r["value"]) if r else None


def is_setup() -> bool:
    return bool(_passcode_hash())


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def is_loopback(request: Request) -> bool:
    return client_ip(request) in LOOPBACK_HOSTS


def set_passcode(passcode: str) -> None:
    if len(passcode) < 6:
        raise ValueError("Passcode must be at least 6 characters.")
    db.execute(
        "INSERT INTO settings(key, value) VALUES('auth.passcode_hash', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (db.dumps(_hash_passcode(passcode)),),
    )


def change_passcode(current: str, new: str) -> None:
    stored = _passcode_hash()
    if not stored or not _verify_passcode(current, stored):
        raise PermissionError("Current passcode is incorrect.")
    set_passcode(new)
    db.execute("DELETE FROM auth_sessions")  # sign out every device


def check_rate_limit(ip: str) -> float:
    """Seconds the caller must wait before another attempt (0 if allowed)."""
    count, locked_until = _failures.get(ip, (0, 0.0))
    return max(0.0, locked_until - time.time())


def record_failure(ip: str) -> None:
    count, _ = _failures.get(ip, (0, 0.0))
    count += 1
    delay = 0 if count < 5 else min(300, 15 * 2 ** (count - 5))
    _failures[ip] = (count, time.time() + delay)


def login(passcode: str, request: Request) -> str:
    ip = client_ip(request)
    wait = check_rate_limit(ip)
    if wait > 0:
        raise PermissionError(f"Too many attempts. Try again in {int(wait) + 1} seconds.")
    stored = _passcode_hash()
    if not stored or not _verify_passcode(passcode, stored):
        record_failure(ip)
        raise PermissionError("Incorrect passcode.")
    _failures.pop(ip, None)
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    db.execute(
        "INSERT INTO auth_sessions(token_hash, created_at, expires_at, user_agent, ip) VALUES(?,?,?,?,?)",
        (
            hashlib.sha256(token.encode()).hexdigest(),
            now.isoformat(),
            (now + timedelta(days=SESSION_DAYS)).isoformat(),
            request.headers.get("user-agent", "")[:200],
            ip,
        ),
    )
    return token


def logout(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (hashlib.sha256(token.encode()).hexdigest(),))


def session_valid(token: str | None) -> bool:
    if not token:
        return False
    r = db.row(
        "SELECT expires_at FROM auth_sessions WHERE token_hash = ?",
        (hashlib.sha256(token.encode()).hexdigest(),),
    )
    if not r:
        return False
    return datetime.fromisoformat(r["expires_at"]) > datetime.now(timezone.utc)


def operator_name() -> str:
    return settings.get("profile.name") or ""
