"""Read-only calendar from private iCal (ICS) links — Google Calendar, Outlook, Apple, Zoho, etc.

Setup (.env), one or more links separated by commas, optionally named with "Name|":
    CALENDAR_ICS_URLS=Work|https://calendar.google.com/calendar/ical/.../private-.../basic.ics
Google: Calendar settings -> your calendar -> "Secret address in iCal format".
Outlook: Settings -> Calendar -> Shared calendars -> Publish a calendar -> ICS link.
The link is a password: keep it in .env only.

Repeating meetings are expanded (recurring-ical-events). Feeds are cached for a few minutes.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import config
from .access import get_policy

logger = logging.getLogger("jarvis.calendar")

CACHE_S = 300
_cache: dict[str, tuple[float, Any]] = {}


def tz() -> ZoneInfo:
    name = (get_policy().data.get("owner") or {}).get("timezone") or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def feeds() -> list[tuple[str, str]]:
    out = []
    for i, part in enumerate(p.strip() for p in (config.env("CALENDAR_ICS_URLS") or "").split(",")):
        if not part:
            continue
        name, _, url = part.partition("|") if "|" in part.split("://")[0] else ("", "", part)
        url = url.replace("webcal://", "https://", 1)
        out.append((name.strip() or f"Calendar {i + 1}", url.strip()))
    return out


def configured() -> bool:
    return bool(feeds())


def _load(url: str):
    import icalendar

    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    r = httpx.get(url, timeout=20, follow_redirects=True)
    r.raise_for_status()
    cal = icalendar.Calendar.from_ical(r.content)
    _cache[url] = (time.time(), cal)
    return cal


def _as_local(value: date | datetime) -> tuple[datetime, bool]:
    """(local datetime, all_day)"""
    if isinstance(value, datetime):
        return (value.astimezone(tz()) if value.tzinfo else value.replace(tzinfo=tz())), False
    return datetime(value.year, value.month, value.day, tzinfo=tz()), True


def events(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """All events overlapping [start, end) from every feed, sorted by start time."""
    import recurring_ical_events

    out: list[dict[str, Any]] = []
    errors = []
    for name, url in feeds():
        try:
            cal = _load(url)
            for ev in recurring_ical_events.of(cal).between(start, end):
                if str(ev.get("STATUS", "")).upper() == "CANCELLED":
                    continue
                s, all_day = _as_local(ev.decoded("DTSTART"))
                e_raw = ev.decoded("DTEND") if ev.get("DTEND") else None
                e = _as_local(e_raw)[0] if e_raw is not None else s + (timedelta(days=1) if all_day else timedelta(hours=1))
                out.append({
                    "calendar": name,
                    "title": str(ev.get("SUMMARY", "(no title)")),
                    "start": s, "end": e, "all_day": all_day,
                    "location": str(ev.get("LOCATION", "") or ""),
                    "uid": str(ev.get("UID", "")) + s.isoformat(),
                })
        except Exception as e:  # noqa: BLE001 — one broken feed shouldn't hide the others
            logger.warning("calendar %s failed: %s", name, e)
            errors.append(f"{name}: {type(e).__name__}")
    out.sort(key=lambda x: (x["start"].date(), not x["all_day"], x["start"]))
    if errors and not out:
        raise RuntimeError("Couldn't read the calendar (" + "; ".join(errors) + ").")
    return out


def day_range(day: str) -> tuple[datetime, datetime, str]:
    """'today' / 'tomorrow' / 'week' / 'YYYY-MM-DD' / weekday name -> (start, end, label)."""
    now = datetime.now(tz())
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    d = (day or "today").strip().lower()
    if d in ("", "today"):
        return today, today + timedelta(days=1), "today"
    if d == "tomorrow":
        return today + timedelta(days=1), today + timedelta(days=2), "tomorrow"
    if d in ("week", "this week", "next 7 days"):
        return today, today + timedelta(days=7), "the next 7 days"
    names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if d in names:
        ahead = (names.index(d) - today.weekday()) % 7
        s = today + timedelta(days=ahead)
        return s, s + timedelta(days=1), f"{d.title()} {s:%d %b}"
    s = datetime.fromisoformat(d).replace(tzinfo=tz())
    return s, s + timedelta(days=1), f"{s:%A %d %b}"


def describe(evs: list[dict[str, Any]], *, with_date: bool = False) -> list[str]:
    lines = []
    for e in evs:
        when = "all day" if e["all_day"] else f"{e['start']:%H:%M}-{e['end']:%H:%M}"
        if with_date:
            when = f"{e['start']:%a %d %b} {when}"
        lines.append(f"{when}: {e['title']}" + (f" @ {e['location']}" if e["location"] else "")
                     + (f" [{e['calendar']}]" if len(feeds()) > 1 else ""))
    return lines


def upcoming(minutes: int) -> list[dict[str, Any]]:
    """Timed events starting within the next `minutes` (for reminders)."""
    now = datetime.now(tz())
    return [e for e in events(now, now + timedelta(minutes=minutes))
            if not e["all_day"] and now <= e["start"] <= now + timedelta(minutes=minutes)]
