"""Background tasks: long jobs Claude works on while Boss keeps talking to JARVIS.

"Prepare this week's business report and tell me when it's ready" starts a task: it gets its own
screen on the dashboard (live progress, then the result), JARVIS says it's done when it finishes,
and every task is kept in jarvis.tasks (PostgreSQL) for later ("what did that report say?").
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import claude_brain, config, workspace

logger = logging.getLogger("jarvis.tasks")

MAX_RUNNING = 3

DDL = """
CREATE SCHEMA IF NOT EXISTS jarvis;
CREATE TABLE IF NOT EXISTS jarvis.tasks (
    id           text PRIMARY KEY,
    title        text NOT NULL,
    task         text NOT NULL,
    status       text NOT NULL,          -- running / done / failed / cancelled
    created_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    result       text,
    error        text,
    tool_calls   integer,
    usage        jsonb
);
CREATE INDEX IF NOT EXISTS tasks_created_idx ON jarvis.tasks (created_at DESC);
"""


@dataclass
class Task:
    id: str
    title: str
    task: str
    status: str = "running"
    created: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    result: str = ""
    error: str = ""
    handle: asyncio.Task | None = None


_tasks: dict[str, Task] = {}


def _db(sql: str, params: tuple = ()) -> None:
    if not config.env("JARVIS_DB_URL"):
        return
    try:
        import psycopg

        with psycopg.connect(config.env("JARVIS_DB_URL"), connect_timeout=10, autocommit=True) as conn:
            conn.execute(DDL)
            conn.execute(sql, params)
    except Exception:  # noqa: BLE001 — history is a nice-to-have
        logger.debug("could not record task", exc_info=True)


def running() -> list[Task]:
    return [t for t in _tasks.values() if t.status == "running"]


def recent(limit: int = 10) -> list[Task]:
    return sorted(_tasks.values(), key=lambda t: t.created, reverse=True)[:limit]


def find(id_or_title: str) -> Task | None:
    key = id_or_title.strip().lower()
    for t in recent(50):
        if t.id == key or key in t.title.lower():
            return t
    return None


def start(task: str, title: str, *, context: Any, session: Any, room: Any) -> Task:
    if len(running()) >= MAX_RUNNING:
        raise RuntimeError(f"{MAX_RUNNING} background tasks are already running; wait for one to finish.")
    t = Task(id=uuid.uuid4().hex[:8], title=title.strip()[:80] or task[:60], task=task)
    _tasks[t.id] = t
    screen = f"task-{t.id}"
    _db("INSERT INTO jarvis.tasks (id, title, task, status) VALUES (%s, %s, %s, 'running')", (t.id, t.title, task))

    async def progress(msg: str) -> None:
        await workspace.show(room, screen, "background_task", state="progress", message=msg)

    async def run() -> None:
        await workspace.show(room, screen, "background_task", state="start",
                             args={"task": t.title, "status": "running in the background"})
        try:
            res = await claude_brain.run_task(task, context=context, progress=progress, effort="high")
            t.status, t.result = "done", res.text
            _db("UPDATE jarvis.tasks SET status='done', finished_at=now(), result=%s, tool_calls=%s, usage=%s "
                "WHERE id=%s", (res.text, res.tool_calls, json.dumps(res.usage), t.id))
            await workspace.show(room, screen, "background_task", state="result", output=res.text, error=False)
            await _announce(session, f"The background task \"{t.title}\" is finished. Its full result is on "
                                     f"screen. In one or two spoken sentences, tell {_owner()} the key finding "
                                     f"from this result:\n\n{res.text[:3000]}")
        except asyncio.CancelledError:
            t.status = "cancelled"
            _db("UPDATE jarvis.tasks SET status='cancelled', finished_at=now() WHERE id=%s", (t.id,))
            await workspace.show(room, screen, "background_task", state="cancelled")
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("background task %s failed", t.id)
            t.status, t.error = "failed", str(e)
            _db("UPDATE jarvis.tasks SET status='failed', finished_at=now(), error=%s WHERE id=%s", (str(e), t.id))
            await workspace.show(room, screen, "background_task", state="result", output=str(e), error=True)
            await _announce(session, f"Tell {_owner()} briefly that the background task \"{t.title}\" failed: {e}")

    t.handle = asyncio.ensure_future(run())
    return t


def cancel(t: Task) -> bool:
    if t.status != "running" or t.handle is None:
        return False
    t.handle.cancel()
    return True


def _owner() -> str:
    from .access import get_policy

    return get_policy().owner_name


async def _announce(session: Any, instructions: str) -> None:
    """Make JARVIS speak up about a finished task (if Boss is still connected)."""
    if session is None:
        return
    try:
        session.generate_reply(instructions=instructions)
    except Exception:  # noqa: BLE001 — session closed: the result is still on screen / in the DB
        logger.debug("could not announce task result", exc_info=True)
