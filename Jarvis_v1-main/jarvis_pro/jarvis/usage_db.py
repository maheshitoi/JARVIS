"""JARVIS usage history in PostgreSQL (the database in JARVIS_DB_URL).

Tables (schema `jarvis`):
    action_log      every audit event: each action JARVIS attempts, as it moves through
                    running -> awaiting_approval -> done / failed / blocked / denied
    actions (view)  one row per action, with its latest status
    memories        facts JARVIS remembers           (data/memory.json)
    devices         registered devices + permissions (data/devices.json, key hashes left out)
    phone_messages  messages taken on customer calls (data/phone_messages.jsonl)
    site_checks     website uptime checks            (data/site_checks.jsonl)

The files stay the source of truth; the database is a queryable copy:
    python -m jarvis.usage_db          create the tables and import / refresh everything
New audit events are also written live by audit.record() (best-effort, in a background thread,
so a missing or slow database never delays JARVIS).
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
from datetime import datetime, timezone
from typing import Any

from . import config

logger = logging.getLogger("jarvis.usage_db")

SCHEMA = "jarvis"

DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

CREATE TABLE IF NOT EXISTS {SCHEMA}.action_log (
    event_hash   text PRIMARY KEY,              -- sha1 of the audit line, makes imports idempotent
    ts           timestamptz NOT NULL,
    action_id    text,                          -- groups the events of one action
    capability   text NOT NULL,                 -- e.g. media.play, database.query
    summary      text,
    status       text NOT NULL,                 -- running, done, failed, awaiting_approval, ...
    mode         text,                          -- owner, guest, caller, worker, mcp
    level        text,                          -- allow / confirm / deny
    approved_by  text,
    device       text,
    call_id      text,                          -- links to the tool call / workspace screen
    result       text,
    error        text,
    reason       text,
    details      jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    raw          jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS action_log_ts_idx         ON {SCHEMA}.action_log (ts DESC);
CREATE INDEX IF NOT EXISTS action_log_capability_idx ON {SCHEMA}.action_log (capability);
CREATE INDEX IF NOT EXISTS action_log_action_id_idx  ON {SCHEMA}.action_log (action_id);
CREATE INDEX IF NOT EXISTS action_log_status_idx     ON {SCHEMA}.action_log (status);

CREATE OR REPLACE VIEW {SCHEMA}.actions AS
SELECT DISTINCT ON (action_id)
       action_id, capability, summary, mode, level, approved_by, device, call_id,
       status AS final_status, result, error, details,
       first_value(ts) OVER w AS started_at,
       ts AS finished_at,
       EXTRACT(EPOCH FROM ts - first_value(ts) OVER w) AS duration_s
FROM {SCHEMA}.action_log
WHERE action_id IS NOT NULL
WINDOW w AS (PARTITION BY action_id ORDER BY ts)
ORDER BY action_id, ts DESC;

CREATE TABLE IF NOT EXISTS {SCHEMA}.memories (
    id     text PRIMARY KEY,
    fact   text NOT NULL,
    saved  timestamp
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.devices (
    id           text PRIMARY KEY,
    name         text,
    role         text,
    enabled      boolean,
    created_at   timestamptz,
    last_seen    timestamptz,
    permissions  jsonb NOT NULL DEFAULT '{{}}'::jsonb
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.phone_messages (
    event_hash       text PRIMARY KEY,
    time             timestamp,
    caller_name      text,
    number           text,
    message          text,
    callback_number  text
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.site_checks (
    event_hash     text PRIMARY KEY,
    ts             timestamptz NOT NULL,
    name           text,
    url            text,
    up             boolean,
    status         integer,
    response_ms    integer,
    ssl_days_left  integer,
    full_check     boolean,
    login_ok       boolean,
    issues         jsonb NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS site_checks_ts_idx ON {SCHEMA}.site_checks (ts DESC);
"""


def configured() -> bool:
    return bool(config.env("JARVIS_DB_URL"))


def _connect():
    import psycopg

    return psycopg.connect(config.env("JARVIS_DB_URL"), connect_timeout=10)


def _hash(line: str) -> str:
    return hashlib.sha1(line.strip().encode("utf-8")).hexdigest()


def _ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _jsonl(path) -> list[tuple[str, dict[str, Any]]]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rows.append((_hash(line), json.loads(line)))
            except json.JSONDecodeError:
                continue
    return rows


# ---------------------------------------------------------------- writes --
_ACTION_SQL = f"""
INSERT INTO {SCHEMA}.action_log (event_hash, ts, action_id, capability, summary, status, mode, level,
                                 approved_by, device, call_id, result, error, reason, details, raw)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (event_hash) DO NOTHING
"""


def _action_row(event_hash: str, e: dict[str, Any]) -> tuple:
    from psycopg.types.json import Jsonb

    details = e.get("details")
    return (
        event_hash, _ts(e.get("ts")) or datetime.now(timezone.utc), e.get("id"),
        e.get("capability") or "unknown", _text(e.get("summary")), e.get("status") or "unknown",
        e.get("mode"), e.get("level"), e.get("approved_by"), _text(e.get("device")), e.get("call_id"),
        _text(e.get("result")), _text(e.get("error")), _text(e.get("reason")),
        Jsonb(details if isinstance(details, dict) else ({"value": details} if details else {})),
        Jsonb(e),
    )


def ensure_schema() -> None:
    with _connect() as conn:
        conn.execute(DDL)


def sync_files() -> dict[str, int]:
    """Create the tables and import everything from the files (safe to run again)."""
    from psycopg.types.json import Jsonb

    from .audit import AUDIT_FILE

    counts: dict[str, int] = {}
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(DDL)

        audit_rows = [_action_row(h, e) for h, e in _jsonl(AUDIT_FILE)]
        cur.executemany(_ACTION_SQL, audit_rows)
        counts["action_log"] = len(audit_rows)

        mem_file = config.DATA_DIR / "memory.json"
        memories = json.loads(mem_file.read_text(encoding="utf-8")) if mem_file.exists() else []
        cur.executemany(
            f"""INSERT INTO {SCHEMA}.memories (id, fact, saved) VALUES (%s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET fact = EXCLUDED.fact, saved = EXCLUDED.saved""",
            [(m["id"], m.get("fact", ""), _ts(m.get("saved"))) for m in memories if m.get("id")],
        )
        counts["memories"] = len(memories)

        dev_file = config.DATA_DIR / "devices.json"
        devices = json.loads(dev_file.read_text(encoding="utf-8")) if dev_file.exists() else []
        cur.executemany(
            f"""INSERT INTO {SCHEMA}.devices (id, name, role, enabled, created_at, last_seen, permissions)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, role = EXCLUDED.role,
                    enabled = EXCLUDED.enabled, last_seen = EXCLUDED.last_seen,
                    permissions = EXCLUDED.permissions""",
            [(d["id"], d.get("name"), d.get("role"), d.get("enabled"), _ts(d.get("created")),
              _ts(d.get("last_seen")), Jsonb(d.get("permissions") or {})) for d in devices if d.get("id")],
        )
        counts["devices"] = len(devices)

        msgs = _jsonl(config.DATA_DIR / "phone_messages.jsonl")
        cur.executemany(
            f"""INSERT INTO {SCHEMA}.phone_messages
                    (event_hash, time, caller_name, number, message, callback_number)
                VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (event_hash) DO NOTHING""",
            [(h, _ts(m.get("time")), m.get("caller_name"), m.get("number"), m.get("message"),
              m.get("callback_number")) for h, m in msgs],
        )
        counts["phone_messages"] = len(msgs)

        checks = _jsonl(config.DATA_DIR / "site_checks.jsonl")
        cur.executemany(
            f"""INSERT INTO {SCHEMA}.site_checks (event_hash, ts, name, url, up, status, response_ms,
                    ssl_days_left, full_check, login_ok, issues)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (event_hash) DO NOTHING""",
            [(h, _ts(c.get("ts")), c.get("name"), c.get("url"), c.get("up"), c.get("status"),
              c.get("response_ms"), c.get("ssl_days_left"), c.get("full"),
              (c.get("login") or {}).get("ok") if isinstance(c.get("login"), dict) else None,
              Jsonb(c.get("issues") or [])) for h, c in checks],
        )
        counts["site_checks"] = len(checks)
        for table in ("action_log", "memories", "devices", "phone_messages", "site_checks"):
            cur.execute(f"ANALYZE {SCHEMA}.{table}")  # fresh row counts for database_tables
    return counts


# ------------------------------------------------------ live audit writes --
_queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(maxsize=5000)
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def _run_worker() -> None:
    conn = None
    while True:
        line, entry = _queue.get()
        try:
            if conn is None or conn.closed:
                conn = _connect()
                conn.autocommit = True
            conn.execute(_ACTION_SQL, _action_row(_hash(line), entry))
        except Exception:  # noqa: BLE001 — the file log already has it; never break JARVIS
            logger.debug("could not write audit event to the database", exc_info=True)
            try:
                if conn is not None:
                    conn.close()
            except Exception:  # noqa: BLE001
                pass
            conn = None


def record_action(line: str, entry: dict[str, Any]) -> None:
    """Queue one audit event (the exact JSON line written to audit.jsonl) for the database."""
    global _worker
    if not configured():
        return
    with _worker_lock:
        if _worker is None:
            _worker = threading.Thread(target=_run_worker, name="jarvis-usage-db", daemon=True)
            _worker.start()
    try:
        _queue.put_nowait((line, entry))
    except queue.Full:
        logger.warning("usage database queue full; audit event kept in the file only")


if __name__ == "__main__":
    if not configured():
        raise SystemExit("Set JARVIS_DB_URL in jarvis_pro/.env first.")
    for table, n in sync_files().items():
        print(f"{SCHEMA}.{table:<15} {n} rows from file")
