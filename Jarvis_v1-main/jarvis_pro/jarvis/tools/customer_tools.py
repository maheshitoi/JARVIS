"""Customer calls (capabilities.customer in access.yaml).

Customer-side tools (the AI uses them while talking to a customer):
    record_outcome, request_callback, transfer_to_staff, end_call
Owner tools (Boss asks JARVIS):
    call_customer, read_customer_calls

Customer sessions run in "caller" mode, so the guard refuses every owner tool and every
`confirm` action there — a customer can never trigger anything that needs Boss's approval.
"""

from __future__ import annotations

from livekit import api
from livekit.agents import RunContext, ToolError, function_tool, get_job_context

from .. import business
from ..guard import action


def _call_state(context: RunContext) -> dict:
    return context.userdata.extra.setdefault("call", {})


# -------------------------------------------------------- customer side ----
@function_tool
async def record_outcome(context: RunContext, outcome: str, notes: str = "") -> str:
    """Record how the call went. Call this once, just before saying goodbye.

    Args:
        outcome: One of: informed, confirmed, rescheduled, question_answered, callback_requested,
            transferred, not_interested, voicemail, wrong_number, other.
        notes: Key facts staff need (new date/time agreed, questions you couldn't answer, complaints).
    """
    outcome = outcome.strip().lower().replace(" ", "_")
    if outcome not in business.OUTCOMES:
        outcome = "other"
    async with action(context, "customer.log", f"Call outcome: {outcome}", {"notes": notes}):
        state = _call_state(context)
        state["outcome"], state["notes"] = outcome, notes
    return "Recorded. Now say goodbye and end the call."


@function_tool
async def request_callback(context: RunContext, customer_name: str, reason: str, preferred_time: str = "") -> str:
    """Arrange for a staff member to call the customer back (refunds, complaints, anything you can't answer).

    Args:
        customer_name: The customer's name.
        reason: What they need, in detail.
        preferred_time: When they'd like the callback, if they said.
    """
    text = f"Call back {customer_name}: {reason}" + (f" (preferred: {preferred_time})" if preferred_time else "")
    async with action(context, "customer.callback", text,
                      {"number": context.userdata.caller_number, "reason": reason, "when": preferred_time}):
        _call_state(context)["callback"] = text
    return "Callback arranged. Tell the customer our team will call them back."


@function_tool
async def transfer_to_staff(context: RunContext) -> str:
    """Transfer the customer to a human staff member. Use when they ask for a person, are upset,
    or need something you can't handle — and only during the transfer hours."""
    target = str((business.load_business().get("escalation") or {}).get("transfer_number") or "")
    if not target:
        raise ToolError("Live transfer isn't set up. Offer a callback with request_callback instead.")
    identity = context.userdata.caller_identity
    async with action(context, "customer.transfer", f"Transfer customer to {target}"):
        _call_state(context)["outcome"] = "transferred"
        await context.wait_for_playout()
        job = get_job_context()
        await job.api.sip.transfer_sip_participant(
            api.TransferSIPParticipantRequest(room_name=job.room.name, participant_identity=identity,
                                              transfer_to=f"tel:{target}", play_dialtone=True)
        )
    return "Transferred."


@function_tool
async def end_call(context: RunContext) -> str:
    """Hang up. Use only after record_outcome and saying goodbye."""
    async with action(context, "customer.end_call", "End the customer call"):
        await context.wait_for_playout()
        job = get_job_context()
        await job.api.room.delete_room(api.DeleteRoomRequest(room=job.room.name))
    return "Call ended."


# ------------------------------------------------------------ owner side ----
@function_tool
async def call_customer(context: RunContext, who: str, message: str, confirmed: bool = False) -> str:
    """Phone a customer to inform them of something (reminder, order update, appointment change).
    The AI handles the call on its own, answers their questions from the business info and logs the outcome.

    Args:
        who: Customer name or id from the customer list, or a number like +919876543210.
        message: Exactly what the customer must be told, with all key facts (date, time, amount, order no).
        confirmed: Set true ONLY after Boss verbally confirmed (when asked to).
    """
    try:
        number, customer = business.resolve_number(who)
    except business.CallError as e:
        raise ToolError(str(e)) from e
    label = (customer or {}).get("name") or number
    async with action(context, "customer.call_out", f"Call customer {label}: {message}",
                      {"number": number, "message": message}, confirmed) as act:
        try:
            act["result"] = (await business.dispatch_customer_call(number, message))["room"]
        except business.CallError as e:
            raise ToolError(str(e)) from e
    return f"Calling {label} now. The outcome will be in the customer call log."


@function_tool
async def read_customer_calls(context: RunContext, count: int = 5) -> str:
    """Read the latest customer calls: who, outcome, summary and follow-ups.

    Args:
        count: How many calls (1-20).
    """
    async with action(context, "customer.read_calls", "Read customer call log"):
        calls = business.recent_calls(max(1, min(count, 20)))
    if not calls:
        return "No customer calls yet."
    return "\n".join(
        f"- {c['time']} {c['direction']} {c.get('customer') or c.get('number')}: {c['outcome']}. {c['summary']}"
        + (f" FOLLOW-UP: {c['follow_up']}" if c.get("follow_up") else "")
        for c in calls
    )


OWNER_TOOLS = [
    ("customer.call_out", call_customer),
    ("customer.read_calls", read_customer_calls),
]
CUSTOMER_TOOLS = [
    ("customer.log", record_outcome),
    ("customer.callback", request_callback),
    ("customer.transfer", transfer_to_staff),
    ("customer.end_call", end_call),
]
