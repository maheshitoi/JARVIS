"""Proactive JARVIS: a daily briefing and live alerts, without being asked.

While Boss is connected to the dashboard:
  - Briefing: the first time Boss connects after `brief_time` each day, JARVIS gives a short spoken
    briefing (websites, new phone messages, failed actions, background tasks, what's in memory for
    today). Optionally Claude writes it (brief_with_claude: true) for a richer analysis.
  - Alerts: every `check_every_minutes`, JARVIS checks for a website going down / coming back,
    SSL about to expire, new phone messages and a burst of failed actions. New alerts pop up on the
    dashboard and JARVIS says them out loud as soon as it isn't talking or listening to Boss.

Settings: access.yaml -> capabilities.proactive.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import audit, config, websites
from .access import DENY, get_policy
from .guard import EVENTS_TOPIC

logger = logging.getLogger("jarvis.proactive")

STATE_FILE = config.DATA_DIR / "proactive_state.json"
PHONE_FILE = config.DATA_DIR / "phone_messages.jsonl"


def _settings() -> dict[str, Any]:
    return get_policy().section("proactive")


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    except json.JSONDecodeError:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _phone_messages() -> list[dict[str, Any]]:
    if not PHONE_FILE.exists():
        return []
    out = []
    for line in PHONE_FILE.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _recent_failures(minutes: int) -> list[dict[str, Any]]:
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    out = []
    for e in audit.tail(400):
        if e.get("status") != "failed":
            continue
        try:
            if datetime.fromisoformat(e["ts"]) >= since:
                out.append(e)
        except (KeyError, ValueError):
            continue
    return out


# ----------------------------------------------------------------- facts --
def snapshot() -> dict[str, Any]:
    """Everything the briefing and alerts look at, in one dict."""
    sites = websites.latest_status()
    return {
        "sites": [{"name": s.get("name"), "up": s.get("up"), "status": s.get("status"),
                   "response_ms": s.get("response_ms"), "ssl_days_left": s.get("ssl_days_left"),
                   "issues": s.get("issues") or []} for s in sites],
        "phone_messages": _phone_messages(),
        "failed_24h": [{"capability": e.get("capability"), "summary": e.get("summary"),
                        "error": (e.get("error") or "")[:160]} for e in _recent_failures(24 * 60)],
    }


def _task_lines() -> list[str]:
    from . import tasks

    return [f"{t.title}: {t.status}" for t in tasks.recent(5)]


# ---------------------------------------------------------------- engine --
class Proactive:
    def __init__(self, session: Any, room: Any) -> None:
        self.session, self.room = session, room
        self.queue: list[str] = []           # things to say once JARVIS is idle
        self.state = _load_state()
        self.task: asyncio.Task | None = None

    async def run(self) -> None:
        from . import digest

        digest.touch_presence()  # tells the away-mode watcher (server.py) that Boss is here
        await asyncio.sleep(8)  # let the greeting / first exchange happen
        while self.room.isconnected():
            cfg = _settings()
            self.state = {**_load_state(), **self.state}  # pick up what the watcher saw while Boss was away
            if get_policy().level("proactive.brief") != DENY:
                await self._maybe_brief(cfg)
            if get_policy().level("proactive.alerts") != DENY:
                await self._check_alerts(cfg)
            self._automation_results()
            await self._speak_queued()
            # Short ticks so queued alerts are spoken soon after JARVIS goes idle.
            for _ in range(max(1, int(cfg.get("check_every_minutes", 5))) * 6):
                if not self.room.isconnected():
                    return
                digest.touch_presence()
                await asyncio.sleep(10)
                if _ % 3 == 2:
                    self._automation_results()
                await self._speak_queued()

    # -------- briefing --------
    async def _maybe_brief(self, cfg: dict[str, Any]) -> None:
        now = datetime.now()
        hh, mm = (str(cfg.get("brief_time", "09:00")).split(":") + ["0"])[:2]
        if self.state.get("last_brief") == now.date().isoformat():
            return
        if (now.hour, now.minute) < (int(hh), int(mm)):
            return
        self.state["last_brief"] = now.date().isoformat()
        _save_state(self.state)
        snap = await asyncio.to_thread(snapshot)
        last = self.state.get("brief_seen_messages", 0)
        snap["new_phone_messages"] = snap["phone_messages"][last:]
        snap.pop("phone_messages")
        self.state["brief_seen_messages"] = last + len(snap["new_phone_messages"])
        _save_state(self.state)
        snap["background_tasks"] = _task_lines()

        from . import digest, memory

        for key, fn, arg in (("calendar_today", digest.calendar_text, 1), ("unread_email", digest.unread_email, 8)):
            try:
                value = await asyncio.to_thread(fn, arg)
                if value is not None:
                    snap[key] = value or "nothing"
            except Exception as e:  # noqa: BLE001 — a broken calendar/email setup shouldn't kill the brief
                snap[key] = f"(couldn't read: {str(e)[:80]})"

        try:
            snap["memory_today"] = [m["fact"] for m in await memory.search(
                f"plans, meetings or deadlines on {now:%A %d %B %Y}", limit=5)]
        except Exception:  # noqa: BLE001
            snap["memory_today"] = []

        text = None
        if cfg.get("brief_with_claude"):
            text = await self._claude_brief(snap)
        owner = get_policy().owner_name
        if text:
            instructions = (f"Give {owner} this daily briefing in your own words, spoken, under 40 seconds. "
                            f"Greet them first.\n\n{text}")
        else:
            instructions = (f"Greet {owner} and give a short daily briefing (under 30 seconds, spoken, no lists) "
                            f"from this data. Lead with anything that needs attention (a site down, failures, "
                            f"messages), then today's meetings and important unread emails (senders/subjects, "
                            f"not the bodies); if everything is fine, say so in one line. Data:\n"
                            f"{json.dumps(snap, default=str)[:6000]}")
        await self._publish({"type": "alert", "level": "info", "title": "Daily briefing",
                             "text": f"Briefing for {now:%A %d %B}"})
        self.queue.insert(0, instructions)
        audit.record({"capability": "proactive.brief", "summary": "Daily briefing", "status": "done", "mode": "owner"})

    async def _claude_brief(self, snap: dict[str, Any]) -> str | None:
        from . import claude_brain

        if not claude_brain.configured():
            return None
        try:
            res = await claude_brain.run_task(
                "Write today's briefing for the owner: what needs attention first, then key numbers from "
                "the business database worth knowing today (compare with yesterday/last week where useful). "
                "Under 120 words, spoken style.",
                extra_context=f"Current status data: {json.dumps(snap, default=str)[:8000]}", effort="medium")
            return res.text
        except Exception:  # noqa: BLE001
            logger.warning("Claude briefing failed; using the quick one", exc_info=True)
            return None

    # -------- alerts --------
    async def _check_alerts(self, cfg: dict[str, Any]) -> None:
        from . import notify

        alerts = await asyncio.to_thread(detect_alerts, self.state, cfg)
        _save_state(self.state)
        owner = get_policy().owner_name
        for level, title, text in alerts:
            await self._publish({"type": "alert", "level": level, "title": title, "text": text})
            if notify.wants(level, away=False):
                await notify.push(title, text, level)
            if level != "info" or cfg.get("speak_info_alerts", True):
                self.queue.append(f"Briefly tell {owner} this alert in one spoken sentence, then stop: {title}. {text}")

    def _automation_results(self) -> None:
        """Queue scheduled-automation results that haven't been spoken yet (one combined message)."""
        from . import automations

        try:
            items = automations.unannounced()
        except Exception:  # noqa: BLE001
            return
        if not items:
            return
        automations.mark_announced([a["id"] for a in items])
        owner = get_policy().owner_name
        body = "\n\n".join(f"[{a['title']}, ran {a['last_run'][11:16]}]\n{(a.get('last_result') or '')[:1500]}"
                           for a in items)
        self.queue.append(f"Scheduled automations finished. Tell {owner} the key point of each in one short "
                          f"spoken sentence each (lead with anything that needs attention), then stop:\n{body}")

    # -------- output --------
    async def _publish(self, event: dict[str, Any]) -> None:
        if not self.room.isconnected():
            return
        try:
            await self.room.local_participant.publish_data(json.dumps(event), topic=EVENTS_TOPIC, reliable=True)
        except Exception:  # noqa: BLE001
            logger.debug("could not publish alert", exc_info=True)

    def _idle(self) -> bool:
        return (getattr(self.session, "agent_state", None) == "listening"
                and getattr(self.session, "user_state", None) != "speaking")

    async def _speak_queued(self) -> None:
        if not self.queue or not self._idle():
            return
        instructions = self.queue.pop(0)
        try:
            self.session.generate_reply(instructions=instructions)
        except Exception:  # noqa: BLE001 — session gone
            logger.debug("could not speak alert", exc_info=True)


# ---------------------------------------------------------------- alerts --
def detect_alerts(state: dict[str, Any], cfg: dict[str, Any]) -> list[tuple[str, str, str]]:
    """New alerts since the last check: [(level, title, text)]. Updates `state` (caller saves it).
    Shared by the live session (Proactive) and the away-mode watcher (background.py), which never
    run at the same time, so every alert fires once."""
    snap = snapshot()
    alerts: list[tuple[str, str, str]] = []  # (level, title, text)
    prev_up: dict[str, bool] = state.get("site_up", {})
    for s in snap["sites"]:
        name, up = s["name"], s["up"]
        if name is None or up is None:
            continue
        if prev_up.get(name, True) and not up:
            alerts.append(("critical", f"{name} is DOWN",
                           f"{name} is not responding (status {s['status']}). Issues: {', '.join(s['issues'][:3]) or 'none listed'}."))
        elif prev_up.get(name) is False and up:
            alerts.append(("info", f"{name} is back up", f"{name} is responding again ({s['response_ms']} ms)."))
        prev_up[name] = up
        days = s.get("ssl_days_left")
        warn_days = int(cfg.get("ssl_warn_days", 14))
        key = f"ssl_warned_{name}"
        if isinstance(days, int) and days <= warn_days and state.get(key) != datetime.now().date().isoformat():
            state[key] = datetime.now().date().isoformat()
            alerts.append(("warning", f"SSL expiring: {name}", f"The SSL certificate of {name} expires in {days} days."))
    state["site_up"] = prev_up

    msgs = snap["phone_messages"]
    seen = state.get("alert_seen_messages")
    if seen is None:
        seen = len(msgs)  # first run: don't alert about old messages
    for m in msgs[seen:]:
        alerts.append(("info", f"Phone message from {m.get('caller_name') or m.get('number')}", m.get("message", "")[:200]))
    state["alert_seen_messages"] = len(msgs)

    window = int(cfg.get("failure_window_minutes", 30))
    limit = int(cfg.get("failure_alert_count", 3))
    fails = _recent_failures(window)
    last_fail_alert = state.get("last_fail_alert")
    if len(fails) >= limit and (not last_fail_alert or
                                datetime.fromisoformat(last_fail_alert) < datetime.now(timezone.utc) - timedelta(minutes=window)):
        state["last_fail_alert"] = datetime.now(timezone.utc).isoformat()
        caps = sorted({f.get("capability", "?") for f in fails})
        alerts.append(("warning", f"{len(fails)} actions failed", f"In the last {window} minutes: {', '.join(caps)}."))

    alerts += _meeting_reminders(state, cfg)
    for level, title, text in alerts:
        audit.record({"capability": "proactive.alert", "summary": title, "details": {"text": text},
                      "status": "done", "mode": "owner"})
    return alerts


def _meeting_reminders(state: dict[str, Any], cfg: dict[str, Any]) -> list[tuple[str, str, str]]:
    from . import calendar_feed

    minutes = int(cfg.get("meeting_reminder_minutes", 10) or 0)
    if minutes <= 0 or not calendar_feed.configured() or get_policy().level("calendar.read") == DENY:
        return []
    try:
        soon = calendar_feed.upcoming(minutes + int(cfg.get("check_every_minutes", 5)))
    except Exception:  # noqa: BLE001 — calendar unreachable: no reminders this round
        return []
    done: list[str] = state.get("reminded", [])
    out = []
    for e in soon:
        if e["uid"] in done:
            continue
        done.append(e["uid"])
        mins = max(0, int((e["start"] - datetime.now(e["start"].tzinfo)).total_seconds() // 60))
        out.append(("warning", f"Meeting in {mins} min: {e['title']}",
                    f"{e['title']} starts at {e['start']:%H:%M}" + (f" ({e['location']})" if e["location"] else "") + "."))
    state["reminded"] = done[-200:]
    return out


def start(session: Any, room: Any) -> Proactive | None:
    if get_policy().level("proactive.brief") == DENY and get_policy().level("proactive.alerts") == DENY:
        return None
    p = Proactive(session, room)
    p.task = asyncio.ensure_future(p.run())
    return p
