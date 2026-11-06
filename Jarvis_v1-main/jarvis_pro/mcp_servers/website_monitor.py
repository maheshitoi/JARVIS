"""JARVIS Website Monitor — MCP server.

Exposes your websites (listed in access.yaml -> capabilities.websites.sites) as MCP tools.
JARVIS starts it automatically. Any other MCP client can use it too, e.g. Claude Desktop:

    "mcpServers": {
      "jarvis-websites": {
        "command": "python",
        "args": ["D:/project/jarvis/JARVIS/Jarvis_v1-main/jarvis_pro/mcp_servers/website_monitor.py"]
      }
    }

Permissions are enforced HERE (not by the client): every tool re-checks access.yaml.
Transport: stdio (default) or `--http` for streamable HTTP on 127.0.0.1:8765/mcp.
"""

from __future__ import annotations

import functools
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from jarvis import audit, websites  # noqa: E402
from jarvis.access import DENY, get_policy  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)

server = MCPServer(
    name="jarvis-website-monitor",
    log_level="WARNING",
    instructions=(
        "Monitors the owner's websites: uptime, speed, SSL expiry, required text, basic SEO and broken "
        "links, plus daily reports. Only sites configured in access.yaml can be checked."
    ),
)


class Refused(Exception):
    """A user-facing problem; its message is returned to the client as the tool result."""


def _require(action: str) -> None:
    if get_policy().level(f"websites.{action}") == DENY:
        audit.record({"capability": f"websites.{action}", "summary": "MCP call blocked", "status": "blocked",
                      "mode": "mcp"})
        raise Refused(f"Access denied: 'websites.{action}' is turned off in access.yaml.")


def _site(name_or_url: str) -> dict:
    site = websites.find_site(name_or_url)
    if not site:
        names = ", ".join(s["name"] for s in websites.sites()) or "none configured"
        raise Refused(f"'{name_or_url}' is not one of the monitored sites ({names}). "
                      "Add it under websites.sites in access.yaml.")
    return site


def friendly(fn):
    """Return Refused messages as text instead of an opaque MCP error."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Refused as e:
            return str(e)
    return wrapper


@server.tool(description="List the websites being monitored with their latest known status.")
@friendly
async def list_websites() -> str:
    _require("check")
    rows = websites.latest_status()
    if not rows:
        return "No websites configured. Add them under capabilities.websites.sites in access.yaml."
    lines = []
    for r in rows:
        state = "unknown (not checked yet)" if r.get("up") is None else ("UP" if r["up"] else "DOWN")
        extra = f", {r['response_ms']} ms" if r.get("response_ms") else ""
        when = f", checked {r['ts'][11:16]} UTC" if r.get("ts") else ""
        lines.append(f"- {r['name']} ({r['url']}): {state}{extra}{when}")
    return "\n".join(lines)


@server.tool(description="Run a live check on one monitored website now. full=true also scans for broken "
                         "internal links (slower).")
@friendly
async def check_website(site: str, full: bool = False) -> str:
    _require("check")
    result = await websites.check_site(_site(site), full=full)
    audit.record({"capability": "websites.check", "summary": websites.summarise(result), "status": "done",
                  "mode": "mcp"})
    return websites.summarise(result)


@server.tool(description="Run a quick live check on ALL monitored websites and summarise.")
@friendly
async def check_all_websites() -> str:
    _require("check")
    results = await websites.check_all(full=False)
    if not results:
        return "No websites configured."
    audit.record({"capability": "websites.check", "summary": f"Checked {len(results)} website(s)",
                  "status": "done", "mode": "mcp"})
    return "\n".join(websites.summarise(r) for r in results)


@server.tool(description="Uptime and response-time statistics for one website over the last N hours "
                         "(from the background monitor's history).")
@friendly
async def website_history(site: str, hours: int = 24) -> str:
    _require("report")
    s = _site(site)
    st = websites.stats(websites.history(hours=max(1, min(hours, 24 * 30)), name=s["name"]))
    if not st["checks"]:
        return f"No history for {s['name']} yet. Is the background monitor running (workers/site_monitor.py)?"
    downs = st["down_events"]
    return (f"{s['name']} over {hours} h: {st['uptime_pct']}% uptime in {st['checks']} checks, "
            f"avg {st['avg_ms']} ms, max {st['max_ms']} ms, "
            + (f"down {len(downs)} time(s), last at {downs[-1][11:16]} UTC." if downs else "no downtime."))


@server.tool(description="Get a saved daily website report (Markdown). date is YYYY-MM-DD; empty = latest.")
@friendly
async def get_website_report(date: str = "") -> str:
    _require("report")
    report = websites.read_report(date or None)
    if report is None:
        available = ", ".join(websites.list_reports()[:7]) or "none yet"
        return f"No report for {date or 'any day'}. Available: {available}. Use generate_website_report to make one."
    return report


@server.tool(description="Build today's full website report now (checks every site including broken links). "
                         "Takes up to a minute. Returns a short summary; the full report is saved and shown on "
                         "the dashboard.")
@friendly
async def generate_website_report() -> str:
    _require("report")
    report = await websites.build_daily_report()
    return report["headline"] + "\n" + "\n".join(report["summary"])


if __name__ == "__main__":
    if "--http" in sys.argv:
        server.run("streamable-http", host="127.0.0.1", port=8765)
    else:
        server.run("stdio")
