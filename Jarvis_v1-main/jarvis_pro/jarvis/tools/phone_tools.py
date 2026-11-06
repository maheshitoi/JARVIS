"""Phone calls through LiveKit SIP.

Owner tools : make_phone_call, read_phone_messages
Caller tools: take_message, transfer_to_owner, end_call   (used when someone phones in)

.env: SIP_OUTBOUND_TRUNK_ID (outbound), LIVEKIT_URL/API_KEY/API_SECRET
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime

from livekit import api
from livekit.agents import RunContext, ToolError, function_tool, get_job_context

from .. import config
from ..access import get_policy
from ..guard import action
from .memory_tools import find_contact

MESSAGES_FILE = config.DATA_DIR / "phone_messages.jsonl"
E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def normalise_number(value: str) -> str:
    raw = value.strip()
    if not raw.startswith("+") and not raw.replace(" ", "").replace("-", "").isdigit():
        c = find_contact(raw)
        if not c or not c.get("phone"):
            raise ToolError(f"No phone number for '{value}'. Ask Boss for the number with country code.")
        raw = c["phone"]
    number = re.sub(r"[^\d+]", "", raw)
    if not E164.match(number):
        raise ToolError(f"'{value}' is not a valid international number. It must look like +919876543210.")
    return number


# ------------------------------------------------------------ owner tools ----
@function_tool
async def make_phone_call(context: RunContext, who: str, purpose: str, confirmed: bool = False) -> str:
    """Have JARVIS phone someone on Boss's behalf. JARVIS handles the call in a separate session.

    Args:
        who: Phone number in international format (+91...) or a contact name.
        purpose: What the call is for, in detail (what to say/ask, any key facts).
        confirmed: Set true ONLY after Boss verbally confirmed (when asked to).
    """
    number = normalise_number(who)
    phone_cfg = get_policy().section("phone")
    allowed = phone_cfg.get("allowed_country_codes") or []
    if allowed and not any(number.startswith(cc) for cc in allowed):
        raise ToolError(f"Calls to {number} are blocked: country code not in allowed_country_codes.")
    config.require_env("SIP_OUTBOUND_TRUNK_ID")

    async with action(context, "phone.call_out", f"Call {number}: {purpose}",
                      {"number": number, "purpose": purpose}, confirmed) as act:
        room = f"call-out-{uuid.uuid4().hex[:8]}"
        async with api.LiveKitAPI() as lk:
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=config.AGENT_NAME,
                    room=room,
                    metadata=json.dumps({"mode": "outbound", "phone_number": number, "purpose": purpose}),
                )
            )
        act["result"] = room
    return f"Calling {number} now. I'll take it from here and log the outcome."


@function_tool
async def read_phone_messages(context: RunContext, count: int = 5) -> str:
    """Read the latest messages that phone callers left for Boss.

    Args:
        count: How many messages (1-20).
    """
    async with action(context, "phone.read_messages", "Read phone messages"):
        if not MESSAGES_FILE.exists():
            return "No phone messages."
        lines = MESSAGES_FILE.read_text(encoding="utf-8").splitlines()[-max(1, min(count, 20)):]
    msgs = [json.loads(line) for line in lines if line.strip()]
    return "\n".join(
        f"- {m['time']}: {m['caller_name']} ({m.get('number') or 'unknown'}): {m['message']}"
        + (f" Call back: {m['callback_number']}" if m.get("callback_number") else "")
        for m in reversed(msgs)
    ) or "No phone messages."


# ----------------------------------------------------------- caller tools ----
@function_tool
async def take_message(context: RunContext, caller_name: str, message: str, callback_number: str = "") -> str:
    """Record a message from the caller for Boss.

    Args:
        caller_name: The caller's name.
        message: The full message, in the caller's own words where possible.
        callback_number: A number to call back, if given.
    """
    number = getattr(context.userdata, "caller_number", None)
    entry = {"time": datetime.now().isoformat(timespec="minutes"), "caller_name": caller_name,
             "number": number, "message": message, "callback_number": callback_number}
    async with action(context, "phone.take_message", f"Message from {caller_name}", entry):
        with MESSAGES_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    policy = get_policy()
    if policy.section("phone").get("email_messages_to_owner") and policy.allowed("email.send"):
        try:
            from .email_tools import build_message, creds, smtp_send

            body = "\n".join(f"{k}: {v}" for k, v in entry.items() if v)
            msg = build_message(creds()["EMAIL_ADDRESS"], f"📞 Phone message from {caller_name}", body)
            await asyncio.to_thread(smtp_send, msg)
        except Exception:  # noqa: BLE001 — the message is already saved locally
            pass
    return "Message recorded. Tell the caller Boss will get it."


@function_tool
async def transfer_to_owner(context: RunContext) -> str:
    """Transfer the caller to Boss's real phone. Use only if the caller has an urgent need to speak to Boss."""
    target = str(get_policy().section("phone").get("transfer_number") or "")
    if not target:
        raise ToolError("Transfer isn't set up. Offer to take a message instead.")
    identity = getattr(context.userdata, "caller_identity", None)
    async with action(context, "phone.transfer", f"Transfer caller to {target}"):
        await context.wait_for_playout()
        job = get_job_context()
        await job.api.sip.transfer_sip_participant(
            api.TransferSIPParticipantRequest(
                room_name=job.room.name, participant_identity=identity,
                transfer_to=f"tel:{target}", play_dialtone=True,
            )
        )
    return "Transferred."


@function_tool
async def end_call(context: RunContext) -> str:
    """Hang up. Use after saying goodbye, once the conversation is finished."""
    async with action(context, "phone.end_call", "End the call"):
        await context.wait_for_playout()
        job = get_job_context()
        await job.api.room.delete_room(api.DeleteRoomRequest(room=job.room.name))
    return "Call ended."


OWNER_TOOLS = [
    ("phone.call_out", make_phone_call),
    ("phone.read_messages", read_phone_messages),
]
CALLER_TOOLS = [
    ("phone.take_message", take_message),
    ("phone.transfer", transfer_to_owner),
    ("phone.end_call", end_call),
]
