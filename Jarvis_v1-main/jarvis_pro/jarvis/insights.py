"""Data for the dashboard's Control Center: usage insights, task history and settings.

Reads JARVIS's own tables in the `jarvis` schema (usage_db, memory, tasks). Settings can update a
whitelisted set of .env keys and a few access.yaml -> proactive values, preserving the files'
comments and order.
"""

from __future__ import annotations

import re
from typing import Any

from . import claude_brain, config

ENV_FILE = config.BASE_DIR / ".env"
ENV_KEYS = {  # key -> (secret?, description)
    "ANTHROPIC_API_KEY": (True, "Claude API key (deep-thinking brain)"),
    "CLAUDE_MODEL": (False, "Claude model"),
    "CLAUDE_EFFORT": (False, "Claude effort: low / medium / high / xhigh / max"),
    "JARVIS_MODEL": (False, "Gemini Live voice model"),
    "JARVIS_VOICE": (False, "Gemini voice name"),
}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PROACTIVE_KEYS = {"brief_time": r"\d{1,2}:\d{2}", "brief_with_claude": r"true|false",
                  "speak_info_alerts": r"true|false", "check_every_minutes": r"\d{1,3}"}


def _db():
    import psycopg

    return psycopg.connect(config.env("JARVIS_DB_URL"), connect_timeout=10, autocommit=True)


def _exists(conn, table: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL", (f"jarvis.{table}",)).fetchone()[0]


def usage(days: int = 14) -> dict[str, Any]:
    """Actions per day (done / failed), top capabilities, failures, conversations, memory, tasks."""
    if not config.env("JARVIS_DB_URL"):
        return {"available": False, "reason": "Set JARVIS_DB_URL in .env to see insights."}
    days = max(1, min(days, 90))
    out: dict[str, Any] = {"available": True, "days": days}
    with _db() as conn:
        if not _exists(conn, "action_log"):
            return {"available": False, "reason": "Run `python -m jarvis.usage_db` once to create the tables."}
        out["per_day"] = [
            {"day": r[0].isoformat(), "done": r[1], "failed": r[2]}
            for r in conn.execute(
                """
                WITH d AS (SELECT generate_series(current_date - (%s - 1), current_date, '1 day')::date AS day)
                SELECT d.day,
                       count(a.*) FILTER (WHERE a.final_status = 'done'),
                       count(a.*) FILTER (WHERE a.final_status IN ('failed', 'blocked', 'denied'))
                FROM d LEFT JOIN jarvis.actions a ON a.started_at::date = d.day
                GROUP BY d.day ORDER BY d.day
                """, (days,)).fetchall()]
        out["top"] = [{"capability": r[0], "count": r[1], "failed": r[2]} for r in conn.execute(
            """SELECT capability, count(*), count(*) FILTER (WHERE final_status = 'failed')
               FROM jarvis.actions WHERE started_at >= current_date - %s
               GROUP BY 1 ORDER BY 2 DESC LIMIT 10""", (days,)).fetchall()]
        out["recent_failures"] = [
            {"ts": r[0].isoformat(), "capability": r[1], "summary": r[2], "error": r[3]}
            for r in conn.execute(
                """SELECT finished_at, capability, summary, error FROM jarvis.actions
                   WHERE final_status = 'failed' ORDER BY finished_at DESC LIMIT 8""").fetchall()]
        tot = conn.execute(
            """SELECT count(*), count(*) FILTER (WHERE final_status = 'done'),
                      count(*) FILTER (WHERE final_status = 'failed'),
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_s)
               FROM jarvis.actions WHERE started_at >= current_date - %s""", (days,)).fetchone()
        out["totals"] = {"actions": tot[0], "done": tot[1], "failed": tot[2],
                         "median_s": round(tot[3], 1) if tot[3] is not None else None,
                         "success_rate": round(100 * tot[1] / (tot[1] + tot[2]), 1) if (tot[1] + tot[2]) else None}
        if _exists(conn, "conversations"):
            out["conversations_per_day"] = [
                {"day": r[0].isoformat(), "sessions": r[1], "lines": r[2]}
                for r in conn.execute(
                    """
                    WITH d AS (SELECT generate_series(current_date - (%s - 1), current_date, '1 day')::date AS day)
                    SELECT d.day, count(DISTINCT c.session_id), count(c.id)
                    FROM d LEFT JOIN jarvis.conversations c ON c.ts::date = d.day
                    GROUP BY d.day ORDER BY d.day""", (days,)).fetchall()]
            out["totals"]["sessions"] = sum(r["sessions"] for r in out["conversations_per_day"])
        if _exists(conn, "memory_items"):
            out["totals"]["memories"] = conn.execute(
                "SELECT count(*) FROM jarvis.memory_items WHERE scope = 'owner'").fetchone()[0]
        if _exists(conn, "tasks"):
            r = conn.execute("""SELECT count(*), count(*) FILTER (WHERE status = 'done')
                                FROM jarvis.tasks WHERE created_at >= current_date - %s""", (days,)).fetchone()
            out["totals"]["tasks"], out["totals"]["tasks_done"] = r[0], r[1]
    return out


def task_history(limit: int = 30) -> list[dict[str, Any]]:
    if not config.env("JARVIS_DB_URL"):
        return []
    with _db() as conn:
        if not _exists(conn, "tasks"):
            return []
        rows = conn.execute("""SELECT id, title, status, created_at, finished_at, result, error, tool_calls
                               FROM jarvis.tasks ORDER BY created_at DESC LIMIT %s""", (limit,)).fetchall()
    return [{"id": r[0], "title": r[1], "status": r[2], "created": r[3].isoformat(),
             "finished": r[4].isoformat() if r[4] else None, "result": r[5], "error": r[6],
             "tool_calls": r[7]} for r in rows]


def memories(limit: int = 200) -> list[dict[str, Any]]:
    if not config.env("JARVIS_DB_URL"):
        return []
    with _db() as conn:
        if not _exists(conn, "memory_items"):
            return []
        rows = conn.execute("""SELECT id, kind, fact, source, updated_at, used_count FROM jarvis.memory_items
                               WHERE scope = 'owner' ORDER BY updated_at DESC LIMIT %s""", (limit,)).fetchall()
    return [{"id": r[0], "kind": r[1], "fact": r[2], "source": r[3], "updated": r[4].isoformat(),
             "used": r[5]} for r in rows]


def delete_memory(memory_id: str) -> bool:
    if not config.env("JARVIS_DB_URL"):
        return False
    with _db() as conn:
        return conn.execute("DELETE FROM jarvis.memory_items WHERE scope = 'owner' AND id = %s",
                            (memory_id,)).rowcount > 0


# -------------------------------------------------------------- settings --
def _mask(value: str | None) -> str:
    if not value:
        return ""
    return value[:7] + "…" + value[-4:] if len(value) > 14 else "set"


def settings() -> dict[str, Any]:
    from .access import get_policy

    pro = get_policy().section("proactive")
    return {
        "env": {k: {"value": _mask(config.env(k)) if secret else (config.env(k) or ""),
                    "secret": secret, "set": bool(config.env(k)), "label": label}
                for k, (secret, label) in ENV_KEYS.items()},
        "defaults": {"CLAUDE_MODEL": claude_brain.model(), "CLAUDE_EFFORT": "high",
                     "JARVIS_MODEL": config.REALTIME_MODEL, "JARVIS_VOICE": config.VOICE},
        "efforts": EFFORTS,
        "claude_ready": claude_brain.configured(),
        "database": bool(config.env("JARVIS_DB_URL")),
        "proactive": {k: pro.get(k) for k in PROACTIVE_KEYS},
    }


class SettingsError(ValueError):
    pass


def _check_env_value(key: str, value: str) -> str:
    value = value.strip()
    if any(c in value for c in "\r\n#\"' "):
        raise SettingsError(f"{key}: invalid characters")
    if key == "ANTHROPIC_API_KEY" and value and not value.startswith("sk-ant-"):
        raise SettingsError("That doesn't look like an Anthropic API key (it starts with sk-ant-).")
    if key == "CLAUDE_EFFORT" and value and value not in EFFORTS:
        raise SettingsError(f"Effort must be one of {', '.join(EFFORTS)}.")
    if key in ("CLAUDE_MODEL", "JARVIS_MODEL", "JARVIS_VOICE") and value and not re.fullmatch(r"[\w.\-/]{1,80}", value):
        raise SettingsError(f"{key}: invalid value")
    return value


def update_env(values: dict[str, str]) -> list[str]:
    """Set whitelisted keys in jarvis_pro/.env (in place, comments kept). Returns the keys changed."""
    import os

    clean = {k: _check_env_value(k, str(v)) for k, v in values.items() if k in ENV_KEYS and v is not None}
    if not clean:
        return []
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in clean:
            lines[i] = f"{m.group(1)}={clean[m.group(1)]}"
            seen.add(m.group(1))
    for k, v in clean.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    tmp = ENV_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(ENV_FILE)
    for k, v in clean.items():  # this process (the dashboard) sees it now; the agent on restart
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    claude_brain._client.cache_clear()  # noqa: SLF001 — pick up a new key
    return list(clean)


def update_proactive(values: dict[str, Any]) -> list[str]:
    """Change scalar settings inside access.yaml -> capabilities.proactive, keeping comments."""
    path = config.ACCESS_FILE
    text = path.read_text(encoding="utf-8")
    m = re.search(r"(?m)^  proactive:\n(?:(?:    .*|\s*)\n)+", text)
    if not m:
        raise SettingsError("access.yaml has no proactive section.")
    block = m.group(0)
    changed = []
    for key, pattern in PROACTIVE_KEYS.items():
        if key not in values:
            continue
        raw = values[key]
        val = ("true" if raw else "false") if isinstance(raw, bool) else str(raw).strip()
        if not re.fullmatch(pattern, val):
            raise SettingsError(f"proactive.{key}: invalid value {val!r}")
        shown = f'"{val}"' if key == "brief_time" else val
        new_block, n = re.subn(rf"(?m)^(    {key}:\s*)([^#\n]*?)(\s*(#.*)?)$", rf"\g<1>{shown}\g<3>", block)
        if n:
            block = new_block
            changed.append(key)
    if changed:
        path.write_text(text[:m.start()] + block + text[m.end():], encoding="utf-8")
    return changed


async def test_claude() -> dict[str, Any]:
    """A tiny request to check the key and model work."""
    import anthropic

    if not claude_brain.configured():
        return {"ok": False, "error": "No ANTHROPIC_API_KEY set."}
    try:
        resp = await claude_brain._client().messages.create(  # noqa: SLF001
            model=claude_brain.model(), max_tokens=64, output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Reply with exactly: JARVIS link OK"}])
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return {"ok": True, "model": resp.model, "reply": text[:80]}
    except anthropic.AuthenticationError:
        return {"ok": False, "error": "The key was rejected (401)."}
    except anthropic.NotFoundError:
        return {"ok": False, "error": f"Model {claude_brain.model()} not found for this key."}
    except anthropic.APIStatusError as e:
        return {"ok": False, "error": f"API error {e.status_code}: {e.message}"}
    except anthropic.APIConnectionError:
        return {"ok": False, "error": "Couldn't reach the Claude API."}
