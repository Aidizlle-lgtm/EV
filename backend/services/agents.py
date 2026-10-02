"""Agents are focused modes of E.V.: a short brief plus the subset of tools they may use.
They are real configurations, not decoration — the tool list is enforced."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..integrations import github, weather
from . import files


@dataclass
class Agent:
    id: str
    name: str
    tagline: str
    brief: str
    tools: list[str] | None = None          # None = every tool
    implemented: bool = True
    icon: str = "core"
    needs: list[str] = field(default_factory=list)


AGENTS: dict[str, Agent] = {a.id: a for a in [
    Agent("ev", "E.V.", "General assistant", "", None, icon="core"),
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
]}


def readiness(agent: Agent) -> dict[str, Any]:
    if not agent.implemented:
        return {"status": "not_implemented", "detail": "Planned — browser automation isn't built yet."}
    if "folders" in agent.needs and not files.list_folders():
        return {"status": "setup_required", "detail": "Authorize a project folder in Settings → Folders."}
    if agent.id == "system" and not (weather.configured() or github.configured()):
        return {"status": "ready", "detail": "System metrics ready; weather/GitHub need setup."}
    return {"status": "ready", "detail": agent.tagline}


def get(agent_id: str | None) -> Agent:
    agent = AGENTS.get(agent_id or "ev", AGENTS["ev"])
    return agent if agent.implemented else AGENTS["ev"]
