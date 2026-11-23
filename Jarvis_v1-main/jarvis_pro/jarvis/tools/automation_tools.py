"""Voice tools for scheduled automations, the calendar and phone notifications."""

from __future__ import annotations

import asyncio

from livekit.agents import RunContext, ToolError, function_tool

from .. import automations, calendar_feed, notify
from ..guard import action


# ------------------------------------------------------------ automations --
@function_tool
async def create_automation(context: RunContext, title: str, job: str, when: str, notify_mode: str = "auto") -> str:
    """Schedule a job JARVIS runs by itself, repeatedly or once, even when the user isn't connected.
    Use for "every Monday...", "each morning...", "every 2 hours check...", "remind me on Friday at 5...".
    Repeat the schedule back to the user after creating it.

    Args:
        title: 2-5 word name, e.g. "Weekly payments check".
        job: Complete instructions with every detail, written so it makes sense on its own later,
             e.g. "Compare last week's successful and failed payments with the week before and point out anything unusual".
        when: Schedule in one of these forms: "daily 09:00", "weekdays 18:30", "weekends 10:00",
              "monday 09:00", "mon,thu 10:30", "every 2 hours", "every 30 minutes", "hourly",
              "once 2026-12-01 09:00". Use 24-hour time.
        notify_mode: "auto" (speak if connected, else push to phone), "push", "speak", "both",
              or "only_if_issue" (stay quiet when everything is fine).
    """
    async with action(context, "automation.manage", f"Schedule: {title} ({when})", {"job": job, "when": when}) as act:
        try:
            a = await asyncio.to_thread(automations.create, title, job, when, notify_mode)
        except automations.ScheduleError as e:
            raise ToolError(str(e)) from e
        act["result"] = a["id"]
    nr = (a["next_run"] or "")[:16].replace("T", " ")
    return f"Scheduled '{a['title']}': {a['schedule']}. First run {nr}. (id {a['id']})"


@function_tool
async def list_automations(context: RunContext) -> str:
    """List the scheduled automations with their schedule, next run and last result."""
    async with action(context, "automation.manage", "List automations"):
        items = await asyncio.to_thread(automations.load)
    if not items:
        return "No automations scheduled."
    lines = []
    for a in items:
        line = f"- {a['title']} (id {a['id']}): {a['schedule']}, {'on' if a['enabled'] else 'paused'}"
        if a.get("next_run") and a["enabled"]:
            line += f", next {a['next_run'][:16].replace('T', ' ')}"
        if a.get("last_run"):
            line += f"; last run {a['last_run'][:16].replace('T', ' ')} ({a['last_status']}): {(a.get('last_result') or '')[:200]}"
        lines.append(line)
    return "\n".join(lines)


@function_tool
async def change_automation(context: RunContext, automation: str, change: str) -> str:
    """Pause, resume, delete or run-now a scheduled automation.

    Args:
        automation: Its id or part of its title.
        change: "pause", "resume", "delete" or "run_now".
    """
    a = automations.find(automation)
    if a is None:
        raise ToolError(f"No automation matches '{automation}'.")
    async with action(context, "automation.manage", f"Automation {change}: {a['title']}"):
        if change == "delete":
            automations.delete(a["id"])
            return f"Deleted '{a['title']}'."
        if change in ("pause", "resume"):
            automations.set_enabled(a["id"], change == "resume")
            return f"'{a['title']}' is {'paused' if change == 'pause' else 'running again on schedule'}."
        if change == "run_now":
            automations.run_soon(a["id"])
            return f"Running '{a['title']}' now; the result comes within a minute or two."
    raise ToolError("change must be pause, resume, delete or run_now.")


# --------------------------------------------------------------- calendar --
@function_tool
async def check_calendar(context: RunContext, day: str = "today") -> str:
    """Read the user's calendar (meetings, appointments, events).

    Args:
        day: "today", "tomorrow", "week" (next 7 days), a weekday name like "friday", or "YYYY-MM-DD".
    """
    if not calendar_feed.configured():
        raise ToolError("No calendar is connected: CALENDAR_ICS_URLS is missing in .env. Tell Boss.")
    try:
        start, end, label = calendar_feed.day_range(day)
    except ValueError as e:
        raise ToolError("Say the day as today, tomorrow, week, a weekday or YYYY-MM-DD.") from e
    async with action(context, "calendar.read", f"Calendar: {label}") as act:
        evs = await asyncio.to_thread(calendar_feed.events, start, end)
        act["result"] = f"{len(evs)} events"
    if not evs:
        return f"Nothing on the calendar for {label}."
    return f"Calendar for {label}:\n" + "\n".join(calendar_feed.describe(evs, with_date=(end - start).days > 1))


# ----------------------------------------------------------------- phone --
@function_tool
async def send_to_phone(context: RunContext, title: str, text: str) -> str:
    """Send a push notification to the user's phone (e.g. "send that to my phone", "remind me on my phone").

    Args:
        title: Short title.
        text: The message (can be a summary, a list, a reminder).
    """
    if not notify.configured():
        raise ToolError("Phone notifications aren't set up: NTFY_TOPIC is missing in .env. Tell Boss.")
    async with action(context, "notify.push", f"Send to phone: {title}"):
        ok = await notify.push(title, text, "result")
    if not ok:
        raise ToolError("The notification server didn't accept it.")
    return "Sent to the phone."


TOOLS = [
    ("automation.manage", create_automation),
    ("automation.manage", list_automations),
    ("automation.manage", change_automation),
    ("calendar.read", check_calendar),
    ("notify.push", send_to_phone),
]
