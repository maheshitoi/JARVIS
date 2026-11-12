"""Multi-device registry, per-device permissions and permission requests.

Devices
    Each device that runs the JARVIS Device app has a secret key (stored only as a SHA-256 hash)
    and a role:
        owner  - your own devices: full JARVIS, acting on THAT device
        guest  - someone else's device: only its own screen/windows/keyboard/apps/websites/PC info,
                 plus (if you grant it) its OWN separate memory. Never your email, WhatsApp,
                 contacts, social media, phone or device management.

Per-device permissions  (three layers, an action needs all of them)
    1. access.yaml        - global ceiling. deny there = deny everywhere.
    2. this registry      - per-device level, set by you (dashboard / voice / CLI). It can only
                            make things stricter than access.yaml, never looser.
    3. device.yaml        - on the device itself: what that device's user allows.

Permission requests
    When a device tries something its per-device level denies (but access.yaml allows and the role
    could be granted), a request is queued. You grant it (allow / confirm) or deny it on the
    dashboard or by voice. A grant applies immediately; the user just asks again.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from typing import Any

from . import config

FILE = config.DATA_DIR / "devices.json"  # data/*.json is git-ignored
REQUESTS_FILE = config.DATA_DIR / "permission_requests.json"
ROLES = ("owner", "guest")

ALLOW, CONFIRM, DENY = "allow", "confirm", "deny"
_STRICT = {ALLOW: 0, CONFIRM: 1, DENY: 2}

# Capabilities the Device app performs on its own machine.
DEVICE_CAPS = frozenset({
    "system.info", "system.open_url", "system.open_app",
    "screen.view", "screen.windows", "screen.control",
})
# Extra things a guest device CAN be granted. Its memory is its own file, separate from yours.
# Database / website reports are the owner's data: a guest gets them only when you grant them.
GUEST_GRANTABLE = DEVICE_CAPS | {"memory.read", "memory.write",
                                 "database.schema", "database.query", "websites.report"}
# Never possible from the Device app (home-PC WhatsApp Desktop; live video share is browser-only).
NOT_ON_DEVICE = ("social.whatsapp.", "screen.live_share")

_lock = threading.Lock()


# ---------------------------------------------------------------- storage --
def _load(path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(path, rows: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    tmp.replace(path)


def _public(d: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in d.items() if k != "key_hash"}
    out.setdefault("permissions", {})
    out.setdefault("enabled", True)
    return out


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "device"


# ---------------------------------------------------------------- devices --
def add(name: str, role: str) -> tuple[dict[str, Any], str]:
    """Register a device. Returns (device, secret_key). The key is never stored in plain text."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    name = name.strip()
    if not name:
        raise ValueError("name is required")
    key = "jd_" + secrets.token_urlsafe(32)
    with _lock:
        rows = _load(FILE)
        device = {
            "id": f"{_slug(name)}-{secrets.token_hex(3)}",
            "name": name,
            "role": role,
            "key_hash": _hash(key),
            "created": int(time.time()),
            "last_seen": None,
            "enabled": True,
            "permissions": {},
        }
        rows.append(device)
        _save(FILE, rows)
    return _public(device), key


def remove(device_id: str) -> bool:
    with _lock:
        rows = _load(FILE)
        kept = [d for d in rows if d["id"] != device_id]
        if len(kept) == len(rows):
            return False
        _save(FILE, kept)
        return True


def set_enabled(device_id: str, enabled: bool) -> bool:
    return _update(device_id, lambda d: d.__setitem__("enabled", enabled))


def _update(device_id: str, fn) -> bool:
    with _lock:
        rows = _load(FILE)
        for d in rows:
            if d["id"] == device_id:
                fn(d)
                _save(FILE, rows)
                return True
    return False


def all_devices() -> list[dict[str, Any]]:
    return [_public(d) for d in _load(FILE)]


def get(device_id: str) -> dict[str, Any] | None:
    for d in _load(FILE):
        if d["id"] == device_id:
            return _public(d)
    return None


def find(name_or_id: str) -> dict[str, Any] | None:
    """Match by id, exact name, or a unique partial name (for voice: "Ravi's PC", "ravi")."""
    q = name_or_id.strip().lower()
    rows = all_devices()
    for d in rows:
        if d["id"] == q or d["name"].lower() == q:
            return d
    partial = [d for d in rows if q and q in d["name"].lower()]
    return partial[0] if len(partial) == 1 else None


def verify(key: str) -> dict[str, Any] | None:
    """Return the device for this key (and mark it seen), or None."""
    if not key or not key.startswith("jd_"):
        return None
    digest = _hash(key)
    with _lock:
        rows = _load(FILE)
        for d in rows:
            if secrets.compare_digest(d.get("key_hash", ""), digest):
                if not d.get("enabled", True):
                    return None
                d["last_seen"] = int(time.time())
                _save(FILE, rows)
                return _public(d)
    return None


# ------------------------------------------------------------ permissions --
def grantable(role: str, capability: str) -> bool:
    """Can this kind of device EVER be given this capability?"""
    return role == "owner" or capability in GUEST_GRANTABLE


def default_level(role: str, capability: str, global_level: str) -> str:
    """Level when you haven't set one for this device."""
    if role == "owner" or capability in DEVICE_CAPS:
        return global_level  # owner devices / device-local actions inherit access.yaml
    return DENY  # guest extras (own memory) must be granted explicitly


def effective(device: dict[str, Any], capability: str, global_level: str) -> tuple[str, str]:
    """(level, source) for this device. source: not_grantable | access.yaml | device | default."""
    role = device.get("role", "guest")
    if not grantable(role, capability):
        return DENY, "not_grantable"
    if global_level == DENY:
        return DENY, "access.yaml"
    own = (device.get("permissions") or {}).get(capability)
    source = "device" if own in _STRICT else "default"
    level = own if own in _STRICT else default_level(role, capability, global_level)
    if _STRICT[global_level] > _STRICT[level]:  # access.yaml is the ceiling
        return global_level, "access.yaml"
    return level, source


def set_permission(device_id: str, capability: str, level: str | None) -> dict[str, Any]:
    """Set (allow/confirm/deny) or reset (None / "default") one capability for a device."""
    device = get(device_id)
    if device is None:
        raise ValueError("No such device")
    if level in (None, "default", "inherit", ""):
        level = None
    elif level not in _STRICT:
        raise ValueError("level must be allow, confirm, deny or default")
    if level is not None and level != DENY and not grantable(device["role"], capability):
        raise ValueError(f"A {device['role']} device can't be given '{capability}'.")

    def _apply(d: dict[str, Any]) -> None:
        perms = d.setdefault("permissions", {})
        if level is None:
            perms.pop(capability, None)
        else:
            perms[capability] = level

    _update(device_id, _apply)
    if level in (ALLOW, CONFIRM):  # granted directly: close matching pending requests
        _close_requests(device_id, capability, level)
    return get(device_id)  # type: ignore[return-value]


def _close_requests(device_id: str, capability: str, level: str) -> None:
    with _lock:
        rows = _load(REQUESTS_FILE)
        changed = False
        for r in rows:
            if r["status"] == "pending" and r["device_id"] == device_id and r["capability"] == capability:
                r.update(status="granted", decision=level, decided=int(time.time()))
                changed = True
        if changed:
            _save(REQUESTS_FILE, rows)


def permission_table(device: dict[str, Any], policy) -> list[dict[str, Any]]:
    """Every capability this device could use, with global / device / effective levels."""
    rows = []
    for cap in policy.summary():
        for action_name, global_level in cap["actions"].items():
            cid = f"{cap['id']}.{action_name}"
            if not grantable(device["role"], cid) or cid.startswith(NOT_ON_DEVICE):
                continue
            level, source = effective(device, cid, global_level)
            rows.append({
                "capability": cid,
                "description": cap.get("description", ""),
                "risk": cap.get("risk", "medium"),
                "global": global_level,
                "device": (device.get("permissions") or {}).get(cid),
                "effective": level,
                "source": source,
            })
    return rows


# --------------------------------------------------- permission requests --
def request_permission(device: dict[str, Any], capability: str, summary: str) -> dict[str, Any]:
    """Queue a request for the owner (deduplicated per device + capability)."""
    with _lock:
        rows = _load(REQUESTS_FILE)
        for r in rows:
            if r["status"] == "pending" and r["device_id"] == device["id"] and r["capability"] == capability:
                r["summary"], r["ts"], r["count"] = summary, int(time.time()), r.get("count", 1) + 1
                _save(REQUESTS_FILE, rows)
                return r
        req = {
            "id": uuid.uuid4().hex[:8],
            "device_id": device["id"],
            "device_name": device["name"],
            "role": device["role"],
            "capability": capability,
            "summary": summary,
            "ts": int(time.time()),
            "count": 1,
            "status": "pending",
        }
        rows.append(req)
        _save(REQUESTS_FILE, rows[-300:])
        return req


def requests(status: str | None = "pending") -> list[dict[str, Any]]:
    rows = _load(REQUESTS_FILE)
    rows = [r for r in rows if status is None or r["status"] == status]
    return sorted(rows, key=lambda r: r["ts"], reverse=True)


def resolve_request(request_id: str, decision: str) -> dict[str, Any]:
    """decision: allow | confirm (grant at that level) | deny (refuse; device stays denied)."""
    if decision not in _STRICT:
        raise ValueError("decision must be allow, confirm or deny")
    with _lock:
        rows = _load(REQUESTS_FILE)
        req = next((r for r in rows if r["id"] == request_id), None)
        if req is None:
            raise ValueError("No such request")
        if req["status"] != "pending":
            raise ValueError(f"Request already {req['status']}")
    if decision != DENY:
        set_permission(req["device_id"], req["capability"], decision)
    with _lock:
        rows = _load(REQUESTS_FILE)
        for r in rows:
            if r["id"] == request_id:
                r.update(status="granted" if decision != DENY else "denied",
                         decision=decision, decided=int(time.time()))
                req = r
        _save(REQUESTS_FILE, rows)
    return req


# ----------------------------------------------------------------- online --
def room_device_id(room_name: str) -> str | None:
    """dev-<device id>-<6 hex>  ->  <device id>"""
    if not room_name.startswith("dev-"):
        return None
    return room_name[4:].rsplit("-", 1)[0] or None


async def online_ids() -> set[str]:
    """Devices with a live session right now (a LiveKit room with someone in it)."""
    from livekit import api

    url, key, secret = (config.env(n) for n in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"))
    if not (url and key and secret):
        return set()
    lk = api.LiveKitAPI(url=url, api_key=key, api_secret=secret)
    try:
        resp = await lk.room.list_rooms(api.ListRoomsRequest())
    except Exception:  # noqa: BLE001  (offline / bad creds: just show nothing online)
        return set()
    finally:
        await lk.aclose()
    return {i for r in resp.rooms if r.num_participants > 0 and (i := room_device_id(r.name))}
