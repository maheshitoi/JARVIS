"""Customer calls: business knowledge, customer lookup, outbound dispatch and the call log.

data/business.yaml   what the AI knows (falls back to business.example.yaml)
data/customers.json  who your customers are (falls back to customers.example.json)
data/customer_calls.jsonl  one line per finished call: transcript, outcome, AI summary

Used by agent.py (the call itself), customer_tools.py (tools), server.py and call_customer.py.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from collections import deque
from datetime import datetime
from typing import Any

import yaml

from . import audit, config
from .access import get_policy

logger = logging.getLogger("jarvis.business")

BUSINESS_FILE = config.DATA_DIR / "business.yaml"
CUSTOMERS_FILE = config.DATA_DIR / "customers.json"
CALLS_FILE = config.DATA_DIR / "customer_calls.jsonl"
E164 = re.compile(r"^\+[1-9]\d{6,14}$")
OUTCOMES = ("informed", "confirmed", "rescheduled", "question_answered", "callback_requested",
            "transferred", "not_interested", "voicemail", "wrong_number", "no_answer", "other")
_lock = threading.Lock()


class CallError(Exception):
    """A customer call can't be placed; the message is safe to show to the owner."""


# ------------------------------------------------------------- knowledge ----
def load_business() -> dict[str, Any]:
    path = BUSINESS_FILE if BUSINESS_FILE.exists() else config.DATA_DIR / "business.example.yaml"
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        logger.exception("could not read %s", path)
        return {}


def knowledge_text(business: dict[str, Any]) -> str:
    """Everything the AI may tell customers, as readable YAML (internal call settings left out)."""
    public = {k: v for k, v in business.items() if k not in ("calls", "escalation")}
    return yaml.safe_dump(public, allow_unicode=True, sort_keys=False, width=100).strip()


def assistant_name(business: dict[str, Any]) -> str:
    return str((business.get("company") or {}).get("assistant_name") or "the assistant")


def company_name(business: dict[str, Any]) -> str:
    return str((business.get("company") or {}).get("name") or get_policy().owner_name)


# ------------------------------------------------------------- customers ----
def load_customers() -> list[dict[str, Any]]:
    path = CUSTOMERS_FILE if CUSTOMERS_FILE.exists() else config.DATA_DIR / "customers.example.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        logger.exception("could not read %s", path)
        return []


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def find_customer(*, phone: str | None = None, name: str | None = None) -> dict[str, Any] | None:
    """Match by phone (last 10 digits, so +91 98765 43210 == 9876543210) or by name/id."""
    customers = load_customers()
    if phone:
        want = _digits(phone)[-10:]
        if want:
            for c in customers:
                if _digits(c.get("phone"))[-10:] == want:
                    return c
    if name:
        key = name.strip().lower()
        for c in customers:
            if key in (str(c.get("id", "")).lower(), str(c.get("name", "")).lower()):
                return c
        for c in customers:  # partial: "Ravi" -> "Ravi Kumar"
            if key and key in str(c.get("name", "")).lower():
                return c
    return None


def customer_text(customer: dict[str, Any] | None) -> str:
    if not customer:
        return "Unknown caller: not in the customer list. Ask for their name politely if it's needed."
    return yaml.safe_dump(customer, allow_unicode=True, sort_keys=False, width=100).strip()


def resolve_number(who: str) -> tuple[str, dict[str, Any] | None]:
    """'Ravi' / 'C001' / '+9198...' -> (E.164 number, customer or None)."""
    raw = who.strip()
    customer = None
    if not raw.replace(" ", "").replace("-", "").lstrip("+").isdigit():
        customer = find_customer(name=raw)
        if not customer or not customer.get("phone"):
            raise CallError(f"No customer called '{who}' with a phone number in data/customers.json.")
        raw = str(customer["phone"])
    number = re.sub(r"[^\d+]", "", raw)
    if not E164.match(number):
        raise CallError(f"'{who}' is not a valid international number. Use the format +919876543210.")
    return number, customer or find_customer(phone=number)


# -------------------------------------------------------------- outbound ----
async def dispatch_customer_call(who: str, message: str, *, campaign: str | None = None) -> dict[str, Any]:
    """Start an outbound call where the AI informs the customer of `message`.
    Permission (allow/confirm) is decided by the caller of this function; this only checks the
    hard limits: capability on, trunk configured, allowed country code."""
    from livekit import api

    policy = get_policy()
    if not policy.allowed("customer.call_out"):
        raise CallError("Customer calls are turned off (customer.call_out in access.yaml).")
    trunk = config.env("SIP_OUTBOUND_TRUNK_ID")
    if not trunk:
        raise CallError("SIP_OUTBOUND_TRUNK_ID is missing in jarvis_pro/.env (see README: Customer calls).")
    message = message.strip()
    if not message:
        raise CallError("Say what the call should tell the customer.")
    number, customer = resolve_number(who)
    allowed = policy.section("customer").get("allowed_country_codes") or []
    if allowed and not any(number.startswith(cc) for cc in allowed):
        raise CallError(f"Calls to {number} are blocked: country code not in customer.allowed_country_codes.")

    room = f"cust-out-{uuid.uuid4().hex[:8]}"
    async with api.LiveKitAPI() as lk:
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=config.AGENT_NAME,
                room=room,
                metadata=json.dumps({"mode": "customer_out", "phone_number": number, "message": message,
                                     "campaign": campaign}),
            )
        )
    audit.record({"capability": "customer.call_out", "summary": f"Calling {number}: {message}",
                  "status": "dispatched", "details": {"room": room, "customer": (customer or {}).get("name")}})
    return {"room": room, "number": number, "customer": (customer or {}).get("name")}


# -------------------------------------------------------------- call log ----
SUMMARY_PROMPT = """Summarise this phone call between our AI assistant and a customer for the business owner.
Return JSON only: {{"outcome": one of {outcomes}, "summary": "2-3 sentences", "follow_up": "what staff must do next, or empty"}}

Reason for the call: {purpose}

Transcript:
{transcript}"""


async def summarise(transcript: list[str], purpose: str) -> dict[str, str]:
    if not transcript:
        return {"outcome": "no_answer", "summary": "No conversation took place.", "follow_up": ""}
    from google.genai import types

    from .genai_client import agenerate

    prompt = SUMMARY_PROMPT.format(outcomes=", ".join(OUTCOMES), purpose=purpose or "customer phoned in",
                                   transcript="\n".join(transcript)[-12000:])
    try:
        resp = await agenerate(prompt, types.GenerateContentConfig(response_mime_type="application/json"),
                               fast=True)
        data = json.loads(resp.text or "{}")
        return {"outcome": str(data.get("outcome") or "other"), "summary": str(data.get("summary") or ""),
                "follow_up": str(data.get("follow_up") or "")}
    except Exception as e:  # noqa: BLE001 — the transcript is still saved
        logger.warning("call summary failed: %s", e)
        return {"outcome": "other", "summary": "(summary unavailable)", "follow_up": ""}


async def finish_call(*, direction: str, number: str | None, customer: dict[str, Any] | None,
                      purpose: str, transcript: list[str], started: datetime,
                      recorded: dict[str, Any] | None = None, error: str | None = None,
                      campaign: str | None = None) -> dict[str, Any]:
    """Save one finished call. `recorded` = what the AI logged during the call (outcome, callback)."""
    recorded = recorded or {}
    ai = await summarise(transcript, purpose) if not error else {
        "outcome": "no_answer", "summary": f"Call did not connect: {error}", "follow_up": ""}
    entry = {
        "id": uuid.uuid4().hex[:10],
        "time": started.isoformat(timespec="seconds"),
        "duration_s": int((datetime.now() - started).total_seconds()),
        "direction": direction,
        "campaign": campaign,
        "number": number,
        "customer": (customer or {}).get("name"),
        "customer_id": (customer or {}).get("id"),
        "purpose": purpose,
        "outcome": recorded.get("outcome") or ai["outcome"],
        "summary": ai["summary"],
        "follow_up": "; ".join(x for x in (recorded.get("callback"), ai["follow_up"]) if x),
        "ai_notes": recorded.get("notes", ""),
        "transcript": transcript,
    }
    with _lock, CALLS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    audit.record({"capability": "customer.call_log",
                  "summary": f"{direction} call {entry['customer'] or number}: {entry['outcome']}",
                  "status": "done", "details": {k: entry[k] for k in ("summary", "follow_up")}})
    await _notify_owner(entry)
    return entry


async def _notify_owner(entry: dict[str, Any]) -> None:
    """Email the owner when staff need to act (needs email enabled; best effort)."""
    policy = get_policy()
    if not (entry["follow_up"] and policy.section("customer").get("email_follow_ups_to_owner")
            and policy.allowed("email.send")):
        return
    try:
        import asyncio

        from .tools.email_tools import build_message, creds, smtp_send

        body = "\n".join(f"{k}: {entry[k]}" for k in
                         ("time", "direction", "customer", "number", "purpose", "outcome", "summary", "follow_up"))
        msg = build_message(creds()["EMAIL_ADDRESS"],
                            f"📞 Customer follow-up: {entry['customer'] or entry['number']}", body)
        await asyncio.to_thread(smtp_send, msg)
    except Exception:  # noqa: BLE001
        logger.warning("could not email follow-up", exc_info=True)


def recent_calls(limit: int = 20, *, with_transcript: bool = False) -> list[dict[str, Any]]:
    if not CALLS_FILE.exists():
        return []
    with _lock, CALLS_FILE.open(encoding="utf-8") as f:
        lines = deque(f, maxlen=max(1, min(limit, 200)))
    out = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not with_transcript:
            entry.pop("transcript", None)
        out.append(entry)
    return out


# -------------------------------------------------------------- campaigns ----
class _Blank(dict):
    def __missing__(self, key: str) -> str:
        return ""


def render_message(template: str, customer: dict[str, Any]) -> str:
    """'Hi {name}, reminder: {upcoming}' -> filled from the customer's record (missing fields = blank)."""
    fields = _Blank({k: v for k, v in customer.items() if isinstance(v, (str, int, float))})
    fields.setdefault("first_name", str(customer.get("name", "")).split(" ")[0])
    return template.format_map(fields).strip()


def calling_window() -> tuple[str, str]:
    hours = str((load_business().get("calls") or {}).get("campaign_hours") or "09:00-21:00")
    start, _, end = hours.partition("-")
    return start.strip(), end.strip()


def within_calling_hours(now: datetime | None = None) -> bool:
    start, end = calling_window()
    return start <= (now or datetime.now()).strftime("%H:%M") < end


def campaign_targets(only: list[str] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Customers to call, and reasons others were skipped (do_not_call, no phone)."""
    targets, skipped = [], []
    wanted = {o.strip().lower() for o in only or [] if o.strip()}
    for c in load_customers():
        label = c.get("name") or c.get("id") or "?"
        if wanted and not ({str(c.get("id", "")).lower(), str(c.get("name", "")).lower()} & wanted):
            continue
        if c.get("do_not_call"):
            skipped.append(f"{label}: do_not_call")
        elif not c.get("phone"):
            skipped.append(f"{label}: no phone number")
        else:
            targets.append(c)
    return targets, skipped


async def _wait_until_finished(room: str, max_wait_s: float) -> None:
    """The call's room is deleted when the call ends; wait for that."""
    import asyncio

    from livekit import api

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait_s
    await asyncio.sleep(5)
    async with api.LiveKitAPI() as lk:
        while loop.time() < deadline:
            rooms = await lk.room.list_rooms(api.ListRoomsRequest(names=[room]))
            if not rooms.rooms:
                return
            await asyncio.sleep(4)


async def run_campaign(template: str, customers: list[dict[str, Any]], *, gap_s: float = 5,
                       max_call_s: float = 600, log=print) -> dict[str, Any]:
    """Call customers ONE AT A TIME with a personalised message. Stops outside calling hours.
    One at a time keeps it within Twilio trial limits and lets you watch each call."""
    import asyncio

    campaign = f"camp-{datetime.now():%Y%m%d-%H%M%S}"
    placed, failed = [], []
    audit.record({"capability": "customer.call_out", "summary": f"Campaign {campaign}: {len(customers)} customers",
                  "status": "started", "details": {"message": template}})
    for i, c in enumerate(customers, 1):
        if not within_calling_hours():
            log(f"Stopped: outside calling hours {'-'.join(calling_window())}. "
                f"{len(customers) - i + 1} customers not called.")
            break
        message = render_message(template, c)
        log(f"[{i}/{len(customers)}] Calling {c.get('name')} ({c.get('phone')}): {message}")
        try:
            result = await dispatch_customer_call(str(c["phone"]), message, campaign=campaign)
        except CallError as e:
            log(f"   not called: {e}")
            failed.append(c.get("name"))
            continue
        placed.append(c.get("name"))
        await _wait_until_finished(result["room"], max_call_s)
        log("   call finished")
        if i < len(customers):
            await asyncio.sleep(gap_s)
    log(f"Campaign {campaign} done: {len(placed)} called, {len(failed)} failed. "
        "Results: python call_customer.py --log")
    return {"campaign": campaign, "called": placed, "failed": failed}
