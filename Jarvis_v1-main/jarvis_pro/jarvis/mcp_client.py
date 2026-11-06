"""Builds the MCP server connections JARVIS uses, from access.yaml -> capabilities.mcp.servers."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from livekit.agents import mcp

from . import config
from .access import DENY, AccessPolicy

logger = logging.getLogger("jarvis.mcp")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def build_toolsets(policy: AccessPolicy) -> list[mcp.MCPToolset]:
    """One MCPToolset per enabled server; the agent connects to them when the session starts."""
    return [mcp.MCPToolset(id=f"mcp_{name}", mcp_server=srv) for name, srv in _build_servers(policy)]


def _build_servers(policy: AccessPolicy) -> list[tuple[str, mcp.MCPServer]]:
    if policy.level("mcp.use") == DENY:
        return []
    servers: list[tuple[str, mcp.MCPServer]] = []
    for name, spec in (policy.section("mcp").get("servers") or {}).items():
        if not isinstance(spec, dict) or spec.get("enabled") is not True:
            continue
        spec = _expand(spec)
        allowed = spec.get("allowed_tools") or None
        try:
            if spec.get("url"):
                servers.append((name, mcp.MCPServerHTTP(
                    url=spec["url"], headers=spec.get("headers") or None, allowed_tools=allowed,
                    timeout=float(spec.get("timeout", 10)), client_session_timeout_seconds=90,
                )))
            elif spec.get("command"):
                command = sys.executable if spec["command"] in ("python", "python3", "py") else spec["command"]
                if allowed:
                    logger.warning("allowed_tools is only supported for url servers; ignored for %s", name)
                servers.append((name, mcp.MCPServerStdio(
                    command=command, args=[str(a) for a in spec.get("args") or []],
                    cwd=str(config.BASE_DIR), env={**os.environ, **(spec.get("env") or {})},
                    client_session_timeout_seconds=90,
                )))
            else:
                logger.warning("MCP server %s has neither url nor command — skipped", name)
                continue
            logger.info("MCP server enabled: %s", name)
        except Exception:  # noqa: BLE001 — a broken server must not stop JARVIS
            logger.exception("could not set up MCP server %s", name)
    return servers
