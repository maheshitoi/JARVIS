"""Loads access.yaml and answers "what is JARVIS allowed to do?".

Capability ids look like ``email.send`` or ``social.facebook.post``:
everything before the last dot is the section in access.yaml, the last part
is the action inside that section's ``actions:`` map.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import yaml

from . import config

logger = logging.getLogger("jarvis.access")

ALLOW, CONFIRM, DENY = "allow", "confirm", "deny"
LEVELS = (ALLOW, CONFIRM, DENY)


class AccessPolicy:
    def __init__(self, data: dict[str, Any] | None):
        self.data = data or {}

    # ---- lookups -------------------------------------------------------
    @property
    def capabilities(self) -> dict[str, Any]:
        caps = self.data.get("capabilities")
        return caps if isinstance(caps, dict) else {}

    def section(self, path: str) -> dict[str, Any]:
        node: Any = self.capabilities
        for part in path.split("."):
            if not isinstance(node, dict):
                return {}
            node = node.get(part)
        return node if isinstance(node, dict) else {}

    def level(self, capability: str) -> str:
        """allow | confirm | deny for a capability id. Anything unknown = deny."""
        if "." not in capability:
            return DENY
        section_path, action = capability.rsplit(".", 1)
        node = self.section(section_path)
        if not node or node.get("enabled") is not True:
            return DENY
        level = str((node.get("actions") or {}).get(action, DENY)).strip().lower()
        return level if level in LEVELS else DENY

    def allowed(self, capability: str) -> bool:
        return self.level(capability) != DENY

    @property
    def owner_name(self) -> str:
        return str((self.data.get("owner") or {}).get("name") or "Boss")

    @property
    def approval_timeout(self) -> float:
        return float((self.data.get("approval") or {}).get("timeout_seconds", 60))

    @property
    def allow_voice_confirmation(self) -> bool:
        return bool((self.data.get("approval") or {}).get("allow_voice_confirmation", True))

    # ---- for the dashboard ----------------------------------------------
    def summary(self) -> list[dict[str, Any]]:
        """Flatten every section that has `actions:` into a list for the UI."""
        out: list[dict[str, Any]] = []

        def walk(node: dict[str, Any], path: list[str]) -> None:
            if "actions" in node and isinstance(node.get("actions"), dict):
                cid = ".".join(path)
                out.append(
                    {
                        "id": cid,
                        "description": node.get("description", ""),
                        "risk": node.get("risk", "medium"),
                        "enabled": node.get("enabled") is True,
                        "actions": {a: self.level(f"{cid}.{a}") for a in node["actions"]},
                    }
                )
                return
            for key, child in node.items():
                if isinstance(child, dict):
                    walk(child, path + [key])

        walk(self.capabilities, [])
        return out

    def describe_for_llm(self) -> str:
        lines = []
        for cap in self.summary():
            if not cap["enabled"]:
                continue
            acts = ", ".join(f"{a}={lvl}" for a, lvl in cap["actions"].items() if lvl != DENY)
            if acts:
                lines.append(f"- {cap['id']}: {cap['description']} ({acts})")
        return "\n".join(lines) or "- (no capabilities enabled)"


# ---- hot-reloading singleton -----------------------------------------------
_lock = threading.Lock()
_cache: dict[str, Any] = {"mtime": None, "policy": AccessPolicy({})}


def get_policy() -> AccessPolicy:
    """Return the current policy, re-reading access.yaml if it changed on disk."""
    path = config.ACCESS_FILE
    with _lock:
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            logger.error("access file %s not found — everything is DENIED", path)
            _cache.update(mtime=None, policy=AccessPolicy({}))
            return _cache["policy"]

        if mtime != _cache["mtime"]:
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                _cache.update(mtime=mtime, policy=AccessPolicy(data))
                logger.info("loaded access policy from %s", path)
            except yaml.YAMLError as e:
                # Keep the last good policy rather than failing open or crashing.
                logger.error("access.yaml has a syntax error, keeping previous policy: %s", e)
                _cache["mtime"] = mtime
        return _cache["policy"]
