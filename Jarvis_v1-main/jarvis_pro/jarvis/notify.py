"""Push notifications to Boss's phone through ntfy (https://ntfy.sh — free app for Android/iOS).

Setup (.env):
    NTFY_TOPIC=jarvis-<long-random-words>   # anyone who knows the topic can read it: make it unguessable
    NTFY_SERVER=https://ntfy.sh             # optional: your own self-hosted ntfy server
    NTFY_TOKEN=tk_...                       # optional: access token for a protected topic/server
Then install the ntfy app on the phone and subscribe to the same topic.

Nothing is sent while NTFY_TOPIC is empty. Which alerts are pushed: access.yaml -> capabilities.notify.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import audit, config
from .access import DENY, get_policy

logger = logging.getLogger("jarvis.notify")

_PRIORITY = {"critical": "5", "warning": "4", "info": "3", "result": "3"}  # ntfy 1-5
_TAGS = {"critical": "rotating_light", "warning": "warning", "info": "information_source", "result": "robot"}


def configured() -> bool:
    return bool(config.env("NTFY_TOPIC"))


def settings() -> dict[str, Any]:
    return get_policy().section("notify")


def wants(level: str, *, away: bool) -> bool:
    """Should an alert of this level be pushed right now? (away = no dashboard session is open)"""
    if not configured() or get_policy().level("notify.push") == DENY:
        return False
    cfg = settings()
    if not away and cfg.get("only_when_away", True):
        return False
    return level in (cfg.get("levels") or ["critical", "warning"])


async def push(title: str, text: str, level: str = "info", *, click: str | None = None) -> bool:
    """Send one notification. Returns True when the server accepted it. Never raises."""
    if not configured() or get_policy().level("notify.push") == DENY:
        return False
    server = (config.env("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    body: dict[str, Any] = {"topic": config.env("NTFY_TOPIC"), "title": title[:200] or "JARVIS",
                            "message": text[:3500] or title, "priority": int(_PRIORITY.get(level, "3")),
                            "tags": [_TAGS.get(level, "robot")]}
    if click or config.env("JARVIS_PUBLIC_URL"):
        body["click"] = click or config.env("JARVIS_PUBLIC_URL")
    headers = {"Authorization": f"Bearer {config.env('NTFY_TOKEN')}"} if config.env("NTFY_TOKEN") else {}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(server, json=body, headers=headers)  # JSON publish: UTF-8 safe
        ok = r.status_code < 300
        if not ok:
            logger.warning("ntfy refused the notification: %s %s", r.status_code, r.text[:200])
    except httpx.HTTPError as e:
        logger.warning("could not reach ntfy: %s", e)
        ok = False
    audit.record({"capability": "notify.push", "summary": f"Push: {title}"[:160],
                  "status": "done" if ok else "failed", "mode": "owner",
                  **({} if ok else {"error": "notification server unreachable or refused"})})
    return ok
