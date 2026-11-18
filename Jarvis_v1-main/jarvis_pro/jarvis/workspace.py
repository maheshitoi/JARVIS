"""Live workspace: every tool JARVIS runs (its own tools and MCP tools alike) shows up on the owner's
dashboard as a mini screen: "working" while it runs, then its result (tables, lists, text, players).

Events on the dashboard data topic (guard.EVENTS_TOPIC):
    {"type": "tool", "state": "start", "call_id", "name", "args"}
Results can be large (a database table), so they go as a LiveKit text stream on TOOL_TOPIC:
    {"type": "tool", "state": "result", "call_id", "name", "output", "error"}
guard.action() adds the same call_id to its "action" events, so the dashboard can show the
action's summary and approval state on the same mini screen.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from livekit import rtc
from livekit.agents import AgentSession

from .guard import EVENTS_TOPIC

logger = logging.getLogger("jarvis.workspace")

TOOL_TOPIC = "jarvis.tool"
MAX_OUTPUT = 200_000  # characters sent to the dashboard per result
MAX_ARG = 400


def _args(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {"input": (raw or "")[:MAX_ARG]}
    if not isinstance(data, dict):
        return {"input": data}
    return {k: (v[:MAX_ARG] + "…" if isinstance(v, str) and len(v) > MAX_ARG else v) for k, v in data.items()}


async def show(room: rtc.Room | None, call_id: str, name: str, *, state: str, **fields: Any) -> None:
    """Drive a screen that isn't a normal tool call (e.g. a background task).

    state: "start" (args=...), "progress" (message=...), "result" (output=..., error=bool), "cancelled".
    """
    if room is None or not room.isconnected():
        return
    event = {"type": "tool", "state": state, "call_id": call_id, "name": name, **fields}
    try:
        if state == "result":
            out = str(event.get("output", ""))
            event["output"] = out if len(out) <= MAX_OUTPUT else out[:MAX_OUTPUT] + "\n… (cut: too long to show)"
            await room.local_participant.send_text(json.dumps(event, default=str), topic=TOOL_TOPIC)
        else:
            await room.local_participant.publish_data(json.dumps(event, default=str), topic=EVENTS_TOPIC,
                                                      reliable=True)
    except Exception:  # noqa: BLE001 — cosmetic
        logger.debug("could not update workspace screen", exc_info=True)


def attach(session: AgentSession, room: rtc.Room) -> None:
    """Mirror the session's tool calls to the dashboard in `room`."""
    tasks: set[asyncio.Task] = set()

    def send(coro) -> None:
        t = asyncio.ensure_future(coro)
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    async def publish(event: dict[str, Any]) -> None:
        if not room.isconnected():
            return
        try:
            await room.local_participant.publish_data(json.dumps(event, default=str), topic=EVENTS_TOPIC,
                                                      reliable=True)
        except Exception:  # noqa: BLE001 — the workspace is cosmetic; never break the session
            logger.debug("could not publish tool event", exc_info=True)

    async def stream(event: dict[str, Any]) -> None:
        if not room.isconnected():
            return
        try:
            await room.local_participant.send_text(json.dumps(event, default=str), topic=TOOL_TOPIC)
        except Exception:  # noqa: BLE001
            logger.debug("could not stream tool result", exc_info=True)

    @session.on("tool_execution_updated")
    def _updated(ev) -> None:
        u = ev.update
        if u.type == "tool_call_started":
            fc = u.function_call
            send(publish({"type": "tool", "state": "start", "call_id": fc.call_id, "name": fc.name,
                          "args": _args(fc.arguments)}))
        elif u.type == "tool_call_updated" and not u.silent:
            send(publish({"type": "tool", "state": "progress", "call_id": u.call_id, "message": u.message}))
        elif u.type == "tool_call_ended" and u.status == "cancelled":
            send(publish({"type": "tool", "state": "cancelled", "call_id": u.call_id}))

    @session.on("function_tools_executed")
    def _executed(ev) -> None:
        for fc, out in ev.zipped():
            text = out.output if isinstance(out.output, str) else json.dumps(out.output, default=str)
            if len(text) > MAX_OUTPUT:
                text = text[:MAX_OUTPUT] + "\n… (cut: too long to show)"
            send(stream({"type": "tool", "state": "result", "call_id": fc.call_id, "name": fc.name,
                         "args": _args(fc.arguments), "output": text, "error": bool(out.is_error)}))
