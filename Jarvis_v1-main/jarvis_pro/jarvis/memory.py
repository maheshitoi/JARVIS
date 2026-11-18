"""Long-term memory: what JARVIS knows about Boss, searchable by meaning (mem0-style).

    jarvis.memory_items   one fact per row (fact / preference / person / plan / decision) with a
                          Gemini embedding, so "what coffee do I like?" finds "Boss drinks filter coffee"
    jarvis.conversations  every spoken line of every session (user + JARVIS), for history and search

How memories are made:
  - explicitly: the `remember` voice tool -> add()
  - automatically: when a conversation ends (or every EXTRACT_EVERY user turns), the new lines are
    read by a Gemini Flash model that pulls out durable facts; near-duplicates (cosine >= DUP_SIM)
    update the existing fact instead of adding a second copy.

Scope: Boss's memories have scope "owner"; each guest device has its own scope (its device id) and
never sees Boss's. Needs JARVIS_DB_URL; without it, memory falls back to data/memory.json keywords.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from . import config

logger = logging.getLogger("jarvis.memory")

SCHEMA = "jarvis"
EMBED_DIMS = 768
DUP_SIM = 0.90        # this similar to an existing fact = same fact, update it
MIN_SIM = 0.60        # below this a search hit is noise (measured: unrelated facts score ~0.50-0.55)
EXTRACT_EVERY = 12    # user turns between automatic extractions in a long session
KINDS = ("fact", "preference", "person", "plan", "decision")

DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE TABLE IF NOT EXISTS {SCHEMA}.memory_items (
    id          text PRIMARY KEY,
    scope       text NOT NULL DEFAULT 'owner',
    kind        text NOT NULL DEFAULT 'fact',
    fact        text NOT NULL,
    source      text,                       -- 'voice', 'auto', 'import'
    embedding   real[],
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    used_count  integer NOT NULL DEFAULT 0,
    last_used   timestamptz
);
CREATE INDEX IF NOT EXISTS memory_items_scope_idx ON {SCHEMA}.memory_items (scope);
CREATE TABLE IF NOT EXISTS {SCHEMA}.conversations (
    id          bigserial PRIMARY KEY,
    session_id  text NOT NULL,
    ts          timestamptz NOT NULL DEFAULT now(),
    scope       text NOT NULL DEFAULT 'owner',
    mode        text,
    role        text NOT NULL,              -- user / assistant
    text        text NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_session_idx ON {SCHEMA}.conversations (session_id, ts);
CREATE INDEX IF NOT EXISTS conversations_ts_idx ON {SCHEMA}.conversations (ts DESC);
CREATE INDEX IF NOT EXISTS conversations_text_idx
    ON {SCHEMA}.conversations USING gin (to_tsvector('simple', text));
"""

_schema_ready = False
_schema_lock = threading.Lock()


def db_ready() -> bool:
    return bool(config.env("JARVIS_DB_URL"))


def _connect():
    import psycopg

    conn = psycopg.connect(config.env("JARVIS_DB_URL"), connect_timeout=10, autocommit=True)
    global _schema_ready
    if not _schema_ready:
        with _schema_lock:
            if not _schema_ready:
                conn.execute(DDL)
                _schema_ready = True
    return conn


# -------------------------------------------------------------- embeddings --
async def embed(texts: list[str], *, query: bool = False) -> list[list[float]] | None:
    """Gemini embeddings (768 dims), or None when unavailable (memory then uses keywords)."""
    if not texts:
        return []
    try:
        from google.genai import types

        from .genai_client import client

        res = await client().aio.models.embed_content(
            model=config.env("JARVIS_EMBED_MODEL") or "gemini-embedding-001",
            contents=texts,
            config=types.EmbedContentConfig(
                output_dimensionality=EMBED_DIMS,
                task_type="RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT",
            ),
        )
        return [list(e.values) for e in res.embeddings]
    except Exception:  # noqa: BLE001 — memory must keep working without embeddings
        logger.warning("embedding failed; using keyword search", exc_info=True)
        return None


def _cos(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _keyword_score(query: str, fact: str) -> float:
    words = {w for w in query.lower().split() if len(w) > 2}
    if not words:
        return 0.0
    f = fact.lower()
    return sum(1 for w in words if w in f) / len(words)


# ---------------------------------------------------------------- facts --
def _json_file(scope: str):
    if scope == "owner":
        return config.DATA_DIR / "memory.json"
    folder = config.DATA_DIR / "memory"
    folder.mkdir(exist_ok=True)
    return folder / f"{scope}.json"


def _json_load(scope: str) -> list[dict[str, Any]]:
    p = _json_file(scope)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except json.JSONDecodeError:
        return []


def _json_append(scope: str, fact: str) -> None:
    """Keep data/memory.json in step (it stays a readable backup and works without a database)."""
    p = _json_file(scope)
    facts = _json_load(scope)
    if any(f.get("fact", "").strip().lower() == fact.strip().lower() for f in facts):
        return
    facts.append({"id": uuid.uuid4().hex[:8], "fact": fact, "saved": datetime.now().isoformat(timespec="minutes")})
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(facts, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


async def add(fact: str, *, kind: str = "fact", scope: str = "owner", source: str = "voice") -> dict[str, Any]:
    """Save a fact; a near-duplicate of an existing fact updates it instead. Returns {id, status}."""
    fact = " ".join(fact.split())
    if not fact:
        return {"status": "empty"}
    kind = kind if kind in KINDS else "fact"
    await asyncio.to_thread(_json_append, scope, fact)
    if not db_ready():
        return {"status": "saved", "id": None}
    vecs = await embed([fact])
    vec = vecs[0] if vecs else None

    def write() -> dict[str, Any]:
        with _connect() as conn:
            if vec is not None:
                rows = conn.execute(f"SELECT id, fact, embedding FROM {SCHEMA}.memory_items "
                                    f"WHERE scope = %s AND embedding IS NOT NULL", (scope,)).fetchall()
                best = max(rows, key=lambda r: _cos(vec, r[2]), default=None)
                if best is not None and _cos(vec, best[2]) >= DUP_SIM:
                    conn.execute(f"UPDATE {SCHEMA}.memory_items SET fact = %s, kind = %s, embedding = %s, "
                                 f"updated_at = now() WHERE id = %s", (fact, kind, vec, best[0]))
                    return {"status": "updated", "id": best[0], "previous": best[1]}
            else:
                hit = conn.execute(f"SELECT id FROM {SCHEMA}.memory_items WHERE scope = %s AND lower(fact) = lower(%s)",
                                   (scope, fact)).fetchone()
                if hit:
                    return {"status": "exists", "id": hit[0]}
            mid = uuid.uuid4().hex[:12]
            conn.execute(f"INSERT INTO {SCHEMA}.memory_items (id, scope, kind, fact, source, embedding) "
                         f"VALUES (%s, %s, %s, %s, %s, %s)", (mid, scope, kind, fact, source, vec))
            return {"status": "saved", "id": mid}

    return await asyncio.to_thread(write)


async def search(query: str, *, scope: str = "owner", limit: int = 8) -> list[dict[str, Any]]:
    """Facts most related to `query` (by meaning when embeddings work, else by keywords)."""
    limit = max(1, min(limit, 50))
    if not db_ready():
        facts = _json_load(scope)
        scored = sorted(((_keyword_score(query, f["fact"]), f) for f in facts), key=lambda x: -x[0])
        hits = [f for s, f in scored if s > 0][:limit] if query.strip() else facts[-limit:]
        return [{"fact": f["fact"], "saved": f.get("saved")} for f in hits]

    vecs = await embed([query], query=True) if query.strip() else None
    qvec = vecs[0] if vecs else None

    def read() -> list[dict[str, Any]]:
        with _connect() as conn:
            rows = conn.execute(f"SELECT id, kind, fact, embedding, updated_at FROM {SCHEMA}.memory_items "
                                f"WHERE scope = %s", (scope,)).fetchall()
            if not query.strip():
                picked = sorted(rows, key=lambda r: r[4], reverse=True)[:limit]
                scored = [(1.0, r) for r in picked]
            else:
                scored = []
                for r in rows:
                    s = _cos(qvec, r[3]) if qvec is not None and r[3] else 0.0
                    s = max(s, _keyword_score(query, r[2]) * 0.8)
                    if s >= MIN_SIM:
                        scored.append((s, r))
                scored.sort(key=lambda x: -x[0])
                scored = scored[:limit]
            if scored:
                conn.execute(f"UPDATE {SCHEMA}.memory_items SET used_count = used_count + 1, last_used = now() "
                             f"WHERE id = ANY(%s)", ([r[0] for _, r in scored],))
            return [{"id": r[0], "kind": r[1], "fact": r[2], "updated": r[4].isoformat(timespec="minutes"),
                     "score": round(s, 2)} for s, r in scored]

    return await asyncio.to_thread(read)


async def forget(memory_id_or_text: str, *, scope: str = "owner") -> int:
    if not db_ready():
        return 0

    def delete() -> int:
        with _connect() as conn:
            cur = conn.execute(f"DELETE FROM {SCHEMA}.memory_items WHERE scope = %s AND (id = %s OR fact ILIKE %s)",
                               (scope, memory_id_or_text, f"%{memory_id_or_text}%"))
            return cur.rowcount

    return await asyncio.to_thread(delete)


async def import_json() -> int:
    """Copy facts from data/memory.json into the database (run once; safe to repeat)."""
    n = 0
    for f in _json_load("owner"):
        if (await add(f["fact"], source="import"))["status"] == "saved":
            n += 1
    return n


# ---------------------------------------------------------- extraction --
EXTRACT_PROMPT = """From this conversation between {owner} and the assistant JARVIS, extract facts about
{owner} worth remembering for months: stable personal facts, preferences, people in their life (name +
relation), ongoing plans/projects with dates, and decisions they made. Ignore small talk, one-off
requests, anything JARVIS said about itself, and anything already obvious or temporary.

Write each as one short self-contained sentence in the third person using "{owner}".
Return JSON: {{"memories": [{{"kind": "fact|preference|person|plan|decision", "fact": "..."}}]}}.
Return {{"memories": []}} if nothing is worth keeping.

Conversation:
{lines}"""


async def extract(lines: list[tuple[str, str]], *, owner: str, scope: str = "owner") -> list[dict[str, Any]]:
    """Pull durable facts out of (role, text) lines and save them. Returns what was saved/updated."""
    convo = "\n".join(f"{'JARVIS' if r == 'assistant' else owner}: {t}" for r, t in lines if t.strip())
    if len(convo) < 40:
        return []
    from google.genai import types

    from .genai_client import agenerate

    try:
        resp = await agenerate(EXTRACT_PROMPT.format(owner=owner, lines=convo[-20000:]),
                               types.GenerateContentConfig(response_mime_type="application/json"), fast=True)
        items = json.loads(resp.text or "{}").get("memories", [])
    except Exception:  # noqa: BLE001
        logger.warning("memory extraction failed", exc_info=True)
        return []
    saved = []
    for it in items[:20]:
        if isinstance(it, dict) and it.get("fact"):
            r = await add(str(it["fact"]), kind=str(it.get("kind", "fact")), scope=scope, source="auto")
            if r.get("status") in ("saved", "updated"):
                saved.append({**r, "fact": it["fact"]})
    if saved:
        logger.info("auto-memory: %d facts saved/updated", len(saved))
    return saved


# ------------------------------------------------------- conversation log --
class ConversationRecorder:
    """Attached to an AgentSession: saves every spoken line and extracts memories as it goes."""

    def __init__(self, session: Any, *, owner: str, scope: str = "owner", mode: str = "owner") -> None:
        self.session_id = uuid.uuid4().hex[:16]
        self.owner, self.scope, self.mode = owner, scope, mode
        self.pending: list[tuple[str, str]] = []   # lines not yet run through extraction
        self.user_turns = 0
        self._tasks: set[asyncio.Task] = set()
        session.on("conversation_item_added", self._on_item)
        session.on("close", self._on_close)

    def _spawn(self, coro) -> None:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _on_item(self, ev: Any) -> None:
        item = getattr(ev, "item", None)
        role = getattr(item, "role", None)
        if role not in ("user", "assistant"):
            return
        text = (getattr(item, "text_content", None) or "").strip()
        if not text:
            return
        self.pending.append((role, text))
        if db_ready():
            self._spawn(asyncio.to_thread(self._save_line, role, text))
        if role == "user":
            self.user_turns += 1
            if self.user_turns % EXTRACT_EVERY == 0:
                self._flush()

    def _save_line(self, role: str, text: str) -> None:
        try:
            with _connect() as conn:
                conn.execute(f"INSERT INTO {SCHEMA}.conversations (session_id, scope, mode, role, text) "
                             f"VALUES (%s, %s, %s, %s, %s)", (self.session_id, self.scope, self.mode, role, text))
        except Exception:  # noqa: BLE001
            logger.debug("could not save conversation line", exc_info=True)

    def _flush(self) -> None:
        lines, self.pending = self.pending, []
        if lines:
            self._spawn(extract(lines, owner=self.owner, scope=self.scope))

    def _on_close(self, _ev: Any) -> None:
        self._flush()


def recent_conversations(*, limit: int = 30, search_text: str = "", scope: str = "owner") -> list[dict[str, Any]]:
    """Sessions (newest first) with their first line, for the dashboard history; or matching lines."""
    if not db_ready():
        return []
    with _connect() as conn:
        if search_text.strip():
            rows = conn.execute(
                f"SELECT session_id, ts, role, text FROM {SCHEMA}.conversations WHERE scope = %s AND "
                f"(to_tsvector('simple', text) @@ plainto_tsquery('simple', %s) OR text ILIKE %s) "
                f"ORDER BY ts DESC LIMIT %s", (scope, search_text, f"%{search_text}%", limit)).fetchall()
            return [{"session_id": r[0], "ts": r[1].isoformat(), "role": r[2], "text": r[3]} for r in rows]
        rows = conn.execute(
            f"SELECT session_id, min(ts), max(ts), count(*), "
            f"(array_agg(text ORDER BY ts) FILTER (WHERE role = 'user'))[1] "
            f"FROM {SCHEMA}.conversations WHERE scope = %s GROUP BY session_id ORDER BY max(ts) DESC LIMIT %s",
            (scope, limit)).fetchall()
        return [{"session_id": r[0], "started": r[1].isoformat(), "ended": r[2].isoformat(), "lines": r[3],
                 "first": r[4]} for r in rows]


def session_lines(session_id: str) -> list[dict[str, Any]]:
    if not db_ready():
        return []
    with _connect() as conn:
        rows = conn.execute(f"SELECT ts, role, text FROM {SCHEMA}.conversations WHERE session_id = %s ORDER BY ts",
                            (session_id,)).fetchall()
    return [{"ts": r[0].isoformat(), "role": r[1], "text": r[2]} for r in rows]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
