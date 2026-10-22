"""E.V.'s built-in tools. Each declares its action level; the registry enforces it."""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .. import db
from ..integrations import email_smtp, firetv, github, system_monitor, weather
from ..services import data, documents, files, jobs, settings
from .registry import Level, ToolContext, ToolError, tool


def _memory_confirm() -> bool:
    return bool(settings.get("memory.require_approval"))


def _need_folders() -> str | None:
    return None if files.list_folders() else "No folders are authorized. Add one in Settings → Folders."


def _wrap(fn):
    """Turn expected data errors into clean tool errors."""
    try:
        return fn()
    except (LookupError, ValueError, files.FileAccessError, documents.IngestError) as e:
        raise ToolError("error", str(e).strip("'\"")) from e


class NoArgs(BaseModel):
    pass


# ---------------------------------------------------------------- Tasks
class ListTasksArgs(BaseModel):
    status: Literal["open", "done", "all"] = Field("open", description="Which tasks to list")


@tool("list_tasks", level=Level.READ, args=ListTasksArgs, category="tasks",
      description="List the user's tasks from E.V.'s task board, with ids, priority, and due dates.")
def list_tasks(a: ListTasksArgs, ctx: ToolContext):
    return {"tasks": data.list_tasks(None if a.status == "all" else a.status)}


class CreateTaskArgs(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    notes: str = Field("", max_length=4000)
    priority: Literal["low", "normal", "high"] = "normal"
    due_at: Optional[str] = Field(None, description="ISO date or datetime in local time, e.g. 2026-10-02T17:00")


@tool("create_task", level=Level.WRITE, args=CreateTaskArgs, category="tasks",
      description="Add a task to the user's task board.",
      summarize=lambda a: f"Create task “{a.title}”" + (f" due {a.due_at}" if a.due_at else "") + f" · {a.priority} priority")
def create_task(a: CreateTaskArgs, ctx: ToolContext):
    return {"created": _wrap(lambda: data.create_task(a.title, a.notes, a.priority, a.due_at))}


class UpdateTaskArgs(BaseModel):
    task_id: int
    title: Optional[str] = Field(None, max_length=200)
    notes: Optional[str] = Field(None, max_length=4000)
    priority: Optional[Literal["low", "normal", "high"]] = None
    due_at: Optional[str] = Field(None, description="ISO date/datetime; empty string clears it")
    status: Optional[Literal["open", "done"]] = Field(None, description="Use 'done' to complete a task")


def _task_label(task_id: int) -> str:
    t = data.get_task(task_id)
    return f"“{t['title']}” (#{task_id})" if t else f"#{task_id} (not found)"


@tool("update_task", level=Level.WRITE, args=UpdateTaskArgs, category="tasks",
      description="Change a task's title, notes, priority, due date, or mark it done/open.",
      summarize=lambda a: f"Update task {_task_label(a.task_id)}: " + ", ".join(
          f"{k}={v!r}" for k, v in a.model_dump(exclude={'task_id'}).items() if v is not None))
def update_task(a: UpdateTaskArgs, ctx: ToolContext):
    fields = a.model_dump(exclude={"task_id"})
    return {"updated": _wrap(lambda: data.update_task(a.task_id, **fields))}


class TaskIdArgs(BaseModel):
    task_id: int


@tool("delete_task", level=Level.EXTERNAL, args=TaskIdArgs, category="tasks",
      description="Permanently delete a task. Destructive — the user must approve.",
      summarize=lambda a: f"Permanently delete task {_task_label(a.task_id)}")
def delete_task(a: TaskIdArgs, ctx: ToolContext):
    return {"deleted": _wrap(lambda: data.delete_task(a.task_id))}


# ---------------------------------------------------------------- Calendar
class ListEventsArgs(BaseModel):
    start: Optional[str] = Field(None, description="ISO start (default: today 00:00)")
    end: Optional[str] = Field(None, description="ISO end (default: start + 7 days)")


@tool("list_events", level=Level.READ, args=ListEventsArgs, category="calendar",
      description="List events from E.V.'s local calendar in a time range.")
def list_events(a: ListEventsArgs, ctx: ToolContext):
    return {"events": _wrap(lambda: data.list_events(a.start, a.end))}


class CreateEventArgs(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    starts_at: str = Field(..., description="ISO datetime in local time")
    ends_at: Optional[str] = None
    location: str = Field("", max_length=200)
    notes: str = Field("", max_length=2000)


@tool("create_event", level=Level.WRITE, args=CreateEventArgs, category="calendar",
      description="Add an event to E.V.'s local calendar (this does not invite anyone).",
      summarize=lambda a: f"Add event “{a.title}” at {a.starts_at}" + (f" until {a.ends_at}" if a.ends_at else ""))
def create_event(a: CreateEventArgs, ctx: ToolContext):
    return {"created": _wrap(lambda: data.create_event(a.title, a.starts_at, a.ends_at, a.location, a.notes))}


class EventIdArgs(BaseModel):
    event_id: int


@tool("delete_event", level=Level.EXTERNAL, args=EventIdArgs, category="calendar",
      description="Delete an event from the local calendar. Destructive — the user must approve.",
      summarize=lambda a: f"Delete calendar event #{a.event_id}")
def delete_event(a: EventIdArgs, ctx: ToolContext):
    return {"deleted": _wrap(lambda: data.delete_event(a.event_id))}


# ---------------------------------------------------------------- Memory
class SearchMemoriesArgs(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)


@tool("search_memories", level=Level.READ, args=SearchMemoriesArgs, category="memory",
      description="Search the user's saved (approved) memories.")
def search_memories(a: SearchMemoriesArgs, ctx: ToolContext):
    return {"memories": data.list_memories(a.query, visible_to=ctx.agent)}


class SaveMemoryArgs(BaseModel):
    content: str = Field(..., min_length=3, max_length=1000, description="One clear fact or preference, in the third person")
    category: str = Field("general", max_length=40, description="e.g. preferences, health, finance, work, people")


@tool("save_memory", level=Level.WRITE, args=SaveMemoryArgs, category="memory", always_confirm=_memory_confirm,
      description="Propose saving a long-term memory about the user. The user sees and approves it first.",
      summarize=lambda a: f"Remember [{a.category}]: “{a.content}”")
def save_memory(a: SaveMemoryArgs, ctx: ToolContext):
    return {"saved": _wrap(lambda: data.add_memory(a.content, a.category, source="assistant"))}


class UpdateMemoryArgs(BaseModel):
    memory_id: int
    content: str = Field(..., min_length=3, max_length=1000)


@tool("update_memory", level=Level.WRITE, args=UpdateMemoryArgs, category="memory", always_confirm=_memory_confirm,
      description="Correct an existing saved memory (the user approves the new wording).",
      summarize=lambda a: f"Change memory #{a.memory_id} to “{a.content}”")
def update_memory(a: UpdateMemoryArgs, ctx: ToolContext):
    return {"updated": _wrap(lambda: data.update_memory(a.memory_id, a.content))}


class MemoryIdArgs(BaseModel):
    memory_id: int


@tool("delete_memory", level=Level.EXTERNAL, args=MemoryIdArgs, category="memory",
      description="Delete a saved memory. Destructive — the user must approve.",
      summarize=lambda a: f"Forget memory #{a.memory_id}")
def delete_memory(a: MemoryIdArgs, ctx: ToolContext):
    return {"deleted": _wrap(lambda: data.delete_memory(a.memory_id))}


# ---------------------------------------------------------------- Knowledge base
class SearchDocsArgs(BaseModel):
    query: str = Field(..., min_length=2, max_length=300)
    k: int = Field(5, ge=1, le=10, description="How many passages to return")


@tool("search_documents", level=Level.READ, args=SearchDocsArgs, category="knowledge",
      description="Search documents the user added to the Knowledge Base. Returns passages with a 'ref' to cite.")
def search_documents(a: SearchDocsArgs, ctx: ToolContext):
    hits = documents.search(a.query, a.k)
    return {"results": hits, "note": "Cite passages using their 'ref'." if hits else "No matching passages in the Knowledge Base."}


@tool("list_documents", level=Level.READ, args=NoArgs, category="knowledge",
      description="List documents in the user's Knowledge Base.")
def list_documents(a: NoArgs, ctx: ToolContext):
    return {"documents": documents.list_documents()}


# ---------------------------------------------------------------- Files
class PathArgs(BaseModel):
    path: str = Field(..., min_length=1, max_length=1000, description="Absolute path, or relative to an authorized folder")


@tool("list_folder", level=Level.READ, args=PathArgs, category="files", requires=_need_folders,
      description="List files in a folder the user has authorized.")
def list_folder(a: PathArgs, ctx: ToolContext):
    return _wrap(lambda: files.list_dir(a.path))


class ReadFileArgs(PathArgs):
    max_chars: int = Field(20000, ge=500, le=60000)


@tool("read_file", level=Level.READ, args=ReadFileArgs, category="files", requires=_need_folders,
      description="Read a plain-text or code file inside an authorized folder.")
def read_file(a: ReadFileArgs, ctx: ToolContext):
    return _wrap(lambda: files.read_text(a.path, a.max_chars))


class SearchFilesArgs(BaseModel):
    query: str = Field(..., min_length=2, max_length=200)
    folder: Optional[str] = Field(None, description="Limit to this authorized folder")


@tool("search_files", level=Level.READ, args=SearchFilesArgs, category="files", requires=_need_folders, timeout=30,
      description="Find files by name or text content inside authorized folders.")
def search_files(a: SearchFilesArgs, ctx: ToolContext):
    return {"hits": _wrap(lambda: files.search(a.query, a.folder))}


class WriteFileArgs(PathArgs):
    content: str = Field(..., max_length=200_000)
    overwrite: bool = Field(False, description="Replace an existing file (a backup copy is kept)")


@tool("write_file", level=Level.WRITE, args=WriteFileArgs, category="files", requires=_need_folders,
      description="Create (or, with overwrite=true, replace) a text/code file inside a folder marked writable.",
      summarize=lambda a: f"{'Overwrite' if a.overwrite else 'Create'} file {a.path} ({len(a.content)} chars)")
def write_file(a: WriteFileArgs, ctx: ToolContext):
    return _wrap(lambda: files.write_text(a.path, a.content, a.overwrite))


# ---------------------------------------------------------------- System & live data
@tool("system_status", level=Level.READ, args=NoArgs, category="system",
      description="Current CPU, memory, disk, battery, and uptime of the user's computer.")
def system_status(a: NoArgs, ctx: ToolContext):
    snap = system_monitor.snapshot()
    snap["online"] = system_monitor.online()
    return snap


@tool("get_weather", level=Level.READ, args=NoArgs, category="system", timeout=15,
      requires=lambda: None if weather.configured() else "Set a location in Settings → Integrations → Weather.",
      description="Current weather and today's forecast for the user's saved location (Open-Meteo).")
async def get_weather(a: NoArgs, ctx: ToolContext):
    return await weather.current()


@tool("github_review_requests", level=Level.READ, args=NoArgs, category="system", timeout=20,
      requires=lambda: None if github.configured() else "Add a GitHub token in Settings → Integrations.",
      description="Open GitHub pull requests that are waiting for the user's review.")
async def github_review_requests(a: NoArgs, ctx: ToolContext):
    return {"pull_requests": await github.review_requests()}


# ---------------------------------------------------------------- Fire TV (home network, ADB)
def _tv(fn):
    try:
        return fn()
    except firetv.FireTVError as e:
        raise ToolError("error", str(e)) from e


TVButton = Literal["home", "back", "select", "up", "down", "left", "right", "menu", "play_pause", "play", "pause",
                   "next", "previous", "fast_forward", "rewind", "volume_up", "volume_down", "mute", "sleep", "wake"]


class TVRemoteArgs(BaseModel):
    button: TVButton = Field(..., description="Remote button to press")
    times: int = Field(1, ge=1, le=10, description="How many times to press it (e.g. volume steps)")


@tool("tv_remote", level=Level.WRITE, args=TVRemoteArgs, category="home", timeout=45, requires=firetv.setup_issue,
      description="Press a button on the user's Fire TV remote (navigation, play/pause, volume, sleep/wake).",
      summarize=lambda a: f"Fire TV: press {a.button.replace('_', ' ')}" + (f" ×{a.times}" if a.times > 1 else ""))
def tv_remote(a: TVRemoteArgs, ctx: ToolContext):
    return _tv(lambda: firetv.press(a.button, a.times))


class TVOpenArgs(BaseModel):
    app: str = Field(..., min_length=2, max_length=120, description="App name like 'Netflix', or an Android package name")


@tool("tv_open_app", level=Level.WRITE, args=TVOpenArgs, category="home", timeout=30, requires=firetv.setup_issue,
      description="Open an app that is installed on the user's Fire TV.",
      summarize=lambda a: f"Fire TV: open {a.app}")
def tv_open_app(a: TVOpenArgs, ctx: ToolContext):
    return _tv(lambda: firetv.open_app(a.app))


@tool("tv_list_apps", level=Level.READ, args=NoArgs, category="home", timeout=30, requires=firetv.setup_issue,
      description="List the apps the user installed on their Fire TV.")
def tv_list_apps(a: NoArgs, ctx: ToolContext):
    return {"apps": _tv(lambda: firetv.installed_packages(third_party_only=True)),
            "note": "Built-in apps (like YouTube or Prime Video on some models) may not be listed but can still be opened."}


@tool("tv_status", level=Level.READ, args=NoArgs, category="home", timeout=30, requires=firetv.setup_issue,
      description="Check whether the Fire TV is reachable, awake, and which app is in front.")
def tv_status(a: NoArgs, ctx: ToolContext):
    return _tv(firetv.tv_status)


# ---------------------------------------------------------------- Communication
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


class EmailArgs(BaseModel):
    to: str = Field(..., max_length=254)
    subject: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=20000)

    @field_validator("to")
    @classmethod
    def _one_address(cls, v: str) -> str:
        v = v.strip()
        if not EMAIL_RE.match(v):
            raise ValueError("must be a single email address")
        return v


@tool("draft_email", level=Level.PREPARE, args=EmailArgs, category="communication",
      description="Save an email draft for the user to review. Nothing is sent.")
def draft_email(a: EmailArgs, ctx: ToolContext):
    return {"draft": data.create_draft("email", a.to, a.subject, a.body), "sent": False}


@tool("send_email", level=Level.EXTERNAL, args=EmailArgs, category="communication", timeout=30,
      requires=email_smtp.setup_issue,
      description="Send an email via the user's SMTP account. Always requires the user's explicit approval of the exact message.",
      summarize=lambda a: f"Send email to {a.to}\nSubject: {a.subject}\n\n{a.body}")
def send_email(a: EmailArgs, ctx: ToolContext):
    result = email_smtp.send(a.to, a.subject, a.body)
    db.execute("INSERT INTO drafts(kind, recipient, subject, body, status, created_at, sent_at) VALUES('email',?,?,?,'sent',?,?)",
               (a.to, a.subject, a.body, db.now_iso(), db.now_iso()))
    return result


# ---------------------------------------------------------------- Workflows
@tool("list_workflows", level=Level.READ, args=NoArgs, category="workflows",
      description="List scheduled workflows (reminders, briefings, recurring prompts).")
def list_workflows(a: NoArgs, ctx: ToolContext):
    return {"workflows": [{k: j[k] for k in ("id", "name", "kind", "schedule_text", "enabled", "next_run_at", "last_status")} for j in jobs.list_jobs()]}


class ScheduleArgs(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    kind: Literal["reminder", "briefing", "prompt"]
    schedule_type: Literal["once", "daily", "interval"]
    at: Optional[str] = Field(None, description="For 'once': ISO local datetime")
    time: Optional[str] = Field(None, description="For 'daily': HH:MM 24-hour")
    weekdays: Optional[list[int]] = Field(None, description="For 'daily': 0=Mon … 6=Sun; omit for every day")
    minutes: Optional[int] = Field(None, description="For 'interval': minutes between runs (≥5)")
    message: Optional[str] = Field(None, max_length=500, description="Reminder text (kind=reminder)")
    prompt: Optional[str] = Field(None, max_length=2000, description="What E.V. should do each run (kind=prompt; read-only tools)")


def _schedule_summary(a: ScheduleArgs) -> str:
    sched = {"type": a.schedule_type, "at": a.at, "time": a.time, "weekdays": a.weekdays, "minutes": a.minutes}
    try:
        when = jobs.describe_schedule(jobs.validate_schedule(sched))
    except ValueError as e:
        when = f"(invalid schedule: {e})"
    what = a.message or a.prompt or "daily briefing"
    return f"Schedule {a.kind} “{a.name}” — {when}: {what}"


@tool("schedule_workflow", level=Level.WRITE, args=ScheduleArgs, category="workflows", always_confirm=True,
      description="Create a background workflow (reminder, briefing, or recurring read-only prompt). The user must approve it.",
      summarize=_schedule_summary)
def schedule_workflow(a: ScheduleArgs, ctx: ToolContext):
    sched = {"type": a.schedule_type, "at": a.at, "time": a.time, "weekdays": a.weekdays, "minutes": a.minutes}
    payload = {"message": a.message} if a.kind == "reminder" else {"prompt": a.prompt} if a.kind == "prompt" else {}
    return {"workflow": _wrap(lambda: jobs.create_job(a.name, a.kind, sched, payload, created_by="assistant"))}
