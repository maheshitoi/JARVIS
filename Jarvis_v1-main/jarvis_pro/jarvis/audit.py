"""Append-only audit log of every action JARVIS attempts (logs/audit.jsonl)."""

from __future__ import annotations

import json
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

from . import config

AUDIT_FILE = config.LOG_DIR / "audit.jsonl"
_SECRET_HINTS = ("password", "secret", "token", "api_key", "apikey")
_lock = threading.Lock()


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("***" if any(h in k.lower() for h in _SECRET_HINTS) else _clean(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, str) and len(value) > 400:
        return value[:400] + "…"
    return value


def record(event: dict[str, Any]) -> dict[str, Any]:
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **_clean(event)}
    with _lock, AUDIT_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def tail(limit: int = 100) -> list[dict[str, Any]]:
    if not AUDIT_FILE.exists():
        return []
    with _lock, AUDIT_FILE.open(encoding="utf-8") as f:
        lines = deque(f, maxlen=limit)
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
