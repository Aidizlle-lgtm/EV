"""GitHub (read-only): pull requests waiting for your review. Needs GITHUB_TOKEN."""
from __future__ import annotations

import time
from typing import Any

import httpx

from .. import config
from ..services import status

_cache: dict[str, Any] = {"at": 0.0, "data": None}
CACHE_SECONDS = 300


def configured() -> bool:
    return bool(config.get_secret("GITHUB_TOKEN"))


async def review_requests(force: bool = False) -> list[dict[str, Any]]:
    token = config.get_secret("GITHUB_TOKEN")
    if not token:
        raise LookupError("Add a GitHub token in Settings → Integrations to see review requests.")
    if not force and _cache["data"] is not None and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "EV-Personal-Assistant",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://api.github.com/search/issues", headers=headers,
                                 params={"q": "is:open is:pr review-requested:@me archived:false", "per_page": 20})
    except httpx.HTTPError as e:
        status.record("github", False, f"GitHub unreachable: {type(e).__name__}")
        raise
    if r.status_code in (401, 403):
        status.record("github", False, f"GitHub rejected the token (HTTP {r.status_code}). It may be expired or missing scopes.")
        raise PermissionError(status.last("github")["message"])  # type: ignore[index]
    r.raise_for_status()
    items = []
    for it in r.json().get("items", []):
        repo = "/".join((it.get("repository_url") or "").split("/")[-2:])
        items.append({"title": it.get("title"), "url": it.get("html_url"), "number": it.get("number"),
                      "repo": repo, "author": (it.get("user") or {}).get("login"), "updated_at": it.get("updated_at")})
    _cache.update(at=time.time(), data=items)
    status.record("github", True, f"{len(items)} review request(s)")
    return items
