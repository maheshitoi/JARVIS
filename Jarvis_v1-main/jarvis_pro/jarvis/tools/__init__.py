"""Tool registry. Only tools whose capability is not `deny` are given to the model,
so JARVIS never even sees what it isn't allowed to do. Each tool re-checks the
policy when it runs, so live edits to access.yaml still apply."""

from __future__ import annotations

from ..access import AccessPolicy
from ..devices import GUEST_GRANTABLE
from ..device_link import DEVICE_CAPS
from ..guard import is_host_only
from . import (
    database_tools,
    device_tools,
    email_tools,
    memory_tools,
    phone_tools,
    report_tools,
    screen_tools,
    social_tools,
    system_tools,
    whatsapp_tools,
)

_OWNER = (
    memory_tools.TOOLS
    + system_tools.TOOLS
    + screen_tools.TOOLS
    + email_tools.TOOLS
    + phone_tools.OWNER_TOOLS
    + social_tools.TOOLS
    + whatsapp_tools.TOOLS
    + device_tools.TOOLS
    + database_tools.TOOLS
    + report_tools.OWNER_TOOLS
)


def owner_tools(policy: AccessPolicy, *, remote: bool = False, device_app: bool = False) -> list:
    """remote=True: the command comes from a browser on another device, so tools that act on the PC
    running JARVIS (apps, screen, WhatsApp Desktop, PC status) are left out entirely.
    device_app=True: the JARVIS Device app is connected, so device tools run on THAT device;
    host-only tools it can't run (WhatsApp Desktop of the home PC) are left out."""
    def keep(cap: str) -> bool:
        if not policy.allowed(cap):
            return False
        if device_app:
            return cap in DEVICE_CAPS or not is_host_only(cap)
        return not (remote and is_host_only(cap))

    return _unique(t for cap, t in _OWNER if keep(cap))


def guest_tools(policy: AccessPolicy) -> list:
    """Someone else's device: tools that act on their own device, plus its own memory.
    Offered even when this device's level is deny, so asking for them sends Boss a permission
    request (the guard enforces the per-device level on every call)."""
    return _unique(t for cap, t in _OWNER + report_tools.GUEST_TOOLS
                   if cap in GUEST_GRANTABLE and policy.allowed(cap)
                   and t is not memory_tools.lookup_contact)  # Boss's contacts stay private


def caller_tools(policy: AccessPolicy) -> list:
    return _unique(t for cap, t in phone_tools.CALLER_TOOLS if policy.allowed(cap))


def _unique(tools) -> list:
    seen, out = set(), []
    for t in tools:
        if id(t) not in seen:
            seen.add(id(t))
            out.append(t)
    return out
