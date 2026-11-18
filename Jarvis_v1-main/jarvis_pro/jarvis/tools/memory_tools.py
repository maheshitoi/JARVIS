"""Long-term memory and contacts, stored as local JSON in data/.

Boss (dashboard, console, owner devices) shares data/memory.json.
Each guest device gets its OWN file, data/memory/<device id>.json, and never sees Boss's
memory or contacts.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from typing import Any

from livekit.agents import RunContext, ToolError, function_tool

from .. import config
from .. import memory as long_memory
from ..guard import action

MEMORY_FILE = config.DATA_DIR / "memory.json"
CONTACTS_FILE = config.DATA_DIR / "contacts.json"
_lock = threading.Lock()


def _load(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _save(path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _memory_file(context: RunContext):
    userdata = getattr(context, "userdata", None)
    if getattr(userdata, "mode", "owner") == "guest":
        device_id = getattr(userdata, "device_id", None)
        if not device_id:
            raise ToolError("Memory isn't available here.")
        folder = config.DATA_DIR / "memory"
        folder.mkdir(exist_ok=True)
        return folder / f"{device_id}.json"
    return MEMORY_FILE


def _scope(context: RunContext) -> str:
    """Boss's memory is "owner"; each guest device keeps its own, separate memory."""
    userdata = getattr(context, "userdata", None)
    if getattr(userdata, "mode", "owner") == "guest":
        device_id = getattr(userdata, "device_id", None)
        if not device_id:
            raise ToolError("Memory isn't available here.")
        return str(device_id)
    return "owner"


def find_contact(name_or_value: str) -> dict[str, Any] | None:
    """Case-insensitive lookup by name or alias. Used by email/phone tools too."""
    key = name_or_value.strip().lower()
    for c in _load(CONTACTS_FILE, []):
        names = [c.get("name", "")] + c.get("aliases", [])
        if key in (n.lower() for n in names if n):
            return c
    return None


@function_tool
async def remember(context: RunContext, fact: str) -> str:
    """Save a long-term fact about the user (preferences, important dates, people, plans).

    Args:
        fact: One short, self-contained sentence, e.g. "The user's sister is called Anya".
    """
    async with action(context, "memory.write", f"Remember: {fact}") as act:
        res = await long_memory.add(fact, scope=_scope(context), source="voice")
        act["result"] = res.get("status", "saved")
    if res.get("status") == "updated":
        return f"Updated what I knew (was: {res.get('previous')})."
    return "Saved to memory."


@function_tool
async def recall(context: RunContext, query: str = "") -> str:
    """Search long-term memory by meaning. Use before saying you don't know something personal about
    the user (preferences, people, plans, past decisions). JARVIS also learns facts automatically
    from conversations, so check here first.

    Args:
        query: What you want to know, e.g. "coffee preference" or "sister's name". Empty = recent facts.
    """
    async with action(context, "memory.read", f"Recall: {query or 'recent'}"):
        facts = await long_memory.search(query, scope=_scope(context), limit=12)
    if not facts:
        return "Nothing in memory about that."
    return "\n".join(f"- {f['fact']}" + (f" ({f['updated']})" if f.get("updated") else "") for f in facts)


@function_tool
async def forget(context: RunContext, what: str) -> str:
    """Delete facts from long-term memory when the user asks you to forget something or says a
    remembered fact is wrong.

    Args:
        what: Words from the fact to delete, e.g. "old phone number".
    """
    async with action(context, "memory.write", f"Forget: {what}") as act:
        n = await long_memory.forget(what, scope=_scope(context))
        act["result"] = f"{n} deleted"
    return f"Forgot {n} fact{'s' if n != 1 else ''}." if n else "I had nothing matching that."


@function_tool
async def lookup_contact(context: RunContext, name: str) -> str:
    """Look up a contact's email address and phone number by name.

    Args:
        name: Contact name or nickname, e.g. "Ravi".
    """
    if getattr(getattr(context, "userdata", None), "mode", "owner") == "guest":
        raise ToolError("Contacts aren't available on this device.")  # Boss's contacts stay private
    async with action(context, "memory.read", f"Look up contact: {name}"):
        c = find_contact(name)
    if not c:
        return f"No contact named {name}. Contacts live in jarvis_pro/data/contacts.json."
    return json.dumps({k: c.get(k) for k in ("name", "email", "phone", "notes") if c.get(k)})


TOOLS = [
    ("memory.write", remember),
    ("memory.read", recall),
    ("memory.write", forget),
    ("memory.read", lookup_contact),
]
