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
        path = _memory_file(context)
        with _lock:
            facts = _load(path, [])
            facts.append({"id": uuid.uuid4().hex[:8], "fact": fact,
                          "saved": datetime.now().isoformat(timespec="minutes")})
            _save(path, facts)
        act["result"] = "saved"
    return "Saved to memory."


@function_tool
async def recall(context: RunContext, query: str = "") -> str:
    """Search long-term memory. Use before saying you don't know something personal about the user.

    Args:
        query: Keywords to search for. Empty returns the most recent facts.
    """
    async with action(context, "memory.read", f"Recall: {query or 'recent'}"):
        facts = _load(_memory_file(context), [])
        if query:
            words = [w for w in query.lower().split() if len(w) > 2]
            facts = [f for f in facts if any(w in f["fact"].lower() for w in words)] or facts
        facts = facts[-20:]
    if not facts:
        return "Memory is empty."
    return "\n".join(f"- {f['fact']} (saved {f['saved']})" for f in facts)


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
    ("memory.read", lookup_contact),
]
