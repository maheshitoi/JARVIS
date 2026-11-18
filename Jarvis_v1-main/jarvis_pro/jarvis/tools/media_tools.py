"""Media + browser tools on the PC running JARVIS: play music on YouTube or Spotify, pause/skip it,
and close browser tabs. Capabilities: media.play, media.control, media.close_tabs.

When Boss is on the web dashboard, the music plays *inside* it (YouTube's / Spotify's embedded
player) instead of a new browser tab. access.yaml media.play_in_dashboard: false turns that off."""

from __future__ import annotations

import asyncio
import os
import re
import time
import webbrowser
from urllib.parse import quote, quote_plus

import httpx
from livekit.agents import RunContext, ToolError, function_tool

from ..access import get_policy
from ..guard import action, owner_on_dashboard, publish_event

_VIDEO_ID = re.compile(r'"videoId":"([\w-]{11})"')
# Window-title endings of common browsers (Edge puts a zero-width space in its name).
_BROWSERS = ("google chrome", "microsoft edge", "mozilla firefox", "brave", "opera", "vivaldi")
_MEDIA_KEYS = {"pause": "playpause", "resume": "playpause", "play_pause": "playpause",
               "next": "nexttrack", "previous": "prevtrack", "stop": "stop",
               "volume_up": "volumeup", "volume_down": "volumedown", "mute": "volumemute"}


_player = {"open": False}  # is a player showing in the dashboard (so "stop" can close it)


def _in_dashboard() -> bool:
    return get_policy().section("media").get("play_in_dashboard", True) is not False and owner_on_dashboard()


async def _show_player(provider: str, title: str, url: str, link: str, act: dict, tall: bool = False) -> None:
    await publish_event({"type": "player", "state": "on", "provider": provider, "title": title,
                         "url": url, "link": link, "tall": tall, "action_id": act["id"],
                         "call_id": act.get("call_id")})
    _player["open"] = True


async def _videos(query: str, limit: int = 6) -> list[str]:
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True,
                                    headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"}) as c:
            ids = _VIDEO_ID.findall((await c.get(url)).text)
        return list(dict.fromkeys(ids))[:limit]
    except httpx.HTTPError:
        return []


async def _embeddable(video: str) -> bool:
    """YouTube's oEmbed answers 401/403 for videos whose owner blocked embedding."""
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get("https://www.youtube.com/oembed",
                            params={"url": f"https://www.youtube.com/watch?v={video}", "format": "json"})
        return r.status_code == 200
    except httpx.HTTPError:
        return False


async def _first_embeddable(ids: list[str]) -> str | None:
    ok = await asyncio.gather(*(_embeddable(v) for v in ids))
    return next((v for v, good in zip(ids, ok) if good), None)


@function_tool
async def play_youtube_music(context: RunContext, query: str) -> str:
    """Play a song, artist, album or playlist on YouTube (inside the dashboard when Boss is on it,
    otherwise in the PC's browser).

    Args:
        query: What to play, e.g. "Believer Imagine Dragons" or "lofi hip hop".
    """
    query = query.strip()
    if not query:
        raise ToolError("Tell me what to play.")
    async with action(context, "media.play", f"Play '{query}' on YouTube", {"query": query}) as act:
        ids = await _videos(query)
        if ids and _in_dashboard():
            video = await _first_embeddable(ids)
            if video:
                link = f"https://www.youtube.com/watch?v={video}"
                await _show_player("youtube", query,
                                   f"https://www.youtube-nocookie.com/embed/{video}?autoplay=1&rel=0&playsinline=1",
                                   link, act)
                act["result"] = f"{link} (in dashboard)"
                return f"Playing {query} inside the dashboard. Boss can close the player or say stop."
        video = ids[0] if ids else None
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
    if key == "stop" and _player["open"]:  # music is in the dashboard player: just close it
        async with action(context, "media.control", "Close the dashboard player"):
            await publish_event({"type": "player", "state": "off"})
            _player["open"] = False
        return "Stopped and closed the player."
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


# ---------------------------------------------------------------- Spotify ----
_SPOTIFY_TOKEN: dict = {"value": "", "exp": 0.0}
_KINDS = ("track", "album", "playlist", "artist")


async def _spotify_token() -> str | None:
    cid, secret = os.getenv("SPOTIFY_CLIENT_ID"), os.getenv("SPOTIFY_CLIENT_SECRET")
    if not cid or not secret:
        return None
    if _SPOTIFY_TOKEN["value"] and _SPOTIFY_TOKEN["exp"] > time.time() + 60:
        return _SPOTIFY_TOKEN["value"]
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post("https://accounts.spotify.com/api/token",
                             data={"grant_type": "client_credentials"}, auth=(cid, secret))
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        raise ToolError("Spotify login failed: check SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in .env.")
    data = r.json()
    _SPOTIFY_TOKEN.update(value=data["access_token"], exp=time.time() + int(data.get("expires_in", 3600)))
    return _SPOTIFY_TOKEN["value"]


async def _spotify_search(query: str, kind: str, token: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.spotify.com/v1/search", params={"q": query, "type": kind, "limit": 1},
                            headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    items = (r.json().get(f"{kind}s") or {}).get("items") or []
    return next((i for i in items if i), None)


@function_tool
async def play_spotify(context: RunContext, query: str, kind: str = "track") -> str:
    """Play a song, album, playlist or artist on Spotify. Use this when Boss says Spotify.

    Args:
        query: What to play, e.g. "Blinding Lights" or "Arijit Singh".
        kind: One of track, album, playlist, artist.
    """
    query, kind = query.strip(), kind.strip().lower()
    if not query:
        raise ToolError("Tell me what to play.")
    if kind not in _KINDS:
        kind = "track"
    async with action(context, "media.play", f"Play '{query}' on Spotify", {"query": query, "kind": kind}) as act:
        token = await _spotify_token()
        item = await _spotify_search(query, kind, token) if token else None
        if item and _in_dashboard():
            artists = ", ".join(a["name"] for a in item.get("artists") or [])
            title = item["name"] + (f" · {artists}" if artists else "")
            await _show_player("spotify", title, f"https://open.spotify.com/embed/{kind}/{item['id']}?theme=0",
                               item.get("external_urls", {}).get("spotify", ""), act, tall=kind != "track")
            act["result"] = f"{title} (in dashboard)"
            return (f"{title} is in the Spotify player inside the dashboard; Boss presses play there. "
                    "Full songs need Boss logged in to Spotify in that browser, otherwise 30-second previews.")
        # No API keys: hand it to the Spotify app / web player outside the dashboard.
        url = (item.get("external_urls", {}).get("spotify") if item else None) \
            or f"https://open.spotify.com/search/{quote(query)}"
        await asyncio.to_thread(webbrowser.open, url)
        act["result"] = url
    if item:
        return f"Opened {item['name']} on Spotify."
    return (f"Opened a Spotify search for {query}; Boss picks the song there. To play Spotify inside the "
            "dashboard, add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to .env.")


TOOLS = [
    ("media.play", play_youtube_music),
    ("media.play", play_spotify),
    ("media.control", media_control),
    ("media.close_tabs", close_browser_tabs),
]
