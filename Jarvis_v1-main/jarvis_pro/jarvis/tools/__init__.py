"""Tool registry. Only tools whose capability is not `deny` are given to the model,
so JARVIS never even sees what it isn't allowed to do. Each tool re-checks the
policy when it runs, so live edits to access.yaml still apply."""

from __future__ import annotations

from ..access import AccessPolicy
from ..guard import is_host_only
from . import (
    email_tools,
    memory_tools,
    phone_tools,
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
)


def owner_tools(policy: AccessPolicy, *, remote: bool = False) -> list:
    """remote=True: the command comes from another device, so tools that act on the PC
    running JARVIS (apps, screen, WhatsApp Desktop, PC status) are left out entirely."""
    return _unique(t for cap, t in _OWNER
                   if policy.allowed(cap) and not (remote and is_host_only(cap)))


def caller_tools(policy: AccessPolicy) -> list:
    return _unique(t for cap, t in phone_tools.CALLER_TOOLS if policy.allowed(cap))


def _unique(tools) -> list:
    seen, out = set(), []
    for t in tools:
        if id(t) not in seen:
            seen.add(id(t))
            out.append(t)
    return out
