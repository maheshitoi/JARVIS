"""The permission gate every tool goes through.

    async with action(context, "email.send", "Send email to ravi@x.com", {...}, confirmed):
        ... do the real work ...

1. Looks up the level in access.yaml (allow / confirm / deny).
2. For `confirm`, asks for approval:
     - dashboard open  -> Approve/Deny popup over LiveKit RPC
     - no dashboard    -> spoken confirmation (tool is called again with confirmed=true)
     - phone caller    -> never; callers can't approve owner actions
3. Writes every step to the audit log and streams it to the dashboard.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from livekit import rtc
from livekit.agents import RunContext, ToolError, get_job_context

from . import audit
from .access import ALLOW, CONFIRM, DENY, AccessPolicy, get_policy
from .device_link import DEVICE_CAPS

logger = logging.getLogger("jarvis.guard")

EVENTS_TOPIC = "jarvis.events"
APPROVE_RPC = "jarvis.approve"
OWNER_ROLE_ATTR = "jarvis.role"  # "owner" | "guest"
DEVICE_ATTR = "jarvis.device"  # "host" (the PC running JARVIS), "remote" (browser elsewhere), "app" (Device app)
DEVICE_ID_ATTR = "jarvis.device_id"
DEVICE_NAME_ATTR = "jarvis.device_name"


@dataclass
class SessionData:
    """Per-session state, available to tools as context.userdata."""

    mode: str = "owner"  # "owner" | "guest" (someone else's device) | "caller"
    caller_number: str | None = None
    caller_identity: str | None = None
    purpose: str | None = None
    # Set when the session comes from the JARVIS Device app: device tools run on THIS participant.
    device_identity: str | None = None
    device_name: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _room() -> rtc.Room | None:
    try:
        ctx = get_job_context()
    except RuntimeError:
        return None
    return None if ctx.is_fake_job() else ctx.room


def find_owner(room: rtc.Room | None) -> rtc.RemoteParticipant | None:
    if room is None:
        return None
    for p in room.remote_participants.values():
        if p.attributes.get(OWNER_ROLE_ATTR) == "owner":
            return p
    return None


# Capabilities that physically act on the PC running JARVIS. They only run when the command
# comes from that PC itself — never on behalf of someone on another device.
HOST_ONLY_PREFIXES = ("system.open_app", "system.info", "screen.", "social.whatsapp.")


def is_host_only(capability: str) -> bool:
    return capability.startswith(HOST_ONLY_PREFIXES)


def owner_is_remote() -> bool:
    """True when Boss is talking from a device other than the PC running JARVIS."""
    owner = find_owner(_room())
    return bool(owner and owner.attributes.get(DEVICE_ATTR) == "remote")


async def publish_event(event: dict[str, Any]) -> None:
    """Best-effort push to the dashboard; never breaks a tool."""
    room = _room()
    if room is None or not room.isconnected():
        return
    try:
        await room.local_participant.publish_data(
            json.dumps(event, default=str), topic=EVENTS_TOPIC, reliable=True
        )
    except Exception:  # noqa: BLE001
        logger.debug("could not publish event", exc_info=True)


async def _log(event: dict[str, Any], status: str, **extra: Any) -> None:
    entry = audit.record({**event, "status": status, **extra})
    await publish_event({"type": "action", **entry})


async def _request_approval(
    policy: AccessPolicy, event: dict[str, Any], mode: str, confirmed: bool,
    device_identity: str | None = None,
) -> str:
    """Return how it was approved, or raise ToolError if it wasn't."""
    if mode == "caller":
        raise ToolError("This action is not available to phone callers.")

    # Device app session: the popup appears on the device that gave the command, nowhere else.
    approver = device_identity
    if approver is None:
        owner = find_owner(_room())
        approver = owner.identity if owner else None
    if approver is not None:
        await _log(event, "awaiting_approval")
        try:
            resp = await _room().local_participant.perform_rpc(  # type: ignore[union-attr]
                destination_identity=approver,
                method=APPROVE_RPC,
                payload=json.dumps(event, default=str),
                response_timeout=policy.approval_timeout,
            )
            if json.loads(resp or "{}").get("approved") is True:
                return "dashboard"
            raise ToolError(f"{_who(policy, mode)} declined this action. Do not retry.")
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001  (timeout / disconnected)
            raise ToolError(
                f"No approval received ({type(e).__name__}). Tell {_who(policy, mode)} it was not done."
            ) from e

    if policy.allow_voice_confirmation:
        if confirmed:
            return "voice"
        raise ToolError(
            "CONFIRMATION REQUIRED. Read these details back to "
            f"{policy.owner_name} and ask for a clear yes: {event['summary']}. "
            "Only if they say yes, call this same tool again with identical arguments "
            "and confirmed=true."
        )
    raise ToolError("This action needs dashboard approval. Ask Boss to open the dashboard.")


def _who(policy: AccessPolicy, mode: str) -> str:
    return "The user" if mode == "guest" else policy.owner_name


@asynccontextmanager
async def action(
    context: RunContext | None,
    capability: str,
    summary: str,
    details: dict[str, Any] | None = None,
    confirmed: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    policy = get_policy()
    level = policy.level(capability)
    userdata = getattr(context, "userdata", None) if context else None
    mode = getattr(userdata, "mode", "owner")
    device_identity = getattr(userdata, "device_identity", None)

    event: dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "capability": capability,
        "summary": summary,
        "details": details or {},
        "level": level,
        "mode": mode,
        **({"device": getattr(userdata, "device_name", None)} if device_identity else {}),
    }

    if mode == "guest" and capability not in DEVICE_CAPS:
        # A guest's session may only act on the guest's own device — never on Boss's accounts.
        await _log(event, "blocked", reason="guest device")
        raise ToolError("That isn't available on this device. You can only act on this device itself.")

    if level != DENY and device_identity and is_host_only(capability) and capability not in DEVICE_CAPS:
        # Device app session: host-only tools that can't run on the device (e.g. WhatsApp Desktop
        # of the home PC) are never run on the home PC on its behalf.
        await _log(event, "blocked", reason="command came from another device")
        raise ToolError("That only works on the home PC itself, not from this device. Say so briefly.")

    if level != DENY and is_host_only(capability) and owner_is_remote():
        # Defence in depth: the tool shouldn't even be offered to a remote device.
        await _log(event, "blocked", reason="command came from another device")
        raise ToolError(
            "That would run on the home PC, but this command came from another device, so it was not done. "
            "Explain that apps, the screen and WhatsApp only work when Boss uses JARVIS on the PC itself; "
            "for their own screen they can tap 'Share screen'."
        )

    if level == DENY:
        await _log(event, "blocked")
        raise ToolError(
            f"Access denied: '{capability}' is turned off in access.yaml. "
            "Tell Boss it can be enabled there."
        )

    approved_by = "policy"
    if level == CONFIRM:
        try:
            approved_by = await _request_approval(policy, event, mode, confirmed, device_identity)
        except ToolError as e:
            status = "needs_confirmation" if "CONFIRMATION REQUIRED" in str(e) else "denied"
            await _log(event, status, reason=str(e)[:200])
            raise
    assert level in (ALLOW, CONFIRM)
    event["approved_by"] = approved_by  # device_link uses it to skip a second popup on the device

    await _log(event, "running")
    try:
        yield event
    except ToolError as e:
        await _log(event, "failed", error=str(e)[:300])
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("tool %s failed", capability)
        await _log(event, "failed", error=f"{type(e).__name__}: {e}"[:300])
        raise ToolError(f"{capability} failed: {e}") from e
    else:
        await _log(event, "done", result=event.get("result"))
