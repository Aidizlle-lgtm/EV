"""Background jobs: approved scheduled tasks plus the built-in feed insights.

Jobs never get more than Prepare-level tool access and can never trigger an
approval prompt — anything that would need your approval is refused.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from .. import db
from . import data

log = logging.getLogger("ev.jobs")
KINDS = {"reminder", "briefing", "prompt"}
TICK_SECONDS = 20
INSIGHTS_EVERY = 300


def validate_schedule(s: dict[str, Any]) -> dict[str, Any]:
    kind = s.get("type")
    if kind == "once":
        at = data.parse_when(s.get("at"))
        if not at:
            raise ValueError("A one-time schedule needs 'at' (ISO date/time).")
        return {"type": "once", "at": at}
    if kind == "daily":
        t = str(s.get("time", ""))
        try:
            hh, mm = (int(x) for x in t.split(":"))
            assert 0 <= hh < 24 and 0 <= mm < 60
        except (ValueError, AssertionError) as e:
            raise ValueError("A daily schedule needs 'time' as HH:MM (24-hour).") from e
        days = s.get("weekdays")
        if days is not None:
            if not isinstance(days, list) or not all(isinstance(d, int) and 0 <= d <= 6 for d in days) or not days:
                raise ValueError("'weekdays' must be a list of numbers 0 (Mon) … 6 (Sun).")
            days = sorted(set(days))
        return {"type": "daily", "time": f"{hh:02d}:{mm:02d}", "weekdays": days}
    if kind == "interval":
        minutes = s.get("minutes")
        if not isinstance(minutes, int) or minutes < 5:
            raise ValueError("An interval schedule needs 'minutes' (whole number, at least 5).")
        return {"type": "interval", "minutes": minutes}
    raise ValueError("Schedule type must be 'once', 'daily', or 'interval'.")


def next_run(s: dict[str, Any], after: datetime) -> datetime | None:
    if s["type"] == "once":
        at = datetime.fromisoformat(s["at"])
        return at if at > after else None
    if s["type"] == "interval":
        return after + timedelta(minutes=s["minutes"])
    hh, mm = (int(x) for x in s["time"].split(":"))
    cand = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
    for _ in range(8):
        if cand > after and (not s.get("weekdays") or cand.weekday() in s["weekdays"]):
            return cand
        cand += timedelta(days=1)
    return None


def describe_schedule(s: dict[str, Any]) -> str:
    if s["type"] == "once":
        return f"once at {s['at'][:16].replace('T', ' ')}"
    if s["type"] == "interval":
        return f"every {s['minutes']} min"
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    days = ", ".join(names[d] for d in s["weekdays"]) if s.get("weekdays") else "every day"
    return f"{days} at {s['time']}"


def create_job(name: str, kind: str, schedule: dict[str, Any], payload: dict[str, Any] | None = None,
               created_by: str = "user") -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    payload = payload or {}
    if kind == "reminder" and not str(payload.get("message", "")).strip():
        raise ValueError("A reminder needs a message.")
    if kind == "prompt" and not str(payload.get("prompt", "")).strip():
        raise ValueError("A prompt job needs a prompt.")
    sched = validate_schedule(schedule)
    nxt = next_run(sched, data.local_now())
    if nxt is None:
        raise ValueError("That schedule never runs in the future.")
    now = db.now_iso()
    jid = db.execute(
        "INSERT INTO jobs(name, kind, schedule, payload, enabled, approved_at, created_by, next_run_at, created_at) "
        "VALUES(?,?,?,?,1,?,?,?,?)",
        (name.strip()[:120], kind, db.dumps(sched), db.dumps(payload), now, created_by, nxt.isoformat(), now),
    )
    return get_job(jid)  # type: ignore[return-value]


def get_job(job_id: int) -> dict[str, Any] | None:
    j = db.row("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if j:
        j["schedule"] = db.loads(j["schedule"], {})
        j["payload"] = db.loads(j["payload"], {})
        j["schedule_text"] = describe_schedule(j["schedule"]) if j["schedule"] else ""
    return j


def list_jobs() -> list[dict[str, Any]]:
    return [get_job(r["id"]) for r in db.rows("SELECT id FROM jobs ORDER BY id DESC")]  # type: ignore[misc]


def set_enabled(job_id: int, enabled: bool) -> dict[str, Any]:
    job = get_job(job_id)
    if not job:
        raise LookupError(f"Job {job_id} not found")
    nxt = next_run(job["schedule"], data.local_now()) if enabled else None
    db.execute("UPDATE jobs SET enabled = ?, next_run_at = ? WHERE id = ?",
               (1 if enabled and nxt else 0, nxt.isoformat() if nxt else None, job_id))
    return get_job(job_id)  # type: ignore[return-value]


def delete_job(job_id: int) -> None:
    db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


async def execute(job: dict[str, Any]) -> str:
    from . import briefing, chat  # local import: avoids a cycle at startup

    kind, payload = job["kind"], job["payload"]
    if kind == "reminder":
        data.notify(f"Reminder: {payload['message'][:120]}", payload["message"], kind="tip", source="workflow")
        return "Reminder delivered to the feed."
    if kind == "briefing":
        text = await briefing.generate()
        data.notify("Your briefing is ready", text, kind="info", source="workflow")
        return text
    if kind == "prompt":
        text = await chat.run_headless(payload["prompt"], label=job["name"])
        data.notify(f"Workflow result: {job['name'][:80]}", text, kind="info", source="workflow")
        return text
    raise ValueError(f"Unknown job kind {kind}")


async def run_now(job_id: int) -> dict[str, Any]:
    job = get_job(job_id)
    if not job:
        raise LookupError(f"Job {job_id} not found")
    return await _run(job, reschedule=False)


async def _run(job: dict[str, Any], reschedule: bool = True) -> dict[str, Any]:
    try:
        result, status = await asyncio.wait_for(execute(job), timeout=180), "ok"
    except Exception as e:  # recorded, shown in Workflows, never silent
        result, status = f"{type(e).__name__}: {getattr(e, 'message', None) or e}", "error"
        log.warning("job %s failed: %s", job["id"], result)
        data.notify(f"Workflow failed: {job['name'][:80]}", result, kind="warn", source="workflow")
    now = data.local_now()
    fields: dict[str, Any] = {"last_run_at": now.isoformat(), "last_status": status, "last_result": str(result)[:4000]}
    if reschedule:
        nxt = None if job["schedule"]["type"] == "once" else next_run(job["schedule"], now)
        fields["next_run_at"] = nxt.isoformat() if nxt else None
        if nxt is None:
            fields["enabled"] = 0
    cols = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE jobs SET {cols} WHERE id = ?", tuple(fields.values()) + (job["id"],))
    return get_job(job["id"])  # type: ignore[return-value]


async def tick() -> None:
    now = data.local_now()
    for j in db.rows("SELECT id, next_run_at FROM jobs WHERE enabled = 1 AND approved_at IS NOT NULL AND next_run_at IS NOT NULL"):
        if datetime.fromisoformat(j["next_run_at"]) <= now:
            job = get_job(j["id"])
            if job:
                await _run(job)


async def scheduler_loop(stop: asyncio.Event) -> None:
    from . import insights

    last_insights = 0.0
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        try:
            await tick()
            if loop.time() - last_insights > INSIGHTS_EVERY or last_insights == 0.0:
                last_insights = loop.time()
                await insights.refresh()
        except Exception:  # keep the scheduler alive no matter what
            log.exception("scheduler tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
