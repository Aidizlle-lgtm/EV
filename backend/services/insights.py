"""Built-in, read-only feed insights computed from your real data (no invented items)."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from ..integrations import github, system_monitor
from . import data, settings


async def refresh() -> None:
    if not settings.get("feed.enabled"):
        return
    now = data.local_now()
    today = now.date().isoformat()

    overdue = data.overdue_tasks()
    if overdue:
        key = hashlib.sha1(",".join(str(t["id"]) for t in overdue).encode()).hexdigest()[:10]
        names = "; ".join(t["title"] for t in overdue[:3])
        data.notify(f"{len(overdue)} task{'s are' if len(overdue) > 1 else ' is'} overdue — {names[:80]}",
                    "Review the board and reschedule what still matters.", kind="warn", source="tasks",
                    dedupe_key=f"overdue:{today}:{key}", link="#/tasks")

    for e in data.list_events(now.isoformat(), (now + timedelta(minutes=30)).isoformat()):
        if datetime.fromisoformat(e["starts_at"]) >= now:
            data.notify(f"Starting soon: {e['title'][:90]}", f"At {e['starts_at'][11:16]}" + (f" · {e['location']}" if e["location"] else ""),
                        kind="info", source="calendar", dedupe_key=f"event-soon:{e['id']}", link="#/calendar")

    if github.configured():
        try:
            prs = await github.review_requests()
            if prs:
                data.notify(f"{len(prs)} pull request{'s are' if len(prs) > 1 else ' is'} awaiting your review",
                            "; ".join(f"{p['repo']}#{p['number']}" for p in prs[:5]), kind="tip", source="github",
                            dedupe_key=f"gh:{today}:{len(prs)}", link=prs[0]["url"])
        except Exception:
            pass  # status is recorded by the integration; the feed stays quiet

    snap = system_monitor.snapshot()
    threshold = settings.get("feed.cpu_alert_percent") or 90
    if snap["cpu_percent"] >= threshold:
        data.notify(f"CPU usage at {snap['cpu_percent']}%", "Sustained high load on this laptop.", kind="live",
                    source="system", dedupe_key=f"cpu:{now.strftime('%Y-%m-%dT%H')}")
