"""Reports from JARVIS's own data: the activity log, and saved daily website reports."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from livekit.agents import RunContext, ToolError, function_tool

from .. import audit, websites
from ..access import get_policy
from ..guard import action

_FINAL = ("done", "failed", "denied", "blocked", "needs_confirmation")


def _tz():
    name = (get_policy().data.get("owner") or {}).get("timezone") or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _day(text: str) -> date:
    """'' / today / yesterday / YYYY-MM-DD / DD-MM-YYYY -> a date in the owner's timezone."""
    t = (text or "").strip().lower()
    today = datetime.now(_tz()).date()
    if t in ("", "today"):
        return today
    if t == "yesterday":
        return today - timedelta(days=1)
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    raise ToolError("Give the date as YYYY-MM-DD (or 'today' / 'yesterday').")


def _local(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts).astimezone(_tz())
    except (TypeError, ValueError):
        return None


@function_tool
async def activity_report(context: RunContext, date: str = "", search: str = "") -> str:
    """Exact report of what JARVIS did on a given day, from the activity log: every action with
    its time and outcome (done, failed, denied, blocked), plus totals.

    Args:
        date: YYYY-MM-DD, "today" or "yesterday". Empty = today.
        search: Optional word to filter by, e.g. "youtube", "whatsapp", "Test Guest PC", "failed".
    """
    day = _day(date)
    async with action(context, "reports.activity", f"Activity report for {day.isoformat()}") as act:
        if not audit.AUDIT_FILE.exists():
            return "The activity log is empty."
        latest: dict[str, dict] = {}
        with audit.AUDIT_FILE.open(encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                when = _local(e.get("ts", ""))
                if when is None or when.date() != day:
                    continue
                key = e.get("id") or f"{e.get('ts')}-{e.get('capability')}-{e.get('status')}"
                e["_when"] = when
                latest[key] = e  # later lines of the same action replace earlier ones
        rows = list(latest.values())
        if search:
            q = search.lower()
            rows = [e for e in rows if q in json.dumps(e, default=str).lower()]
        act["result"] = f"{len(rows)} actions"
        if not rows:
            return f"No activity on {day.strftime('%d %B %Y')}" + (f" matching '{search}'." if search else ".")
        rows.sort(key=lambda e: e["_when"])
        status = Counter(e.get("status", "?") for e in rows)
        caps = Counter(e.get("capability", "?") for e in rows)
        out = [f"Activity on {day.strftime('%A %d %B %Y')}" + (f" matching '{search}'" if search else "")
               + f": {len(rows)} actions.",
               "By outcome: " + ", ".join(f"{k} {v}" for k, v in status.most_common()),
               "By capability: " + ", ".join(f"{k} {v}" for k, v in caps.most_common(12)),
               "", "Time | capability | outcome | what"]
        shown = rows[-80:]
        for e in shown:
            why = e.get("error") or e.get("reason") or ""
            out.append(f"{e['_when'].strftime('%H:%M:%S')} | {e.get('capability', '?')} | "
                       f"{e.get('status', '?')} | {e.get('summary', '')}" + (f" ({why})" if why else ""))
        if len(rows) > len(shown):
            out.append(f"(only the last {len(shown)} of {len(rows)} are listed; use search to narrow it)")
        return "\n".join(out)


@function_tool
async def website_report(context: RunContext, date: str = "") -> str:
    """Get the saved daily website report (uptime, speed, SSL, issues) for a day, word for word.

    Args:
        date: YYYY-MM-DD, "today" or "yesterday". Empty = the latest report.
    """
    day = _day(date).isoformat() if date.strip() else None
    async with action(context, "websites.report", f"Read website report {day or '(latest)'}"):
        text = websites.read_report(day)
    if text is None:
        available = ", ".join(websites.list_reports()[:7]) or "none yet"
        return f"No website report for {day or 'any day'}. Available: {available}."
    return re.sub(r"\n{3,}", "\n\n", text)


OWNER_TOOLS = [("reports.activity", activity_report)]
# Owner sessions already get website reports from the website-monitor MCP server.
GUEST_TOOLS = [("websites.report", website_report)]
