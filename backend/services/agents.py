"""Agents and their code-enforced policies.

Three kinds:
  * orchestrator — E.V. herself. You talk to her; she can delegate to specialists.
  * mode         — focused modes of E.V. (Coding, Research…) with a narrower tool list.
  * specialist   — a separate agent E.V. delegates to (F.R.I.D.A.Y. first). It never
                   talks to you directly and never takes actions; it returns a report.

Personality, role, and behaviour live in editable files under prompts/ (see
services/prompts.py). Everything in THIS file is policy, enforced in code no
matter what any prompt says: which tools an agent can use, the highest action
level it may reach, which memories it can read, and its execution limits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..integrations import github, weather
from ..tools.registry import Level
from . import files

# Delegation limits for one reply from E.V. (enforced in services/delegation.py)
MAX_DELEGATIONS_PER_REPLY = 3
MAX_DELEGATION_DEPTH = 1          # specialists can't delegate further


@dataclass(frozen=True)
class MemoryPolicy:
    """Which saved memories an agent may read.

    own_scope: memories saved specifically for this agent (scope = agent id).
    shared_categories: categories of *shared* memories it may also read
                       (None = all shared memories, empty = none)."""
    all_scopes: bool = False
    shared_categories: frozenset[str] | None = None

    def allows(self, agent_id: str, memory: dict[str, Any]) -> bool:
        scope = memory.get("scope") or "shared"
        if self.all_scopes:
            return True
        if scope == agent_id:
            return True
        if scope != "shared":
            return False
        return self.shared_categories is None or (memory.get("category") or "general") in self.shared_categories


@dataclass
class Agent:
    id: str
    name: str
    tagline: str
    brief: str
    tools: list[str] | None = None          # None = every tool except specialist-only ones
    implemented: bool = True
    icon: str = "core"
    needs: list[str] = field(default_factory=list)
    kind: str = "mode"                      # orchestrator | mode | specialist
    prompt_file: str | None = None          # file stem under prompts/, e.g. "FRIDAY"
    max_level: Level | None = None          # highest action level this agent's tools may have
    memory: MemoryPolicy = field(default_factory=lambda: MemoryPolicy(all_scopes=True))
    max_steps: int = 8                      # model calls per delegated task (specialists)
    timeout_s: float = 150.0                # wall-clock limit per delegated task (specialists)
    domain: str = ""                        # one line E.V. sees when choosing whom to delegate to


EV_MEMORY = MemoryPolicy(all_scopes=True)

AGENTS: dict[str, Agent] = {a.id: a for a in [
    Agent("ev", "E.V.", "Central intelligence", "", None, icon="core", kind="orchestrator", prompt_file="EV"),
    Agent("coding", "Coding Agent", "Reads and writes code in authorized folders",
          "You are in Coding mode. Help with code in the user's authorized project folders: read files before "
          "suggesting changes, explain changes plainly, and only write files when asked. Prefer small, reviewable edits.",
          ["list_folder", "read_file", "search_files", "write_file", "search_documents", "create_task", "list_tasks"],
          icon="code", needs=["folders"]),
    Agent("research", "Research Agent", "Answers from your Knowledge Base with citations",
          "You are in Research mode. Search the Knowledge Base before answering, cite every claim drawn from it with "
          "its ref, and say clearly when the documents don't cover something. Web search is not connected.",
          ["search_documents", "list_documents", "search_memories", "read_file", "search_files"], icon="search"),
    Agent("memory", "Memory Agent", "Reviews and maintains saved memories",
          "You are in Memory mode. Help the user review, correct, and organise saved memories. Every change goes "
          "through the approval system.",
          ["search_memories", "save_memory", "update_memory", "delete_memory"], icon="memory"),
    Agent("task", "Task Agent", "Plans tasks, calendar, reminders, and drafts",
          "You are in Planning mode. Turn requests into concrete tasks, events, and reminders. Check existing tasks "
          "and events first to avoid duplicates.",
          ["list_tasks", "create_task", "update_task", "delete_task", "list_events", "create_event", "delete_event",
           "list_workflows", "schedule_workflow", "draft_email", "send_email"], icon="tasks"),
    Agent("system", "System Agent", "Laptop, TV, weather, and GitHub",
          "You are in System mode. Report on the user's computer, Fire TV, weather, and GitHub review queue using "
          "tools, and control the Fire TV when asked. Quote real numbers from tool results only.",
          ["system_status", "get_weather", "github_review_requests", "tv_status", "tv_remote", "tv_open_app", "tv_list_apps"],
          icon="system"),
    Agent("browser", "Browser Agent", "Web browsing and automation",
          "", [], implemented=False, icon="browser"),
    # ---- specialists ------------------------------------------------------------
    Agent("friday", "F.R.I.D.A.Y.", "Trading & market intelligence",
          "", ["market_quote", "market_company_overview", "market_price_history", "market_news"],
          icon="chart", kind="specialist", prompt_file="FRIDAY",
          max_level=Level.READ,                                     # read-only: no trades, no writes
          memory=MemoryPolicy(shared_categories=frozenset({"finance", "investing"})),
          max_steps=6, timeout_s=150.0,
          domain="Stock and company research, market data, price history, market news. Read-only; no trading."),
]}


def specialists() -> list[Agent]:
    return [a for a in AGENTS.values() if a.kind == "specialist" and a.implemented]


def readiness(agent: Agent) -> dict[str, Any]:
    if not agent.implemented:
        return {"status": "not_implemented", "detail": "Planned — browser automation isn't built yet."}
    if "folders" in agent.needs and not files.list_folders():
        return {"status": "setup_required", "detail": "Authorize a project folder in Settings → Folders."}
    if agent.id == "system" and not (weather.configured() or github.configured()):
        return {"status": "ready", "detail": "System metrics ready; weather/GitHub need setup."}
    return {"status": "ready", "detail": agent.tagline}


def get(agent_id: str | None) -> Agent:
    """An agent you can chat with directly. Specialists are reached through E.V."""
    agent = AGENTS.get(agent_id or "ev", AGENTS["ev"])
    return agent if agent.implemented and agent.kind != "specialist" else AGENTS["ev"]


def get_any(agent_id: str) -> Agent | None:
    agent = AGENTS.get(agent_id)
    return agent if agent and agent.implemented else None


def can_read_memory(agent_id: str, memory: dict[str, Any]) -> bool:
    agent = AGENTS.get(agent_id)
    if agent is None:
        return False
    policy = EV_MEMORY if agent.kind == "mode" else agent.memory
    return policy.allows(agent_id, memory)
