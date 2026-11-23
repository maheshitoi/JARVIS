"""Scheduled automations: jobs JARVIS runs on its own, on a schedule, even when nobody is connected.

    "Every Monday at 9, check last week's payments and tell me anything unusual"
    "Every 2 hours, check the websites and notify me only if something is wrong"

Each automation = a job (plain-language instructions) + a schedule. The runner lives in server.py
(always running), so automations fire whether or not the dashboard is open. The result is:
  - pushed to Boss's phone (notify.py) when Boss isn't connected (or always, per automation),
  - spoken by JARVIS the next time Boss is connected and JARVIS is idle,
  - kept with the automation (last result) and in the audit log.
Claude does the job when ANTHROPIC_API_KEY is set (with the database/website/memory tools);
otherwise, or if Claude fails, Gemini answers from the current status, calendar and email digest.

Schedule phrases (case-insensitive):
    daily 09:00 | every day at 6pm | weekdays 18:30 | weekends 10:00
    monday 09:00 | mon,thu 10:30 | every friday at 5pm
    every 30 minutes | every 2 hours | hourly            (minimum 15 minutes)
    once 2026-12-01 09:00
Stored in data/automations.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any

from . import audit, config
from .calendar_feed import tz

logger = logging.getLogger("jarvis.automations")

FILE = config.DATA_DIR / "automations.json"
MIN_INTERVAL = 15  # minutes
MAX_AUTOMATIONS = 30
NOTIFY_MODES = ("auto", "push", "speak", "both", "only_if_issue")
_lock = threading.Lock()
_running: set[str] = set()

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class ScheduleError(ValueError):
    pass


# -------------------------------------------------------------- schedule --
def _time(text: str) -> str:
    m = re.search(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\b", text)
    if not m or (m.group(2) is None and m.group(3) is None):
        raise ScheduleError("Say the time, e.g. 09:00 or 6pm.")
    h, mnt, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if not (0 <= h < 24 and 0 <= mnt < 60):
        raise ScheduleError("That time doesn't exist.")
    return f"{h:02d}:{mnt:02d}"


def parse(when: str) -> dict[str, Any]:
    """Schedule phrase -> spec dict. Raises ScheduleError with a speakable reason."""
    w = " ".join(when.lower().replace(",", " , ").split())
    if not w:
        raise ScheduleError("When should it run?")
    m = re.fullmatch(r"once (\d{4}-\d{2}-\d{2})(?: at)? (.+)", w)
    if m:
        at = datetime.fromisoformat(f"{m.group(1)}T{_time(m.group(2))}").replace(tzinfo=tz())
        if at <= datetime.now(tz()):
            raise ScheduleError("That moment is already in the past.")
        return {"type": "once", "at": at.isoformat()}
    if w in ("hourly", "every hour"):
        return {"type": "interval", "minutes": 60}
    m = re.fullmatch(r"every (\d+) ?(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)", w)
    if m:
        minutes = int(m.group(1)) * (60 if m.group(2).startswith("h") else 1)
        if minutes < MIN_INTERVAL:
            raise ScheduleError(f"The shortest interval is {MIN_INTERVAL} minutes.")
        return {"type": "interval", "minutes": minutes}
    time_ = _time(w)
    words = set(re.findall(r"[a-z]+", w))
    if words & {"weekdays", "weekday"}:
        days = [0, 1, 2, 3, 4]
    elif words & {"weekends", "weekend"}:
        days = [5, 6]
    else:
        if words - _FILLER - set(_DAY_WORDS):
            raise ScheduleError("I didn't understand the schedule. Try 'daily 09:00', 'monday 9am' or 'every 2 hours'.")
        days = sorted({_DAY_WORDS[t] for t in words if t in _DAY_WORDS}) or list(range(7))
    return {"type": "weekly", "days": days, "time": time_}


_FILLER = {"daily", "day", "days", "everyday", "every", "each", "at", "am", "pm", "and", "on", "in", "the"}
_DAY_WORDS = {w: i for i, names in enumerate([
    ("mon", "monday", "mondays"), ("tue", "tues", "tuesday", "tuesdays"), ("wed", "wednesday", "wednesdays"),
    ("thu", "thur", "thurs", "thursday", "thursdays"), ("fri", "friday", "fridays"),
    ("sat", "saturday", "saturdays"), ("sun", "sunday", "sundays")]) for w in names}


def describe(spec: dict[str, Any]) -> str:
    if spec["type"] == "interval":
        m = spec["minutes"]
        if m == 60:
            return "every hour"
        return f"every {m // 60} hours" if m % 60 == 0 else f"every {m} minutes"
    if spec["type"] == "once":
        return f"once on {datetime.fromisoformat(spec['at']):%a %d %b %Y at %H:%M}"
    d = spec["days"]
    days = ("every day" if len(d) == 7 else "weekdays" if d == [0, 1, 2, 3, 4] else "weekends" if d == [5, 6]
            else ", ".join(DAYS[i].title() for i in d))
    return f"{days} at {spec['time']}"


def next_run(spec: dict[str, Any], after: datetime | None = None) -> datetime | None:
    after = after or datetime.now(tz())
    if spec["type"] == "once":
        at = datetime.fromisoformat(spec["at"])
        return at if at > after else None
    if spec["type"] == "interval":
        return after + timedelta(minutes=spec["minutes"])
    hh, mm = map(int, spec["time"].split(":"))
    for ahead in range(8):
        day = (after + timedelta(days=ahead)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if day.weekday() in spec["days"] and day > after:
            return day
    return None


# ----------------------------------------------------------------- store --
def load() -> list[dict[str, Any]]:
    try:
        return json.loads(FILE.read_text(encoding="utf-8")) if FILE.exists() else []
    except (OSError, json.JSONDecodeError):
        logger.warning("automations.json unreadable; treating as empty")
        return []


def _save(items: list[dict[str, Any]]) -> None:
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(FILE)


def _update(fn) -> Any:
    """Read-modify-write under a lock (re-reads the file so the agent and server don't clobber)."""
    with _lock:
        items = load()
        out = fn(items)
        _save(items)
        return out


def create(title: str, job: str, when: str, notify: str = "auto") -> dict[str, Any]:
    spec = parse(when)
    if notify not in NOTIFY_MODES:
        notify = "auto"
    job = job.strip()
    if len(job) < 8:
        raise ScheduleError("Describe the job in a full sentence.")
    item = {"id": uuid.uuid4().hex[:8], "title": (title.strip() or job[:40])[:80], "job": job[:2000],
            "when": when.strip(), "spec": spec, "schedule": describe(spec), "notify": notify,
            "enabled": True, "created": datetime.now(tz()).isoformat(),
            "next_run": (nr.isoformat() if (nr := next_run(spec)) else None),
            "last_run": None, "last_status": None, "last_result": None, "announced": True}

    def add(items):
        if len(items) >= MAX_AUTOMATIONS:
            raise ScheduleError(f"There are already {MAX_AUTOMATIONS} automations; delete one first.")
        items.append(item)
        return item
    return _update(add)


def find(key: str) -> dict[str, Any] | None:
    k = key.strip().lower()
    items = load()
    return (next((a for a in items if a["id"] == k), None)
            or next((a for a in items if k and k in a["title"].lower()), None))


def delete(automation_id: str) -> bool:
    def rm(items):
        before = len(items)
        items[:] = [a for a in items if a["id"] != automation_id]
        return len(items) < before
    return _update(rm)


def set_enabled(automation_id: str, enabled: bool) -> dict[str, Any] | None:
    def upd(items):
        for a in items:
            if a["id"] == automation_id:
                a["enabled"] = enabled
                nr = next_run(a["spec"]) if enabled else None
                a["next_run"] = nr.isoformat() if nr else None
                if enabled and nr is None:
                    a["enabled"] = False  # a "once" in the past can't be re-enabled
                return a
        return None
    return _update(upd)


def run_soon(automation_id: str) -> bool:
    """Ask the runner to do it now (picked up within ~30 s)."""
    def upd(items):
        for a in items:
            if a["id"] == automation_id:
                a["next_run"] = datetime.now(tz()).isoformat()
                a["run_now"] = True
                return True
        return False
    return _update(upd)


def unannounced(max_age_h: int = 12) -> list[dict[str, Any]]:
    since = datetime.now(tz()) - timedelta(hours=max_age_h)
    return [a for a in load() if not a.get("announced", True) and a.get("last_run")
            and datetime.fromisoformat(a["last_run"]) >= since and a.get("notify") in ("auto", "speak", "both",
                                                                                      "only_if_issue")]


def mark_announced(ids: list[str]) -> None:
    def upd(items):
        for a in items:
            if a["id"] in ids:
                a["announced"] = True
    _update(upd)


# ---------------------------------------------------------------- runner --
async def execute(job: str) -> tuple[str, str]:
    """Do the job now. Returns (result text, engine used)."""
    from . import claude_brain, digest

    facts = await digest.gather(calendar_days=7, emails=10)
    facts_json = json.dumps(facts, default=str, ensure_ascii=False)[:12000]
    if claude_brain.configured():
        try:
            res = await claude_brain.run_task(
                job + "\n\nThis is a scheduled automation: nobody is waiting live. Begin with a one-line "
                      "verdict (e.g. 'All good' or 'Needs attention: ...'), then the details.",
                effort=config.env("AUTOMATION_EFFORT") or "medium",
                extra_context=f"Current status, calendar (next 7 days) and unread email:\n{facts_json}")
            return res.text, "claude"
        except Exception as e:  # noqa: BLE001 — e.g. no credits: still do the job with Gemini
            logger.warning("Claude failed for automation (%s); using Gemini", e)
    from .genai_client import agenerate

    resp = await agenerate(
        f"You are JARVIS, a personal assistant, running a scheduled job for your owner. Today is "
        f"{datetime.now(tz()):%A %d %B %Y, %H:%M}.\nJob: {job}\n\nUse ONLY this data (don't invent anything; "
        f"if the job needs data that isn't here, say what's missing):\n{facts_json}\n\n"
        "Begin with a one-line verdict ('All good' or 'Needs attention: ...'), then short details. "
        "Under 150 words, plain text.")
    return (resp.text or "").strip() or "(no answer)", "gemini"


def _needs_attention(text: str) -> bool:
    head = text.strip().splitlines()[0].lower() if text.strip() else ""
    return not head.startswith(("all good", "all fine", "everything is fine", "everything looks good",
                                "no issues", "nothing unusual"))


async def run_one(a: dict[str, Any]) -> None:
    from . import digest, notify

    _running.add(a["id"])
    started = datetime.now(tz())
    try:
        try:
            text, engine = await execute(a["job"])
            status = "done"
        except Exception as e:  # noqa: BLE001
            logger.exception("automation %s failed", a["id"])
            text, engine, status = f"Failed: {e}", "-", "failed"
        away = not digest.owner_present()
        mode = a.get("notify", "auto")
        issue = status == "failed" or _needs_attention(text)
        push = (mode in ("push", "both") or (mode == "auto" and away)
                or (mode == "only_if_issue" and issue and away))
        if push:
            await notify.push(f"JARVIS: {a['title']}", text, "warning" if issue else "result")
        speak = mode in ("auto", "speak", "both") or (mode == "only_if_issue" and issue)

        def upd(items):
            for x in items:
                if x["id"] == a["id"]:
                    x.update(last_run=started.isoformat(), last_status=status, last_result=text[:6000],
                             last_engine=engine, announced=not speak, run_now=False)
                    nr = next_run(x["spec"], datetime.now(tz()))
                    x["next_run"] = nr.isoformat() if nr else None
                    if nr is None:
                        x["enabled"] = False  # "once" finished
        _update(upd)
        audit.record({"capability": "automation.run", "summary": f"Automation: {a['title']}"[:160],
                      "status": status, "mode": "owner", "result": f"{engine}; {text[:200]}",
                      **({"error": text[:300]} if status == "failed" else {})})
    finally:
        _running.discard(a["id"])


async def tick() -> None:
    """Start every automation that is due (called every ~30 s by background.py)."""
    from .access import DENY, get_policy

    if get_policy().level("automation.run") == DENY:
        return
    now = datetime.now(tz())
    for a in load():
        if not a.get("enabled") or a["id"] in _running or not a.get("next_run"):
            continue
        due = datetime.fromisoformat(a["next_run"])
        if due <= now:
            if now - due > timedelta(hours=6) and not a.get("run_now"):
                # Missed while the PC was off: don't fire stale runs; schedule the next one.
                def skip(items, aid=a["id"]):
                    for x in items:
                        if x["id"] == aid:
                            nr = next_run(x["spec"], now)
                            x["next_run"] = nr.isoformat() if nr else None
                            x["enabled"] = nr is not None
                _update(skip)
                continue
            asyncio.ensure_future(run_one(a))
