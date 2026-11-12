"""Device intelligence: JARVIS knows every device using it and can manage their permissions.

Owner only. Guest devices never get these tools (devices.* can't be granted to a guest).
    "Which devices are online?"            -> list_devices
    "Any permission requests?"             -> list_devices (shows pending requests)
    "Allow Ravi's PC to use the keyboard"  -> set_device_permission (needs Boss's approval)
    "Approve that request"                 -> answer_permission_request (needs Boss's approval)
"""

from __future__ import annotations

from datetime import datetime

from livekit.agents import RunContext, ToolError, function_tool

from .. import devices
from ..access import get_policy
from ..guard import action

LEVELS = ("allow", "confirm", "deny", "default")


def _ago(ts: int | None) -> str:
    if not ts:
        return "never"
    secs = int(datetime.now().timestamp()) - ts
    if secs < 120:
        return "just now"
    if secs < 7200:
        return f"{secs // 60} min ago"
    if secs < 172800:
        return f"{secs // 3600} h ago"
    return datetime.fromtimestamp(ts).strftime("%d %b")


def _find(name: str) -> dict:
    d = devices.find(name)
    if d is None:
        names = ", ".join(x["name"] for x in devices.all_devices()) or "none registered"
        raise ToolError(f"No single device matches '{name}'. Devices: {names}.")
    return d


@function_tool
async def list_devices(context: RunContext) -> str:
    """List every device registered for the JARVIS Device app: online or not, role (owner/guest),
    last seen, permissions you changed for it, and pending permission requests waiting for Boss."""
    async with action(context, "devices.view", "List devices and permission requests"):
        rows = devices.all_devices()
        online = await devices.online_ids()
        policy = get_policy()
        if not rows:
            return "No devices are registered. Add one with: python manage_devices.py add \"Name\" --role guest"
        lines = []
        for d in rows:
            state = "ONLINE" if d["id"] in online else "offline"
            if not d.get("enabled", True):
                state = "DISABLED"
            custom = ", ".join(f"{c}={lvl}" for c, lvl in sorted(d["permissions"].items())) or "defaults"
            usable = [r["capability"] for r in devices.permission_table(d, policy) if r["effective"] != "deny"]
            lines.append(f"- {d['name']} ({d['role']}, {state}, last seen {_ago(d.get('last_seen'))}); "
                         f"custom permissions: {custom}; can use: {', '.join(usable) or 'nothing'}")
        pending = devices.requests("pending")
        if pending:
            lines.append("Pending permission requests:")
            lines += [f"- request {r['id']}: {r['device_name']} wants '{r['capability']}' "
                      f"({r['summary']}), {_ago(r['ts'])}" for r in pending[:10]]
        else:
            lines.append("No pending permission requests.")
        return "\n".join(lines)


@function_tool
async def set_device_permission(context: RunContext, device: str, capability: str, level: str,
                                confirmed: bool = False) -> str:
    """Change what one device may do. Only when Boss explicitly asks.

    Args:
        device: Device name (or part of it), e.g. "Ravi's PC".
        capability: Capability id, e.g. "screen.control", "screen.view", "system.open_app", "memory.read".
        level: allow, confirm, deny, or default (back to the access.yaml / role default).
        confirmed: Set true ONLY after Boss verbally confirmed (when asked to).
    """
    level = level.strip().lower()
    if level not in LEVELS:
        raise ToolError(f"Level must be one of {', '.join(LEVELS)}.")
    d = _find(device)
    async with action(context, "devices.manage", f"Set {capability} = {level} on {d['name']}",
                      {"device": d["name"], "capability": capability, "level": level}, confirmed):
        try:
            devices.set_permission(d["id"], capability.strip(), level)
        except ValueError as e:
            raise ToolError(str(e)) from e
    return f"{d['name']}: {capability} is now {level}."


@function_tool
async def answer_permission_request(context: RunContext, request_id: str, decision: str,
                                    confirmed: bool = False) -> str:
    """Grant or refuse a pending permission request from a device. Only when Boss says so.

    Args:
        request_id: The request id from list_devices.
        decision: allow (grant), confirm (grant, but the device user approves each time) or deny.
        confirmed: Set true ONLY after Boss verbally confirmed (when asked to).
    """
    decision = decision.strip().lower()
    req = next((r for r in devices.requests("pending") if r["id"] == request_id.strip()), None)
    if req is None:
        raise ToolError("No pending request with that id. Use list_devices to see pending requests.")
    async with action(context, "devices.manage",
                      f"{decision} request: {req['device_name']} wants {req['capability']}",
                      {"request": req["id"], "decision": decision}, confirmed):
        try:
            devices.resolve_request(req["id"], decision)
        except ValueError as e:
            raise ToolError(str(e)) from e
    return f"Done: {req['device_name']} {req['capability']} -> {decision}."


TOOLS = [
    ("devices.view", list_devices),
    ("devices.manage", set_device_permission),
    ("devices.manage", answer_permission_request),
]
