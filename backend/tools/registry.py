"""Tool registry and the permission gate.

Every tool declares an action level. The rules below are enforced here, in code,
no matter what the model says:

    read              → runs automatically
    prepare           → runs automatically (drafts, plans; nothing leaves the machine)
    reversible_write  → asks you first (you may switch this level to "auto" in Settings)
    external          → ALWAYS asks you first: sending, deleting, purchasing, booking…

Approval is checked twice: once to decide whether to ask, and again at execution
time against the stored approval record (same tool, byte-identical arguments).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from .. import db
from ..providers.base import ToolSpec
from ..services import settings


class Level(str, Enum):
    READ = "read"
    PREPARE = "prepare"
    WRITE = "reversible_write"
    EXTERNAL = "external"


LEVEL_RANK = {Level.READ: 0, Level.PREPARE: 1, Level.WRITE: 2, Level.EXTERNAL: 3}
LEVEL_INFO = {
    Level.READ.value: {"label": "Read", "description": "Reads sources and folders you have explicitly connected.", "policy": "Automatic"},
    Level.PREPARE.value: {"label": "Prepare", "description": "Drafts messages, plans, and suggestions. Nothing is sent or changed outside E.V.", "policy": "Automatic"},
    Level.WRITE.value: {"label": "Reversible write", "description": "Narrow, undoable changes: tasks, events, approved memories, new files in writable folders.", "policy": "Ask (configurable)"},
    Level.EXTERNAL.value: {"label": "External / destructive", "description": "Sending messages, deleting, purchasing, booking, calling. Always requires your explicit approval.", "policy": "Always ask"},
}


class ToolError(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status   # invalid | denied | unavailable | error | timeout
        self.message = message


@dataclass
class ToolContext:
    actor: str = "assistant"            # assistant | job | user | agent:<id>
    conversation_id: int | None = None
    max_level: Level | None = None      # background jobs are capped at PREPARE; specialists at READ
    agent: str = "ev"                   # whose policy applies (memory visibility, delegation rights)
    depth: int = 0                      # 0 = E.V.'s own turn; 1 = inside a specialist
    cancel: Any = None                  # asyncio.Event set when the user presses Stop
    emit: Callable[[dict[str, Any]], None] | None = None   # progress events for the interface
    run_state: dict[str, Any] = field(default_factory=dict)  # per-reply counters (e.g. delegations)


@dataclass
class Tool:
    name: str
    description: str
    level: Level
    args_model: type[BaseModel]
    handler: Callable[..., Any]
    category: str = "general"
    timeout: float = 20.0
    always_confirm: Callable[[], bool] | bool = False
    summarize: Callable[[BaseModel], str] | None = None
    requires: Callable[[], str | None] | None = None   # returns a setup message when unavailable
    tags: set[str] = field(default_factory=set)

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, clean_schema(self.args_model.model_json_schema()))

    def setup_issue(self) -> str | None:
        return self.requires() if self.requires else None


@dataclass
class Prepared:
    tool: Tool
    args: BaseModel
    summary: str
    needs_approval: bool

    @property
    def args_json(self) -> str:
        return canonical_json(self.args.model_dump(mode="json"))


TOOLS: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    TOOLS[tool.name] = tool
    return tool


def tool(name: str, *, level: Level, args: type[BaseModel], description: str, **kw: Any) -> Callable:
    def deco(fn: Callable) -> Callable:
        register(Tool(name=name, description=description, level=level, args_model=args, handler=fn, **kw))
        return fn
    return deco


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make Pydantic's JSON schema friendlier to every provider: drop titles and
    collapse Optional[X] (anyOf [X, null]) into plain X."""
    def walk(node: Any, is_properties: bool = False) -> Any:
        if isinstance(node, dict):
            if is_properties:  # keys here are field names (a field may be called "title")
                return {k: walk(v) for k, v in node.items()}
            node = {k: walk(v, is_properties=(k == "properties")) for k, v in node.items() if k != "title"}
            if "anyOf" in node:
                options = [o for o in node["anyOf"] if o.get("type") != "null"]
                if len(options) == 1:
                    merged = {**{k: v for k, v in node.items() if k != "anyOf"}, **options[0]}
                    if merged.get("default", 0) is None:
                        merged.pop("default")
                    return merged
            if node.get("default", 0) is None:
                node.pop("default")
            return node
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    out = walk(schema)
    out.setdefault("type", "object")
    out.setdefault("properties", {})
    return out


def disabled_tools() -> set[str]:
    return set(settings.get("tools.disabled") or [])


def needs_approval(t: Tool) -> bool:
    if t.level == Level.EXTERNAL:
        return True                                  # hard rule — not configurable
    confirm = t.always_confirm() if callable(t.always_confirm) else t.always_confirm
    if confirm:
        return True
    if t.level == Level.WRITE:
        if t.name in (settings.get("tools.auto_approve") or []):
            return False                             # you chose "run without asking" for this tool
        return settings.get("permissions.reversible_write") != "auto"
    return False


def can_auto_approve(t: Tool) -> bool:
    """Only reversible writes that don't insist on confirmation may skip the prompt."""
    confirm = t.always_confirm() if callable(t.always_confirm) else t.always_confirm
    return t.level == Level.WRITE and not confirm


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[redacted]" if any(s in k.lower() for s in ("password", "token", "secret", "api_key")) else _redact(v))
                for k, v in value.items()}
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + f"… [{len(value)} chars]"
    if isinstance(value, list):
        return [_redact(v) for v in value[:50]]
    return value


def audit(*, actor: str, tool_name: str, level: str, args: Any, status: str, detail: str = "",
          duration_ms: int | None = None, conversation_id: int | None = None) -> None:
    db.execute(
        "INSERT INTO audit_log(ts, actor, tool, level, args, status, detail, duration_ms, conversation_id) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (db.now_iso(), actor, tool_name, level, db.dumps(_redact(args)) if args is not None else None,
         status, (detail or "")[:1000], duration_ms, conversation_id),
    )


def prepare(name: str, raw_args: dict[str, Any] | None, ctx: ToolContext, *, parse_error: str | None = None) -> Prepared:
    t = TOOLS.get(name)
    if t is None:
        audit(actor=ctx.actor, tool_name=name, level="unknown", args=raw_args, status="invalid",
              detail="unknown tool", conversation_id=ctx.conversation_id)
        raise ToolError("invalid", f"There is no tool named '{name}'.")
    level = t.level.value
    if name in disabled_tools():
        audit(actor=ctx.actor, tool_name=name, level=level, args=raw_args, status="denied",
              detail="disabled in Tools & Skills", conversation_id=ctx.conversation_id)
        raise ToolError("denied", f"The tool '{name}' is disabled by the user in Tools & Skills.")
    if ctx.max_level is not None and LEVEL_RANK[t.level] > LEVEL_RANK[ctx.max_level]:
        audit(actor=ctx.actor, tool_name=name, level=level, args=raw_args, status="denied",
              detail=f"exceeds {ctx.max_level.value} limit for {ctx.actor}", conversation_id=ctx.conversation_id)
        raise ToolError("denied", f"'{name}' is a {LEVEL_INFO[level]['label']} action, which isn't allowed here (limit: {LEVEL_INFO[ctx.max_level.value]['label']}).")
    issue = t.setup_issue()
    if issue:
        audit(actor=ctx.actor, tool_name=name, level=level, args=raw_args, status="unavailable",
              detail=issue, conversation_id=ctx.conversation_id)
        raise ToolError("unavailable", f"Setup required: {issue}")
    if parse_error or raw_args is None:
        audit(actor=ctx.actor, tool_name=name, level=level, args=raw_args, status="invalid",
              detail=parse_error or "no arguments", conversation_id=ctx.conversation_id)
        raise ToolError("invalid", f"Tool arguments could not be read ({parse_error or 'missing'}). Send a valid JSON object.")
    try:
        args = t.args_model.model_validate(raw_args)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'input'}: {err['msg']}" for err in e.errors()[:6])
        audit(actor=ctx.actor, tool_name=name, level=level, args=raw_args, status="invalid",
              detail=problems, conversation_id=ctx.conversation_id)
        raise ToolError("invalid", f"Invalid arguments for {name}: {problems}")
    summary = t.summarize(args) if t.summarize else f"{name}({canonical_json(args.model_dump(mode='json'))[:200]})"
    return Prepared(tool=t, args=args, summary=summary, needs_approval=needs_approval(t))


def _verify_approval(prepared: Prepared, approval_id: str | None) -> None:
    if not prepared.needs_approval:
        return
    if not approval_id:
        raise ToolError("denied", "This action requires your approval, and none was given.")
    rec = db.row("SELECT tool, args, status FROM approvals WHERE id = ?", (approval_id,))
    if not rec or rec["status"] != "approved" or rec["tool"] != prepared.tool.name or rec["args"] != prepared.args_json:
        raise ToolError("denied", "Approval record is missing, not approved, or doesn't match this exact action.")


async def run(prepared: Prepared, ctx: ToolContext, *, approval_id: str | None = None) -> Any:
    t = prepared.tool
    args_dump = prepared.args.model_dump(mode="json")
    try:
        _verify_approval(prepared, approval_id)
    except ToolError as e:
        audit(actor=ctx.actor, tool_name=t.name, level=t.level.value, args=args_dump, status="denied",
              detail=e.message, conversation_id=ctx.conversation_id)
        raise
    started = time.monotonic()
    try:
        if inspect.iscoroutinefunction(t.handler):
            coro = t.handler(prepared.args, ctx)
        else:
            coro = asyncio.to_thread(t.handler, prepared.args, ctx)
        result = await asyncio.wait_for(coro, timeout=t.timeout)
    except asyncio.TimeoutError:
        ms = int((time.monotonic() - started) * 1000)
        audit(actor=ctx.actor, tool_name=t.name, level=t.level.value, args=args_dump, status="timeout",
              detail=f"exceeded {t.timeout:.0f}s", duration_ms=ms, conversation_id=ctx.conversation_id)
        raise ToolError("timeout", f"{t.name} timed out after {t.timeout:.0f}s. Nothing was confirmed.")
    except ToolError as e:
        ms = int((time.monotonic() - started) * 1000)
        audit(actor=ctx.actor, tool_name=t.name, level=t.level.value, args=args_dump, status=e.status,
              detail=e.message, duration_ms=ms, conversation_id=ctx.conversation_id)
        raise
    except Exception as e:  # tool bug or OS error — report, never pretend success
        ms = int((time.monotonic() - started) * 1000)
        audit(actor=ctx.actor, tool_name=t.name, level=t.level.value, args=args_dump, status="error",
              detail=f"{type(e).__name__}: {e}", duration_ms=ms, conversation_id=ctx.conversation_id)
        raise ToolError("error", f"{t.name} failed: {e}") from e
    ms = int((time.monotonic() - started) * 1000)
    audit(actor=ctx.actor, tool_name=t.name, level=t.level.value, args=args_dump, status="ok",
          detail=str(result)[:300], duration_ms=ms, conversation_id=ctx.conversation_id)
    return result


def describe_all() -> list[dict[str, Any]]:
    disabled = disabled_tools()
    out = []
    for t in sorted(TOOLS.values(), key=lambda x: (x.category, LEVEL_RANK[x.level], x.name)):
        out.append({
            "name": t.name,
            "description": t.description,
            "level": t.level.value,
            "level_label": LEVEL_INFO[t.level.value]["label"],
            "category": t.category,
            "enabled": t.name not in disabled,
            "requires_approval": needs_approval(t),
            "can_auto_approve": can_auto_approve(t),
            "auto_approve": t.name in (settings.get("tools.auto_approve") or []),
            "setup_issue": t.setup_issue(),
            "timeout_s": t.timeout,
        })
    return out
