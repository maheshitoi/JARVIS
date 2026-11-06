"""Runs device actions on the device that gave the command.

When someone talks to JARVIS through the JARVIS Device app, their session lives in its own
LiveKit room. Tools that act on a computer (screenshot, windows, typing, apps, websites, PC info)
are sent over RPC to the device in THAT room and run there — never on the PC running JARVIS,
and never on any other device.

    if device_link.active(context):
        info = await device_link.call(context, "info")
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from livekit import rtc
from livekit.agents import RunContext, ToolError, get_job_context

DEVICE_RPC_PREFIX = "jarvis.device."
SCREENSHOT_TOPIC = "jarvis.screenshot"

# Capabilities the device app can perform on its own machine.
DEVICE_CAPS = frozenset({
    "system.info", "system.open_url", "system.open_app",
    "screen.view", "screen.windows", "screen.control",
})

_pending: dict[str, asyncio.Future[bytes]] = {}


def device_identity(context: RunContext | None) -> str | None:
    userdata = getattr(context, "userdata", None) if context else None
    return getattr(userdata, "device_identity", None)


def active(context: RunContext | None) -> bool:
    return device_identity(context) is not None


def install(room: rtc.Room) -> None:
    """Receive screenshots the device sends back. Call once per device session."""

    def _on_bytes(reader: rtc.ByteStreamReader, sender: str) -> None:
        async def _read() -> None:
            request_id = reader.info.attributes.get("request_id", "")
            chunks = [chunk async for chunk in reader]
            fut = _pending.get(request_id)
            if fut and not fut.done():
                fut.set_result(b"".join(chunks))

        asyncio.ensure_future(_read())

    room.register_byte_stream_handler(SCREENSHOT_TOPIC, _on_bytes)


async def call(context: RunContext | None, method: str, args: dict[str, Any] | None = None, *,
               event: dict[str, Any] | None = None, timeout: float = 30) -> dict[str, Any]:
    """Run `method` on the session's device and return its JSON reply.

    `event` is the guard's action event. When it was approved on the device (popup), its id is
    passed along so the device doesn't ask a second time."""
    identity = device_identity(context)
    if identity is None:
        raise ToolError("No device is connected to this session.")
    payload = {"args": args or {}}
    if event and event.get("approved_by") == "dashboard":
        payload["approval_id"] = event["id"]
    room = get_job_context().room
    try:
        raw = await room.local_participant.perform_rpc(
            destination_identity=identity,
            method=DEVICE_RPC_PREFIX + method,
            payload=json.dumps(payload),
            response_timeout=timeout,
        )
    except rtc.RpcError as e:
        # Device-side refusals arrive as application errors with a readable message.
        raise ToolError(f"The device said: {e.message}") from e
    return json.loads(raw or "{}")


async def screenshot(context: RunContext | None, *, event: dict[str, Any] | None = None,
                     timeout: float = 30) -> bytes:
    """Ask the device for a JPEG of its screen. Sent as a byte stream (RPC payloads are tiny)."""
    request_id = uuid.uuid4().hex
    fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
    _pending[request_id] = fut
    try:
        await call(context, "screenshot", {"request_id": request_id}, event=event, timeout=timeout)
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError as e:
        raise ToolError("The device didn't send its screenshot in time.") from e
    finally:
        _pending.pop(request_id, None)
