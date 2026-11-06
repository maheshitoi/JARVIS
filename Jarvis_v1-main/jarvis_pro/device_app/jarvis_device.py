"""JARVIS Device app — talk to JARVIS from any Windows PC; it acts on THIS PC only.

    python jarvis_device.py            (or the built JarvisDevice.exe)

How it works
  1. Gets a LiveKit token from your JARVIS server (server.py) with this device's key.
     The server puts every connection in its own private room.
  2. Joins that room: your mic goes to JARVIS, JARVIS's voice plays on this PC's speakers.
  3. When JARVIS needs the screen, windows, keyboard, an app, a website or PC status, the agent
     calls this app over RPC, and the action runs HERE, on this PC, and nowhere else.
  4. device.yaml on this PC decides what JARVIS may do here (allow / confirm / deny),
     independent of the server. "confirm" shows an Approve/Deny popup on this PC.

Only devices registered with `python manage_devices.py add ...` on the server can connect.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import io
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import scrolledtext
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
import yaml
from livekit import rtc

APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "device.yaml"

DEVICE_RPC_PREFIX = "jarvis.device."
APPROVE_RPC = "jarvis.approve"
EVENTS_TOPIC = "jarvis.events"
SCREENSHOT_TOPIC = "jarvis.screenshot"
ALLOW, CONFIRM, DENY = "allow", "confirm", "deny"

# What this device allows when device.yaml doesn't say. Keyboard control and apps ask first.
DEFAULT_PERMISSIONS = {
    "info": ALLOW, "open_url": ALLOW, "open_app": CONFIRM,
    "view": ALLOW, "windows": ALLOW, "control": CONFIRM,
}
DEFAULT_APPS = {
    "notepad": "notepad.exe", "calculator": "calc.exe", "explorer": "explorer.exe",
    "chrome": "chrome", "edge": "msedge", "settings": "ms-settings:",
}
# RPC method -> permission in device.yaml
METHOD_PERMISSION = {
    "info": "info", "open_url": "open_url", "open_app": "open_app",
    "screenshot": "view", "windows": "windows",
    "switch_window": "control", "type_text": "control", "hotkey": "control",
}
APPROVAL_TIMEOUT_S = 60


# =====================================================================
#  Config
# =====================================================================
class DeviceConfig:
    def __init__(self, data: dict[str, Any]):
        self.server_url = str(data.get("server_url") or "").rstrip("/")
        self.device_key = str(data.get("device_key") or "").strip()
        perms = data.get("permissions") or {}
        self.permissions = {k: str(perms.get(k, v)).strip().lower() for k, v in DEFAULT_PERMISSIONS.items()}
        apps = data.get("apps")
        self.apps = {str(k).lower(): str(v) for k, v in (apps if isinstance(apps, dict) else DEFAULT_APPS).items()}
        self.input_device = data.get("input_device")
        self.output_device = data.get("output_device")

    def level(self, permission: str) -> str:
        level = self.permissions.get(permission, DENY)
        return level if level in (ALLOW, CONFIRM, DENY) else DENY

    @classmethod
    def load(cls) -> "DeviceConfig":
        if not CONFIG_FILE.exists():
            raise FileNotFoundError(
                f"{CONFIG_FILE} not found. Ask the JARVIS owner to run "
                "'python manage_devices.py add \"<this device>\"' and put the result in device.yaml."
            )
        cfg = cls(yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {})
        if not cfg.server_url or not cfg.device_key:
            raise ValueError("device.yaml needs server_url and device_key.")
        return cfg


# =====================================================================
#  Local actions (all run on THIS PC)
# =====================================================================
class ActionError(Exception):
    """A refusal or failure to report back to JARVIS."""


def _status_text() -> str:
    import psutil

    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(os.path.abspath(os.sep))
    parts = [
        f"Time: {datetime.now().strftime('%A %d %B %Y, %I:%M %p')}",
        f"CPU: {psutil.cpu_percent(interval=0.5):.0f}%",
        f"RAM: {mem.percent:.0f}% used ({mem.used / 2**30:.1f} of {mem.total / 2**30:.1f} GB)",
        f"Disk: {disk.percent:.0f}% used, {disk.free / 2**30:.0f} GB free",
    ]
    battery = psutil.sensors_battery()
    if battery:
        parts.append(f"Battery: {battery.percent:.0f}% ({'charging' if battery.power_plugged else 'on battery'})")
    return "\n".join(parts)


def _screenshot_jpeg(max_width: int = 1280) -> bytes:
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


def _windows_text() -> str:
    import pygetwindow as gw

    active = gw.getActiveWindow()
    titles = sorted({t.strip() for t in gw.getAllTitles() if t.strip()})
    return (f"Active window: {active.title if active else 'unknown'}\nOpen windows:\n"
            + "\n".join(f"- {t}" for t in titles[:40]))


def _switch_window(title_contains: str) -> str:
    import pygetwindow as gw

    wins = [w for w in gw.getAllWindows() if title_contains.lower() in w.title.lower()]
    if not wins:
        raise ActionError(f"No window with '{title_contains}' in its title.")
    w = wins[0]
    if w.isMinimized:
        w.restore()
    w.activate()
    return w.title


def _open_app(cfg: DeviceConfig, app: str) -> None:
    command = cfg.apps.get(app.strip().lower())
    if not command:
        raise ActionError(f"'{app}' is not allowed on this device. Allowed: {', '.join(cfg.apps) or 'none'}.")
    if sys.platform == "win32":
        subprocess.Popen(["cmd", "/c", "start", "", command], creationflags=subprocess.CREATE_NO_WINDOW)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-a", command])
    else:
        subprocess.Popen([command])


def _open_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ActionError("Only http(s) web addresses can be opened.")
    webbrowser.open(url)


def _hotkey(keys: list[str]) -> None:
    import pyautogui

    parts = [str(k).strip().lower() for k in keys if str(k).strip()]
    bad = [k for k in parts if k not in pyautogui.KEYBOARD_KEYS]
    if not parts or bad:
        raise ActionError(f"Unknown key(s): {', '.join(bad) or '(none)'}")
    pyautogui.hotkey(*parts)


def _type_text(text: str) -> None:
    import pyautogui

    pyautogui.write(str(text), interval=0.01)


def _describe(method: str, args: dict[str, Any]) -> str:
    return {
        "info": "Read this PC's status (CPU, RAM, disk, battery)",
        "open_url": f"Open website {args.get('url', '')}",
        "open_app": f"Open app '{args.get('app', '')}'",
        "screenshot": "Take a screenshot of this screen",
        "windows": "List open windows",
        "switch_window": f"Switch to window '{args.get('title_contains', '')}'",
        "type_text": f"Type: {str(args.get('text', ''))[:120]}",
        "hotkey": f"Press {'+'.join(args.get('keys') or [])}",
    }.get(method, method)


# =====================================================================
#  LiveKit session (runs in a background asyncio thread)
# =====================================================================
class DeviceClient:
    def __init__(self, ui: "App"):
        self.ui = ui
        self.cfg: DeviceConfig | None = None
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="livekit").start()
        self.room: rtc.Room | None = None
        self.mic_track: rtc.LocalAudioTrack | None = None
        self._mic = None
        self._player = None
        self._approved: set[str] = set()  # event ids approved on THIS device via the popup

    # ---- thread-safe entry points for the UI ----
    def connect(self) -> None:
        asyncio.run_coroutine_threadsafe(self._connect(), self.loop)

    def disconnect(self) -> None:
        asyncio.run_coroutine_threadsafe(self._disconnect(), self.loop)

    def set_muted(self, muted: bool) -> None:
        def _do() -> None:
            if self.mic_track:
                self.mic_track.mute() if muted else self.mic_track.unmute()
        self.loop.call_soon_threadsafe(_do)

    # ---- connection ----
    async def _connect(self) -> None:
        if self.room is not None:
            return
        try:
            self.cfg = DeviceConfig.load()
            self.ui.status("Connecting...")
            async with httpx.AsyncClient(timeout=20) as http:
                r = await http.post(f"{self.cfg.server_url}/api/device/token",
                                    headers={"X-Device-Key": self.cfg.device_key,
                                             "ngrok-skip-browser-warning": "1"})  # free ngrok interstitial
            if r.status_code != 200:
                detail = r.json().get("detail") if "json" in r.headers.get("content-type", "") else r.text
                raise RuntimeError(f"Server refused this device ({r.status_code}): {detail}")
            info = r.json()
            self.ui.set_title(info["device"]["name"], info["device"]["role"])

            room = rtc.Room(loop=self.loop)
            self.room = room
            self._wire_room(room)
            await room.connect(info["url"], info["token"])
            self._register_rpc(room)

            devices = rtc.MediaDevices(loop=self.loop)
            self._mic = devices.open_input(enable_aec=True, input_device=self.cfg.input_device)
            self.mic_track = rtc.LocalAudioTrack.create_audio_track("microphone", self._mic.source)
            await room.local_participant.publish_track(
                self.mic_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
            self._player = devices.open_output(output_device=self.cfg.output_device)
            await self._player.start()
            for p in room.remote_participants.values():  # agent may already be there
                for pub in p.track_publications.values():
                    if pub.track and pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                        await self._player.add_track(pub.track)
            self.ui.connected(True)
            self.ui.status("Connected - waiting for JARVIS...")
            self.ui.log("system", f"Connected to private room {info['room']}.")
        except Exception as e:  # noqa: BLE001
            self.ui.log("error", str(e))
            self.ui.status("Not connected")
            await self._disconnect()

    async def _disconnect(self) -> None:
        room, self.room = self.room, None
        for closer in (self._mic, self._player):
            try:
                if closer:
                    await closer.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._mic = self._player = self.mic_track = None
        if room is not None:
            try:
                await room.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.ui.connected(False)
        self.ui.status("Not connected")

    def _wire_room(self, room: rtc.Room) -> None:
        @room.on("track_subscribed")
        def _on_track(track: rtc.Track, _pub, _participant) -> None:
            if track.kind == rtc.TrackKind.KIND_AUDIO and self._player:
                asyncio.ensure_future(self._player.add_track(track))

        @room.on("participant_connected")
        def _on_join(p: rtc.RemoteParticipant) -> None:
            if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                self.ui.status("JARVIS is here - just talk")

        @room.on("participant_attributes_changed")
        def _on_attrs(changed: dict[str, str], p: rtc.Participant) -> None:
            state = changed.get("lk.agent.state")
            if state:
                self.ui.status({"listening": "Listening", "thinking": "Thinking...",
                                "speaking": "Speaking", "initializing": "Starting..."}.get(state, state))

        @room.on("participant_disconnected")
        def _on_leave(p: rtc.RemoteParticipant) -> None:
            if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                self.ui.log("system", "JARVIS left the session.")
                asyncio.ensure_future(self._disconnect())

        @room.on("disconnected")
        def _on_disconnected(*_args) -> None:
            asyncio.ensure_future(self._disconnect())

        @room.on("data_received")
        def _on_data(packet: rtc.DataPacket) -> None:
            if packet.topic != EVENTS_TOPIC:
                return
            try:
                ev = json.loads(packet.data.decode())
            except (ValueError, UnicodeDecodeError):
                return
            if ev.get("type") == "action" and ev.get("status") in ("done", "blocked", "denied", "failed"):
                self.ui.log("action", f"[{ev['status']}] {ev.get('summary', '')}")

        def _on_transcript(reader: rtc.TextStreamReader, sender: str) -> None:
            async def _read() -> None:
                text = (await reader.read_all()).strip()
                attrs = reader.info.attributes
                if not text or attrs.get("lk.transcription_final") == "false":
                    return
                mine = self.mic_track is not None and attrs.get("lk.transcribed_track_id") == self.mic_track.sid
                self.ui.log("you" if mine else "jarvis", text)
            asyncio.ensure_future(_read())

        room.register_text_stream_handler("lk.transcription", _on_transcript)

    def _register_rpc(self, room: rtc.Room) -> None:
        """Needs a connected room. The agent only joins after us, so nothing is missed."""
        room.local_participant.register_rpc_method(APPROVE_RPC, self._rpc_approve)
        for method in METHOD_PERMISSION:
            room.local_participant.register_rpc_method(DEVICE_RPC_PREFIX + method, self._make_handler(method))

    # ---- RPC from the agent ----
    def _from_agent(self, identity: str) -> bool:
        p = self.room.remote_participants.get(identity) if self.room else None
        return bool(p and p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT)

    async def _rpc_approve(self, data: rtc.RpcInvocationData) -> str:
        if not self._from_agent(data.caller_identity):
            raise rtc.RpcError(1500, "Not allowed")
        event = json.loads(data.payload or "{}")
        ok = await self._ask(f"JARVIS wants to: {event.get('summary', '?')}",
                             min(data.response_timeout, APPROVAL_TIMEOUT_S) - 1)
        if ok and event.get("id"):
            self._approved.add(event["id"])
        self.ui.log("action", f"You {'approved' if ok else 'denied'}: {event.get('summary', '')}")
        return json.dumps({"approved": ok})

    def _make_handler(self, method: str) -> Callable:
        async def handler(data: rtc.RpcInvocationData) -> str:
            if not self._from_agent(data.caller_identity) or self.cfg is None:
                raise rtc.RpcError(1500, "Not allowed")
            payload = json.loads(data.payload or "{}")
            args = payload.get("args") or {}
            what = _describe(method, args)
            level = self.cfg.level(METHOD_PERMISSION[method])
            if level == DENY:
                self.ui.log("action", f"[blocked by device.yaml] {what}")
                raise rtc.RpcError(1500, f"'{METHOD_PERMISSION[method]}' is turned off on this device (device.yaml).")
            if level == CONFIRM and payload.get("approval_id") not in self._approved:
                if not await self._ask(f"JARVIS wants to: {what}", min(data.response_timeout, APPROVAL_TIMEOUT_S) - 1):
                    self.ui.log("action", f"[denied] {what}")
                    raise rtc.RpcError(1500, "The user denied this on the device.")
            self._approved.discard(payload.get("approval_id"))
            try:
                result = await self._run(method, args, data.caller_identity)
            except ActionError as e:
                raise rtc.RpcError(1500, str(e)) from e
            except Exception as e:  # noqa: BLE001
                raise rtc.RpcError(1500, f"{type(e).__name__}: {e}") from e
            self.ui.log("action", f"[done] {what}")
            return json.dumps(result)

        return handler

    async def _run(self, method: str, args: dict[str, Any], caller: str) -> dict[str, Any]:
        cfg = self.cfg
        assert cfg is not None and self.room is not None
        if method == "info":
            return {"text": await asyncio.to_thread(_status_text)}
        if method == "open_url":
            await asyncio.to_thread(_open_url, str(args.get("url", "")))
            return {"ok": True}
        if method == "open_app":
            await asyncio.to_thread(_open_app, cfg, str(args.get("app", "")))
            return {"ok": True}
        if method == "windows":
            return {"text": await asyncio.to_thread(_windows_text)}
        if method == "switch_window":
            return {"title": await asyncio.to_thread(_switch_window, str(args.get("title_contains", "")))}
        if method == "type_text":
            await asyncio.to_thread(_type_text, str(args.get("text", "")))
            return {"ok": True}
        if method == "hotkey":
            await asyncio.to_thread(_hotkey, list(args.get("keys") or []))
            return {"ok": True}
        if method == "screenshot":
            image = await asyncio.to_thread(_screenshot_jpeg)
            await self.room.local_participant.send_bytes(
                image, topic=SCREENSHOT_TOPIC, name="screen.jpg", mime_type="image/jpeg",
                destination_identities=[caller], attributes={"request_id": str(args.get("request_id", ""))},
            )
            return {"ok": True, "bytes": len(image)}
        raise ActionError(f"Unknown action {method}")

    async def _ask(self, message: str, timeout_s: float) -> bool:
        fut: concurrent.futures.Future[bool] = concurrent.futures.Future()
        self.ui.ask(message, max(5, int(timeout_s)), fut)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout_s + 2)
        except asyncio.TimeoutError:
            return False


# =====================================================================
#  Window (Tk main thread)
# =====================================================================
COLORS = {"bg": "#0f1720", "panel": "#16212c", "text": "#e6edf3", "muted": "#8b9bab",
          "accent": "#38bdf8", "you": "#a5d6ff", "jarvis": "#7ee787", "action": "#e3b341", "error": "#ff7b72"}


class App:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("JARVIS Device")
        self.root.geometry("520x620")
        self.root.minsize(380, 420)
        self.root.configure(bg=COLORS["bg"])
        self._q: queue.Queue[tuple] = queue.Queue()
        self._connected = False
        self._muted = False

        self.title_var = tk.StringVar(value="JARVIS Device")
        self.status_var = tk.StringVar(value="Not connected")
        tk.Label(self.root, textvariable=self.title_var, bg=COLORS["bg"], fg=COLORS["text"],
                 font=("Segoe UI", 15, "bold")).pack(anchor="w", padx=16, pady=(14, 0))
        tk.Label(self.root, textvariable=self.status_var, bg=COLORS["bg"], fg=COLORS["accent"],
                 font=("Segoe UI", 11)).pack(anchor="w", padx=16, pady=(2, 10))

        bar = tk.Frame(self.root, bg=COLORS["bg"])
        bar.pack(fill="x", padx=16)
        self.connect_btn = self._button(bar, "Connect", self._toggle_connect)
        self.mute_btn = self._button(bar, "Mute", self._toggle_mute)
        self.mute_btn.configure(state="disabled")

        self.logbox = scrolledtext.ScrolledText(
            self.root, wrap="word", bg=COLORS["panel"], fg=COLORS["text"], insertbackground=COLORS["text"],
            relief="flat", font=("Segoe UI", 10), padx=10, pady=8, state="disabled")
        self.logbox.pack(fill="both", expand=True, padx=16, pady=14)
        for tag in ("you", "jarvis", "action", "error"):
            self.logbox.tag_configure(tag, foreground=COLORS[tag])
        self.logbox.tag_configure("system", foreground=COLORS["muted"])

        self.client = DeviceClient(self)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self.root.after(100, self._pump)
        self.log("system", "Everything JARVIS does from this window happens on THIS PC only. "
                 f"Permissions: {CONFIG_FILE}")

    def _button(self, parent: tk.Widget, text: str, cmd: Callable) -> tk.Button:
        b = tk.Button(parent, text=text, command=cmd, bg=COLORS["panel"], fg=COLORS["text"],
                      activebackground=COLORS["accent"], relief="flat", padx=14, pady=6, font=("Segoe UI", 10))
        b.pack(side="left", padx=(0, 8))
        return b

    # ---- thread-safe API used by DeviceClient ----
    def status(self, text: str) -> None:
        self._q.put(("status", text))

    def log(self, kind: str, text: str) -> None:
        self._q.put(("log", kind, text))

    def set_title(self, name: str, role: str) -> None:
        self._q.put(("title", f"JARVIS - {name}" + ("" if role == "owner" else " (guest)")))

    def connected(self, value: bool) -> None:
        self._q.put(("connected", value))

    def ask(self, message: str, timeout_s: int, fut: concurrent.futures.Future) -> None:
        self._q.put(("ask", message, timeout_s, fut))

    # ---- main-thread handling ----
    def _pump(self) -> None:
        try:
            while True:
                item = self._q.get_nowait()
                kind = item[0]
                if kind == "status":
                    self.status_var.set(item[1])
                elif kind == "title":
                    self.title_var.set(item[1])
                    self.root.title(item[1])
                elif kind == "log":
                    self._append(item[1], item[2])
                elif kind == "connected":
                    self._set_connected(item[1])
                elif kind == "ask":
                    self._approval_dialog(*item[1:])
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def _append(self, kind: str, text: str) -> None:
        label = {"you": "You", "jarvis": "JARVIS", "action": "Action", "error": "Error"}.get(kind, "")
        stamp = datetime.now().strftime("%H:%M")
        self.logbox.configure(state="normal")
        self.logbox.insert("end", f"{stamp}  {label + ': ' if label else ''}{text}\n", kind)
        self.logbox.configure(state="disabled")
        self.logbox.see("end")

    def _set_connected(self, value: bool) -> None:
        self._connected = value
        self.connect_btn.configure(text="Disconnect" if value else "Connect", state="normal")
        self.mute_btn.configure(state="normal" if value else "disabled")

    def _toggle_connect(self) -> None:
        if self._connected:
            self.client.disconnect()
        else:
            self.connect_btn.configure(state="disabled")  # re-enabled by connected(True/False)
            self.client.connect()

    def _toggle_mute(self) -> None:
        self._muted = not self._muted
        self.client.set_muted(self._muted)
        self.mute_btn.configure(text="Unmute" if self._muted else "Mute")
        self.status_var.set("Muted" if self._muted else "Listening")

    def _approval_dialog(self, message: str, timeout_s: int, fut: concurrent.futures.Future) -> None:
        win = tk.Toplevel(self.root)
        win.title("JARVIS needs your approval")
        win.configure(bg=COLORS["bg"])
        win.attributes("-topmost", True)
        win.resizable(False, False)
        remaining = tk.IntVar(value=timeout_s)

        def finish(ok: bool) -> None:
            if not fut.done():
                fut.set_result(ok)
            if win.winfo_exists():
                win.destroy()

        def tick() -> None:
            if not win.winfo_exists():
                return
            remaining.set(remaining.get() - 1)
            if remaining.get() <= 0:
                finish(False)
            else:
                win.after(1000, tick)

        tk.Label(win, text=message, wraplength=380, justify="left", bg=COLORS["bg"], fg=COLORS["text"],
                 font=("Segoe UI", 11)).pack(padx=18, pady=(16, 6), anchor="w")
        tk.Label(win, text="On this PC. Auto-deny in:", bg=COLORS["bg"], fg=COLORS["muted"]).pack(padx=18, anchor="w")
        tk.Label(win, textvariable=remaining, bg=COLORS["bg"], fg=COLORS["action"]).pack(padx=18, anchor="w")
        row = tk.Frame(win, bg=COLORS["bg"])
        row.pack(padx=18, pady=14, anchor="e")
        tk.Button(row, text="Deny", command=lambda: finish(False), padx=14).pack(side="left", padx=6)
        tk.Button(row, text="Approve", command=lambda: finish(True), padx=14,
                  bg=COLORS["accent"]).pack(side="left")
        win.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        win.after(1000, tick)
        win.lift()
        win.focus_force()

    def _quit(self) -> None:
        fut = asyncio.run_coroutine_threadsafe(self.client._disconnect(), self.client.loop)
        try:
            fut.result(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
