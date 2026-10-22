"""Which connection and model each agent thinks with, and how fallback works.

Resolution for an agent:
  1. Its own assignment (agents.<id>.connection / .model / .fallbacks), if set.
  2. Otherwise, a specialist inherits E.V.'s chain.
  3. E.V. without an assignment uses the original AI Core setup: the active
     provider, its model, and the optional fallback provider (unchanged behaviour).

Fallback rules (enforced here, not by the AI):
  * A fallback is only tried when the failure kind allows it (outage, rate limit,
    bad key, missing setup, retired model) and nothing has been streamed yet.
  * After a rate limit (HTTP 429), no other connection in the SAME provider family
    is tried — two keys for one provider are never used to get around its limits.
    A different provider may be tried.
  * A rate-limited family is cooled down until the provider's Retry-After (or 30s)
    has passed; cooled connections are skipped by every agent until then.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..providers.base import Provider, ProviderError
from . import connections, settings

FALLBACK_KINDS = {"connection", "server", "rate_limit", "auth", "setup", "not_found"}
DEFAULT_COOLDOWN_S = 30.0
MAX_COOLDOWN_S = 600.0

_cooldown: dict[str, float] = {}   # family -> monotonic time when it may be used again


@dataclass
class Route:
    connection: str
    model: str
    family: str

    def provider(self) -> Provider:
        return connections.provider_for(self.connection)

    def label(self) -> str:
        try:
            return self.provider().label
        except ProviderError:
            return self.connection


def _route(conn_id: str, model: str | None) -> Route:
    return Route(conn_id, (model or "").strip() or connections.default_model(conn_id), connections.family(conn_id))


def _legacy_ev_chain() -> list[Route]:
    primary = settings.get("llm.provider") or "groq"
    chain = [_route(primary, settings.model_for(primary))]
    fb = settings.get("llm.fallback_provider")
    if fb and fb != primary and connections.exists(fb):
        chain.append(_route(fb, settings.model_for(fb)))
    return chain


def assignment(agent_id: str) -> dict[str, Any]:
    return {"connection": settings.get(f"agents.{agent_id}.connection") or "",
            "model": settings.get(f"agents.{agent_id}.model") or "",
            "fallbacks": settings.get(f"agents.{agent_id}.fallbacks") or []}


def chain_for(agent_id: str) -> list[Route]:
    a = assignment(agent_id)
    if not a["connection"]:
        return _legacy_ev_chain() if agent_id == "ev" else chain_for("ev")
    chain = [_route(a["connection"], a["model"])]
    seen = {(chain[0].connection, chain[0].model)}
    for f in a["fallbacks"]:
        if not isinstance(f, dict) or not connections.exists(f.get("connection", "")):
            continue
        r = _route(f["connection"], f.get("model"))
        if (r.connection, r.model) not in seen:
            chain.append(r)
            seen.add((r.connection, r.model))
    return chain


def describe(agent_id: str) -> dict[str, Any]:
    a = assignment(agent_id)
    chain = chain_for(agent_id)
    return {**a, "inherits": not a["connection"],
            "effective": [{"connection": r.connection, "model": r.model, "label": r.label(),
                           "cooling_down_s": cooling(r.family)} for r in chain]}


# ------------------------------------------------------------------ rate-limit bookkeeping
def cooling(family: str) -> int:
    until = _cooldown.get(family)
    if not until:
        return 0
    left = until - time.monotonic()
    if left <= 0:
        _cooldown.pop(family, None)
        return 0
    return int(left) + 1


def note_failure(route: Route, err: ProviderError) -> None:
    if err.kind == "rate_limit":
        wait = min(max(err.retry_after or DEFAULT_COOLDOWN_S, 1.0), MAX_COOLDOWN_S)
        _cooldown[route.family] = time.monotonic() + wait


def reset_cooldowns() -> None:
    _cooldown.clear()


class Attempts:
    """Walks a chain for one model call, applying the fallback rules above."""

    def __init__(self, chain: list[Route]):
        self.chain = list(chain)
        self.blocked: set[str] = set()     # families ruled out for this call
        self.i = 0

    def first(self) -> Route:
        r = self._next_usable()
        if r is None:
            waits = [cooling(c.family) for c in self.chain]
            wait = min(w for w in waits if w) if any(waits) else 0
            raise ProviderError("rate_limit", f"Every assigned AI connection is cooling down after a rate limit. "
                                f"Try again in about {wait}s, or assign a fallback from a different provider.",
                                retry_after=float(wait) or None)
        return r

    def _next_usable(self) -> Route | None:
        while self.i < len(self.chain):
            r = self.chain[self.i]
            self.i += 1
            if r.family in self.blocked or cooling(r.family):
                continue
            return r
        return None

    def after_failure(self, failed: Route, err: ProviderError, *, streamed: bool) -> Route | None:
        """The next route to try, or None if the error must be shown to the user."""
        note_failure(failed, err)
        if streamed or err.kind not in FALLBACK_KINDS:
            return None
        if err.kind == "rate_limit":
            self.blocked.add(failed.family)
        return self._next_usable()
