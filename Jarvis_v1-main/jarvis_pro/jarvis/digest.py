"""One place that gathers "what's going on": websites, phone messages, failures, calendar, unread email.

Used by the daily briefing (proactive.py), scheduled automations (automations.py) and the
away-mode watcher (background.py). Each source is optional and skipped when it isn't configured
or is denied in access.yaml, so a missing email/calendar setup never breaks the briefing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import calendar_feed, config
from .access import DENY, get_policy

logger = logging.getLogger("jarvis.digest")

PRESENCE_FILE = config.DATA_DIR / "presence.json"


# ------------------------------------------------------------- presence --
def touch_presence() -> None:
    """Called every few seconds by the owner's voice session (proactive.py)."""
    import time

    try:
        tmp = PRESENCE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
        tmp.replace(PRESENCE_FILE)
    except OSError:
        pass


def owner_present(max_age_s: int = 45) -> bool:
    """Is Boss connected to the dashboard right now (an owner voice session is running)?"""
    import time

    try:
        return time.time() - json.loads(Path(PRESENCE_FILE).read_text(encoding="utf-8"))["ts"] < max_age_s
    except (OSError, ValueError, KeyError):
        return False


# --------------------------------------------------------------- sources --
def calendar_text(days: int = 1) -> list[str] | None:
    if not calendar_feed.configured() or get_policy().level("calendar.read") == DENY:
        return None
    start, _, _ = calendar_feed.day_range("today")
    evs = calendar_feed.events(start, start + timedelta(days=days))
    return calendar_feed.describe(evs, with_date=days > 1)


def unread_email(count: int = 8) -> list[dict[str, str]] | None:
    if not config.env("EMAIL_ADDRESS") or get_policy().level("email.read") == DENY:
        return None
    from .tools.email_tools import fetch_emails

    return [{"from": m["from"], "subject": m["subject"], "preview": m["body"][:160]}
            for m in fetch_emails(count, True, "")]


async def gather(*, calendar_days: int = 1, emails: int = 8) -> dict[str, Any]:
    """Everything worth knowing right now, as plain data (safe to json.dumps)."""
    from .proactive import snapshot

    async def safe(fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except Exception as e:  # noqa: BLE001
            logger.info("digest source %s failed: %s", getattr(fn, "__name__", fn), e)
            return {"error": str(e)[:200]}

    snap, cal, mail = await asyncio.gather(safe(snapshot), safe(calendar_text, calendar_days),
                                           safe(unread_email, emails))
    out: dict[str, Any] = dict(snap) if isinstance(snap, dict) and "error" not in snap else {}
    if "phone_messages" in out:
        out["phone_messages"] = out["phone_messages"][-5:]
    if cal is not None:
        out["calendar"] = cal
    if mail is not None:
        out["unread_email"] = mail
    return out
