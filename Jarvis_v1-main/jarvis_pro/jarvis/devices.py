"""Registry of devices allowed to run the JARVIS Device app.

Each device gets its own secret key (shown once, stored here only as a SHA-256 hash) and a role:

    owner  - your own devices: full JARVIS (per access.yaml), acting on THAT device
    guest  - someone else's device: only tools that act on their own device
             (screen, windows, keyboard, apps, websites, PC info). No email, WhatsApp,
             memory, social media or anything else of yours.

Managed with:  python manage_devices.py add "Ravi's laptop" --role guest
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from typing import Any

from . import config

FILE = config.DATA_DIR / "devices.json"  # data/*.json is git-ignored
ROLES = ("owner", "guest")
_lock = threading.Lock()


def _load() -> list[dict[str, Any]]:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(devices: list[dict[str, Any]]) -> None:
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(devices, indent=2), encoding="utf-8")
    tmp.replace(FILE)


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "device"


def add(name: str, role: str) -> tuple[dict[str, Any], str]:
    """Register a device. Returns (device, secret_key). The key is never stored in plain text."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    name = name.strip()
    if not name:
        raise ValueError("name is required")
    key = "jd_" + secrets.token_urlsafe(32)
    with _lock:
        devices = _load()
        device = {
            "id": f"{_slug(name)}-{secrets.token_hex(3)}",
            "name": name,
            "role": role,
            "key_hash": _hash(key),
            "created": int(time.time()),
            "last_seen": None,
            "enabled": True,
        }
        devices.append(device)
        _save(devices)
    return device, key


def remove(device_id: str) -> bool:
    with _lock:
        devices = _load()
        kept = [d for d in devices if d["id"] != device_id]
        if len(kept) == len(devices):
            return False
        _save(kept)
        return True


def set_enabled(device_id: str, enabled: bool) -> bool:
    with _lock:
        devices = _load()
        for d in devices:
            if d["id"] == device_id:
                d["enabled"] = enabled
                _save(devices)
                return True
    return False


def all_devices() -> list[dict[str, Any]]:
    return [{k: v for k, v in d.items() if k != "key_hash"} for d in _load()]


def verify(key: str) -> dict[str, Any] | None:
    """Return the device for this key (and mark it seen), or None."""
    if not key or not key.startswith("jd_"):
        return None
    digest = _hash(key)
    with _lock:
        devices = _load()
        for d in devices:
            if secrets.compare_digest(d.get("key_hash", ""), digest):
                if not d.get("enabled", True):
                    return None
                d["last_seen"] = int(time.time())
                _save(devices)
                return {k: v for k, v in d.items() if k != "key_hash"}
    return None
