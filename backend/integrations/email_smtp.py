"""Outgoing email via SMTP (e.g. Gmail with an App Password). Sending is an
External action, so it only ever runs after you approve the exact message."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from .. import config
from ..services import settings, status


def configured() -> bool:
    return bool(settings.get("smtp.host") and settings.get("smtp.user") and config.get_secret("SMTP_PASSWORD"))


def setup_issue() -> str | None:
    if configured():
        return None
    return "Email sending isn't set up. Add SMTP host, user, and SMTP_PASSWORD in Settings → Integrations."


def _connect() -> smtplib.SMTP:
    host, port = settings.get("smtp.host"), int(settings.get("smtp.port") or 587)
    ctx = ssl.create_default_context()
    if port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, context=ctx, timeout=20)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
    try:
        if port != 465:
            server.starttls(context=ctx)
        server.login(settings.get("smtp.user"), config.get_secret("SMTP_PASSWORD"))
    except BaseException:
        server.close()
        raise
    return server


def check_login() -> None:
    """Log in and disconnect without sending anything."""
    if not configured():
        raise LookupError(setup_issue())
    try:
        with _connect():
            pass
    except smtplib.SMTPAuthenticationError as e:
        status.record("email", False, "SMTP login failed — check user and app password.")
        raise PermissionError("SMTP login failed — check the username and app password.") from e
    except (smtplib.SMTPException, OSError) as e:
        status.record("email", False, f"SMTP error: {e}")
        raise
    status.record("email", True, "SMTP login OK (nothing was sent)")


def send(to: str, subject: str, body: str) -> dict[str, str]:
    sender = settings.get("smtp.from") or settings.get("smtp.user")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg.set_content(body)
    try:
        with _connect() as server:
            refused = server.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        status.record("email", False, "SMTP login failed — check user and app password.")
        raise PermissionError("SMTP login failed — check the username and app password.") from e
    except (smtplib.SMTPException, OSError) as e:
        status.record("email", False, f"SMTP error: {e}")
        raise
    if refused:
        raise RuntimeError(f"Server refused recipients: {', '.join(refused)}")
    status.record("email", True, "Last email accepted by SMTP server")
    return {"to": to, "subject": subject, "status": "accepted by SMTP server"}
