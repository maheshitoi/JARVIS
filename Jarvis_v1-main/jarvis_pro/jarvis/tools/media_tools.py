"""Media + browser tools on the PC running JARVIS: play music on YouTube, pause/skip it,
and close browser tabs. Capabilities: media.play, media.control, media.close_tabs."""

from __future__ import annotations

import asyncio
import re
import webbrowser
from urllib.parse import quote_plus

import httpx
from livekit.agents import RunContext, ToolError, function_tool

from ..guard import action

_VIDEO_ID = re.compile(r'"videoId":"([\w-]{11})"')
# Window-title endings of common browsers (Edge puts a zero-width space in its name).
_BROWSERS = ("google chrome", "microsoft edge", "mozilla firefox", "brave", "opera", "vivaldi")
_MEDIA_KEYS = {"pause": "playpause", "resume": "playpause", "play_pause": "playpause",
               "next": "nexttrack", "previous": "prevtrack", "stop": "stop",
               "volume_up": "volumeup", "volume_down": "volumedown", "mute": "volumemute"}


async def _first_video(query: str) -> str | None:
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True,
                                    headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"}) as c:
            m = _VIDEO_ID.search((await c.get(url)).text)
        return m.group(1) if m else None
    except httpx.HTTPError:
        return None


@function_tool
async def play_youtube_music(context: RunContext, query: str) -> str:
    """Play a song, artist, album or playlist on YouTube in the PC's browser.

    Args:
        query: What to play, e.g. "Believer Imagine Dragons" or "lofi hip hop".
    """
    query = query.strip()
    if not query:
        raise ToolError("Tell me what to play.")
    async with action(context, "media.play", f"Play '{query}' on YouTube", {"query": query}) as act:
        video = await _first_video(query)
        if video:
            url = f"https://www.youtube.com/watch?v={video}"
            reply = f"Playing {query} on YouTube."
        else:  # couldn't pick a video: show the search instead
            url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
            reply = f"I opened YouTube results for {query}; pick one to play."
        await asyncio.to_thread(webbrowser.open, url)
        act["result"] = url
    return reply


@function_tool
async def media_control(context: RunContext, command: str) -> str:
    """Control what's playing on the PC with the media keys.

    Args:
        command: One of pause, resume, next, previous, stop, volume_up, volume_down, mute.
    """
    key = _MEDIA_KEYS.get(command.strip().lower().replace(" ", "_"))
    if not key:
        raise ToolError(f"Unknown command. Use one of: {', '.join(_MEDIA_KEYS)}.")
    async with action(context, "media.control", f"Media key: {command}"):
        import pyautogui

        presses = 5 if key in ("volumeup", "volumedown") else 1
        await asyncio.to_thread(pyautogui.press, key, presses=presses)
    return f"Done: {command}."


def _browser_windows() -> list:
    import pygetwindow as gw

    out = []
    for w in gw.getAllWindows():
        title = w.title.replace("​", "").lower()
        if title and any(title.endswith(b) or f" - {b}" in title or f" — {b}" in title for b in _BROWSERS):
            out.append(w)
    return out


def _close_tabs(scope: str) -> str:
    windows = _browser_windows()
    if not windows:
        return "No browser windows are open."
    if scope == "current":
        import pyautogui

        w = windows[0]  # topmost browser window
        w.activate()
        pyautogui.sleep(0.3)
        pyautogui.hotkey("ctrl", "w")
        return "Closed the current browser tab."
    for w in windows:
        w.close()  # WM_CLOSE: the browser closes the window and all its tabs normally
    return f"Closed {len(windows)} browser window{'s' if len(windows) != 1 else ''} and all their tabs."


@function_tool
async def close_browser_tabs(context: RunContext, scope: str = "all") -> str:
    """Close browser tabs on the PC (Chrome, Edge, Firefox, Brave, Opera, Vivaldi).

    Args:
        scope: "all" closes every browser window with all its tabs; "current" closes only the
            active tab of the front browser window.
    """
    scope = scope.strip().lower()
    if scope not in ("all", "current"):
        raise ToolError('scope must be "all" or "current".')
    async with action(context, "media.close_tabs", f"Close {scope} browser tabs", {"scope": scope}) as act:
        result = await asyncio.to_thread(_close_tabs, scope)
        act["result"] = result
    return result


TOOLS = [
    ("media.play", play_youtube_music),
    ("media.control", media_control),
    ("media.close_tabs", close_browser_tabs),
]
