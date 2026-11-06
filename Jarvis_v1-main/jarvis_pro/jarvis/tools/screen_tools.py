"""Windows screen tools: look at the screen, list windows, optional keyboard control.

Live screen viewing (watching a screen share from the dashboard) is handled by the
agent session's video input, not by a tool — see `screen.live_share` in access.yaml.

When the command comes from the JARVIS Device app, every tool here runs on THAT device
(via device_link), not on the PC running JARVIS.
"""

from __future__ import annotations

import asyncio
import io

from google.genai import types
from livekit.agents import RunContext, ToolError, function_tool

from .. import device_link
from ..genai_client import agenerate
from ..guard import action


def _screenshot_jpeg(max_width: int = 1280) -> bytes:  # smaller = faster upload, still readable
    import mss
    from PIL import Image

    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])  # primary monitor
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if img.width > max_width:
        img = img.resize((max_width, int(img.height * max_width / img.width)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


@function_tool
async def look_at_screen(context: RunContext, question: str = "Describe what is on the screen.") -> str:
    """Take a screenshot of the user's main monitor (on the device they are using) and analyse it.
    Use when the user asks what's on the screen, to read an error, or for help with what they're looking at.

    Args:
        question: What to find out from the screenshot.
    """
    async with action(context, "screen.view", f"Screenshot: {question}") as act:
        if device_link.active(context):
            image = await device_link.screenshot(context, event=act)
        else:
            image = await asyncio.to_thread(_screenshot_jpeg)
        resp = await agenerate(
            [
                types.Part.from_bytes(data=image, mime_type="image/jpeg"),
                f"You are looking at a screenshot of the user's Windows desktop. {question} "
                "Your answer will be spoken aloud: plain text, no markdown, at most 3 short sentences. "
                "Quote exact error text if there is any.",
            ],
            fast=True,
        )
        act["result"] = "analysed"
        return resp.text or "I couldn't read the screen."


def _windows() -> str:
    import pygetwindow as gw

    active = gw.getActiveWindow()
    titles = sorted({t.strip() for t in gw.getAllTitles() if t.strip()})
    head = f"Active window: {active.title if active else 'unknown'}"
    return head + "\nOpen windows:\n" + "\n".join(f"- {t}" for t in titles[:40])


@function_tool
async def list_open_windows(context: RunContext) -> str:
    """List the titles of open windows on the user's device and which one is active."""
    async with action(context, "screen.windows", "List open windows") as act:
        if device_link.active(context):
            return (await device_link.call(context, "windows", event=act))["text"]
        return await asyncio.to_thread(_windows)


@function_tool
async def switch_to_window(context: RunContext, title_contains: str, confirmed: bool = False) -> str:
    """Bring a window to the front.

    Args:
        title_contains: Part of the window title, e.g. "Chrome" or "Visual Studio Code".
        confirmed: Set true ONLY after the user verbally confirmed (when asked to).
    """
    async with action(context, "screen.control", f"Switch to window '{title_contains}'", {}, confirmed) as act:
        if device_link.active(context):
            res = await device_link.call(context, "switch_window", {"title_contains": title_contains}, event=act)
            return f"Switched to {res['title']}."

        def _do() -> str:
            import pygetwindow as gw

            wins = [w for w in gw.getAllWindows() if title_contains.lower() in w.title.lower()]
            if not wins:
                raise ToolError(f"No window with '{title_contains}' in its title.")
            w = wins[0]
            if w.isMinimized:
                w.restore()
            w.activate()
            return w.title

        title = await asyncio.to_thread(_do)
    return f"Switched to {title}."


@function_tool
async def type_text(context: RunContext, text: str, confirmed: bool = False) -> str:
    """Type text into the currently focused window (like the user typing on the keyboard).

    Args:
        text: The text to type. English/ASCII characters only.
        confirmed: Set true ONLY after the user verbally confirmed (when asked to).
    """
    async with action(context, "screen.control", f"Type text into active window: {text[:80]}",
                      {"length": len(text)}, confirmed) as act:
        if device_link.active(context):
            await device_link.call(context, "type_text", {"text": text}, event=act, timeout=60)
            return "Typed."
        import pyautogui

        await asyncio.to_thread(pyautogui.write, text, 0.01)
    return "Typed."


@function_tool
async def press_hotkey(context: RunContext, keys: str, confirmed: bool = False) -> str:
    """Press a keyboard shortcut in the active window.

    Args:
        keys: Keys joined with '+', e.g. "ctrl+s", "alt+tab", "win+d", "enter".
        confirmed: Set true ONLY after the user verbally confirmed (when asked to).
    """
    import pyautogui

    parts = [k.strip().lower() for k in keys.split("+") if k.strip()]
    bad = [k for k in parts if k not in pyautogui.KEYBOARD_KEYS]
    if not parts or bad:
        raise ToolError(f"Unknown key(s): {', '.join(bad) or keys}")
    async with action(context, "screen.control", f"Press {'+'.join(parts)}", {}, confirmed) as act:
        if device_link.active(context):
            await device_link.call(context, "hotkey", {"keys": parts}, event=act)
        else:
            await asyncio.to_thread(pyautogui.hotkey, *parts)
    return f"Pressed {'+'.join(parts)}."


TOOLS = [
    ("screen.view", look_at_screen),
    ("screen.windows", list_open_windows),
    ("screen.control", switch_to_window),
    ("screen.control", type_text),
    ("screen.control", press_hotkey),
]
