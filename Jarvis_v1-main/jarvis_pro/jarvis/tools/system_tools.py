"""Windows PC tools: status, open allowed apps, open websites."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import webbrowser
from datetime import datetime
from urllib.parse import urlparse

import psutil
from livekit.agents import RunContext, ToolError, function_tool

from .. import device_link
from ..access import get_policy
from ..guard import action, owner_is_remote, publish_event


def _status() -> str:
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(os.path.abspath(os.sep))
    parts = [
        f"Time: {datetime.now().strftime('%A %d %B %Y, %I:%M %p')}",
        f"CPU: {cpu:.0f}%",
        f"RAM: {mem.percent:.0f}% used ({mem.used / 2**30:.1f} of {mem.total / 2**30:.1f} GB)",
        f"Disk: {disk.percent:.0f}% used, {disk.free / 2**30:.0f} GB free",
    ]
    battery = psutil.sensors_battery()
    if battery:
        plug = "charging" if battery.power_plugged else "on battery"
        parts.append(f"Battery: {battery.percent:.0f}% ({plug})")
    return "\n".join(parts)


@function_tool
async def system_status(context: RunContext) -> str:
    """Get the current date/time and health of the user's device: CPU, RAM, disk and battery."""
    async with action(context, "system.info", "Read PC status") as act:
        if device_link.active(context):
            return (await device_link.call(context, "info", event=act))["text"]
        return await asyncio.to_thread(_status)


@function_tool
async def open_website(context: RunContext, url: str) -> str:
    """Open a website for the user. Opens on the device they are using (phone/laptop/PC).

    Args:
        url: Full URL starting with http:// or https://
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
        raise ToolError("That is not a valid web address.")
    if device_link.active(context):
        async with action(context, "system.open_url", f"Open {url} on this device", {"url": url}) as act:
            await device_link.call(context, "open_url", {"url": url}, event=act)
        return f"Opened {url}."
    if owner_is_remote():
        # Boss is on another device: open it THERE (the dashboard shows/opens the link), not on the PC.
        async with action(context, "system.open_url", f"Open {url} on Boss's device", {"url": url}):
            await publish_event({"type": "open_url", "url": url})
        return f"Sent {url} to Boss's device — it opens there (tap the link if the browser blocks it)."
    async with action(context, "system.open_url", f"Open {url}", {"url": url}):
        await asyncio.to_thread(webbrowser.open, url)
    return f"Opened {url}."


@function_tool
async def open_application(context: RunContext, app_name: str, confirmed: bool = False) -> str:
    """Launch an application on the user's device. Only apps on that device's allowed list can be opened.

    Args:
        app_name: Name of the app, e.g. "notepad", "chrome", "vscode".
        confirmed: Set true ONLY after the user verbally confirmed (when asked to).
    """
    key = app_name.strip().lower()
    if device_link.active(context):
        # The device's own device.yaml decides which apps exist and may be opened there.
        async with action(context, "system.open_app", f"Open application '{key}' on this device",
                          {"app": key}, confirmed) as act:
            await device_link.call(context, "open_app", {"app": key}, event=act)
        return f"Opened {key}."
    apps: dict[str, str] = get_policy().section("system").get("apps") or {}
    command = apps.get(key)
    if not command:
        raise ToolError(f"'{app_name}' is not in the allowed app list. Allowed: {', '.join(apps) or 'none'}.")
    async with action(context, "system.open_app", f"Open application '{key}'",
                      {"app": key, "command": command}, confirmed):
        if sys.platform == "win32":
            # `start` resolves App Paths, PATH and URI schemes (spotify:, ms-settings:)
            subprocess.Popen(["cmd", "/c", "start", "", command], creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            subprocess.Popen([command])
    return f"Opened {key}."


TOOLS = [
    ("system.info", system_status),
    ("system.open_url", open_website),
    ("system.open_app", open_application),
]
