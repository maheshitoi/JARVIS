"""Builds the MCP server connections JARVIS uses, from access.yaml -> capabilities.mcp.servers."""

from __future__ import annotations

import logging
import os
import re
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


_UNSET = re.compile(r"\$\{?\w+\}?")  # a ${VAR} that .env didn't fill in


def _configured_url(spec: dict[str, Any]) -> str | None:
    """The server's URL once its ${VAR}s are filled in from .env; None while it's still blank."""
    url = str(spec.get("url") or "").strip()
    return url if url and not _UNSET.search(url) else None


def _headers(spec: dict[str, Any]) -> dict[str, str] | None:
    """Drop headers whose ${VAR} isn't set yet (e.g. no token needed), instead of sending the placeholder."""
    out = {}
    for k, v in (spec.get("headers") or {}).items():
        v = str(v).strip()
        if v and not _UNSET.search(v) and v.lower() not in ("bearer", "token"):
            out[str(k)] = v
    return out or None


def describe_for_llm(policy: AccessPolicy) -> str:
    """What each connected MCP server is for, so JARVIS knows when to use its tools."""
    lines = []
    for name, _srv in _build_servers(policy, quiet=True):
        spec = (policy.section("mcp").get("servers") or {}).get(name) or {}
        about = " ".join(str(spec.get("about") or "").split())
        if about:
            lines.append(f"- {spec.get('title') or name}: {about}")
    return "\n".join(lines)


def build_toolsets(policy: AccessPolicy) -> list[mcp.MCPToolset]:
    """One MCPToolset per enabled server; the agent connects to them when the session starts."""
    return [mcp.MCPToolset(id=f"mcp_{name}", mcp_server=srv) for name, srv in _build_servers(policy)]


def _build_servers(policy: AccessPolicy, *, quiet: bool = False) -> list[tuple[str, mcp.MCPServer]]:
    if policy.level("mcp.use") == DENY:
        return []
    servers: list[tuple[str, mcp.MCPServer]] = []
    for name, spec in (policy.section("mcp").get("servers") or {}).items():
        if not isinstance(spec, dict) or spec.get("enabled") is not True:
            continue
        spec = _expand(spec)
        allowed = spec.get("allowed_tools") or None
        try:
            if "url" in spec:
                url = _configured_url(spec)
                if url is None:  # slot is ready, URL not given yet (.env)
                    if not quiet:
                        logger.info("MCP server %s: no URL set yet, skipped", name)
                    continue
                servers.append((name, mcp.MCPServerHTTP(
                    url=url, headers=_headers(spec), allowed_tools=allowed,
                    transport_type=spec.get("transport") or "streamable_http",
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
            if not quiet:
                logger.info("MCP server enabled: %s", name)
        except Exception:  # noqa: BLE001 — a broken server must not stop JARVIS
            logger.exception("could not set up MCP server %s", name)
    return servers
