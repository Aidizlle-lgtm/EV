"""Executive Briefing: a summary built only from your real data."""
from __future__ import annotations

from typing import Any

from ..integrations import github, system_monitor, weather
from . import data


async def snapshot() -> dict[str, Any]:
    snap: dict[str, Any] = {
        "now": data.local_now().isoformat(),
        "today": data.today_timeline(),
        "open_tasks": [{"id": t["id"], "title": t["title"], "priority": t["priority"], "due_at": t["due_at"]}
                       for t in data.list_tasks("open", limit=25)],
        "overdue_tasks": [t["title"] for t in data.overdue_tasks()],
        "unread_feed": [n["title"] for n in data.list_notifications(10, unread_only=True)],
        "system": {k: v for k, v in system_monitor.snapshot().items() if k in ("cpu_percent", "ram_percent", "disk_percent", "battery")},
    }
    try:
        snap["weather"] = await weather.current()
    except Exception:
        snap["weather"] = "not available (setup required or offline)"
    if github.configured():
        try:
            snap["github_reviews"] = [f"{p['repo']}#{p['number']} {p['title']}" for p in await github.review_requests()]
        except Exception:
            snap["github_reviews"] = "unavailable"
    return snap


def plain_briefing(snap: dict[str, Any]) -> str:
    lines = [f"Briefing for {snap['now'][:16].replace('T', ' ')}"]
    w = snap.get("weather")
    if isinstance(w, dict):
        lines.append(f"Weather: {w['temperature']}{w['unit']} and {w['condition'].lower()} in {w['location']}.")
    if snap["today"]:
        lines.append("Today: " + "; ".join(f"{i['at'][11:16]} {i['title']}" for i in snap["today"]))
    else:
        lines.append("Nothing scheduled today.")
    if snap["overdue_tasks"]:
        lines.append(f"Overdue ({len(snap['overdue_tasks'])}): " + "; ".join(snap["overdue_tasks"][:5]))
    lines.append(f"Open tasks: {len(snap['open_tasks'])}.")
    if isinstance(snap.get("github_reviews"), list) and snap["github_reviews"]:
        lines.append(f"GitHub reviews waiting: {len(snap['github_reviews'])}.")
    return "\n".join(lines)


async def generate() -> str:
    from . import chat

    snap = await snapshot()
    prompt = (
        "Write my executive briefing from ONLY the JSON data below. Lead with the 1–3 things that matter most "
        "today, then schedule, tasks (flag overdue), and anything else notable. Be concise, use short bullet "
        "points, and do not invent anything that isn't in the data. If a section has no data, say so in a few words.\n\n"
        f"DATA:\n{snap}"
    )
    try:
        return await chat.run_headless(prompt, label="Executive briefing", use_tools=False)
    except Exception as e:
        return plain_briefing(snap) + f"\n\n(AI summary unavailable: {getattr(e, 'message', e)})"
