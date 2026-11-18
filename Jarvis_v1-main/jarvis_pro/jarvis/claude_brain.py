"""JARVIS's deep-thinking brain: Claude, via the Anthropic API.

The voice stays Gemini Live (fast, real-time). When a job needs real reasoning, such as a multi-step
report, analysis across tables, a plan or a well-written document, the voice agent hands it to
Claude (tools/claude_tools.py), which works through it with JARVIS's own data tools and returns the
answer to speak and to show on a workspace screen.

Setup: ANTHROPIC_API_KEY in jarvis_pro/.env (optional CLAUDE_MODEL, CLAUDE_EFFORT).
Safety: every tool Claude calls goes through guard.action() with the same RunContext as the voice
tool that started it, so access.yaml levels, approval pop-ups, device rules and the audit log all
apply exactly as if JARVIS had called the tool itself. Claude only gets read-only tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any, Awaitable, Callable

from . import config, database, websites
from .access import DENY, get_policy

logger = logging.getLogger("jarvis.claude")

MAX_TURNS = 16           # model calls per task (each may run several tools)
MAX_TOOL_OUTPUT = 60_000  # characters of one tool result sent back to Claude

Progress = Callable[[str], Awaitable[None]]


def model() -> str:
    return config.env("CLAUDE_MODEL") or "claude-opus-5-5"


def configured() -> bool:
    return bool(config.env("ANTHROPIC_API_KEY") or config.env("ANTHROPIC_AUTH_TOKEN"))


@lru_cache(maxsize=1)
def _client():
    import anthropic

    return anthropic.AsyncAnthropic(timeout=180.0, max_retries=2)


SYSTEM = """You are the deep-thinking brain of JARVIS, a personal AI assistant. The voice side of
JARVIS hands you jobs that need real reasoning: reports, analysis, comparisons, plans, drafts.
You work for one person, the owner, whom JARVIS calls "{owner}".

How to work:
- Use the tools to get real data. Never invent figures, names or dates. If data is missing or a
  tool is denied, say so plainly.
- For database questions: call database_tables first (unless you already know the schema), then
  run precise SELECTs that compute the answer in SQL. Use several queries when it helps.
- The "jarvis" schema holds JARVIS's own usage history (action_log, actions, memories, devices,
  phone_messages, site_checks); other schemas are the owner's business data.
- Work efficiently: run independent tool calls in parallel, stop as soon as you can answer well.

How to answer (your reply is shown on a screen AND read aloud by the voice):
- Start with a 1-2 sentence spoken summary that directly answers the job (it is read aloud).
- Then the details for the screen: short sections, bullet points, and markdown tables for data.
- Exact figures with units and dates. No filler, no restating the question.
"""


@dataclass
class Tool:
    name: str
    capability: str
    summary: Callable[[dict[str, Any]], str]
    spec: dict[str, Any]
    run: Callable[[dict[str, Any]], Awaitable[str]]


@dataclass
class BrainResult:
    text: str
    tool_calls: int = 0
    turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str | None = None


# ------------------------------------------------------------------- tools --
async def _db_tables(_: dict[str, Any]) -> str:
    return await asyncio.to_thread(database.schema_text, False)


async def _db_query(args: dict[str, Any]) -> str:
    res = await asyncio.to_thread(database.run_query, str(args.get("sql", "")))
    return database.format_result(res)


async def _site_status(args: dict[str, Any]) -> str:
    hours = int(args.get("hours") or 24)
    latest = websites.latest_status()
    hist = websites.history(hours=max(1, min(hours, 24 * 31)), name=args.get("site") or None)
    return json.dumps({"latest": latest, "stats": websites.stats(hist), "checks": len(hist)}, default=str)


async def _recall(args: dict[str, Any]) -> str:
    from . import memory  # long-term memory (semantic search)

    hits = await memory.search(str(args.get("query", "")), limit=int(args.get("limit") or 8))
    return json.dumps(hits, default=str, ensure_ascii=False) if hits else "No matching memories."


def _base_tools() -> list[Tool]:
    obj = lambda props, req: {"type": "object", "properties": props, "required": req,  # noqa: E731
                              "additionalProperties": False}
    tools = [
        Tool("database_tables", "database.schema", lambda a: "Claude: read the database structure",
             {"description": "List tables (with columns, types, approximate row counts) in the owner's "
                             "PostgreSQL database, including JARVIS's own usage history in schema 'jarvis'.",
              "input_schema": obj({}, [])}, _db_tables),
        Tool("database_query", "database.query", lambda a: f"Claude query: {a.get('purpose', '')}"[:120],
             {"description": "Run ONE read-only PostgreSQL SELECT (or WITH ... SELECT) and get the rows. "
                             "Compute totals/counts/averages in SQL. Only use names seen in database_tables.",
              "input_schema": obj({"sql": {"type": "string", "description": "A single SELECT statement."},
                                   "purpose": {"type": "string", "description": "What this query finds, in a few words."}},
                                  ["sql", "purpose"])}, _db_query),
        Tool("website_status", "websites.report", lambda a: "Claude: website status",
             {"description": "Uptime, response time and SSL status of the owner's monitored websites: the latest "
                             "check per site plus aggregate stats over the last N hours.",
              "input_schema": obj({"hours": {"type": "integer", "description": "Look-back window in hours (default 24)."},
                                   "site": {"type": "string", "description": "Optional site name to filter."}}, [])},
             _site_status),
        Tool("recall_memory", "memory.read", lambda a: f"Claude recall: {a.get('query', '')}"[:120],
             {"description": "Search JARVIS's long-term memory about the owner (facts, preferences, people, "
                             "past decisions) by meaning.",
              "input_schema": obj({"query": {"type": "string"},
                                   "limit": {"type": "integer", "description": "Max results (default 8)."}},
                                  ["query"])}, _recall),
    ]
    return tools


class _MCPTools:
    """The enabled MCP servers from access.yaml, connected for one brain task."""

    def __init__(self) -> None:
        self.servers: list[tuple[str, Any]] = []
        self.tools: list[Tool] = []

    async def __aenter__(self) -> "_MCPTools":
        from .mcp_client import _build_servers, _inline_refs

        for name, srv in _build_servers(get_policy(), quiet=True):
            try:
                await asyncio.wait_for(srv.initialize(), 20)
                raw = (await srv._client.list_tools()).tools  # noqa: SLF001 — livekit keeps the session here
            except Exception:  # noqa: BLE001 — one broken server must not stop the brain
                logger.warning("MCP server %s unavailable for Claude", name, exc_info=True)
                continue
            self.servers.append((name, srv))
            allowed = None
            spec = (get_policy().section("mcp").get("servers") or {}).get(name) or {}
            if isinstance(spec, dict) and spec.get("allowed_tools"):
                allowed = set(spec["allowed_tools"])
            for t in raw:
                if allowed and t.name not in allowed:
                    continue
                self.tools.append(self._wrap(name, srv, t, _inline_refs(dict(getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}))))
        return self

    @staticmethod
    def _wrap(server: str, srv: Any, t: Any, schema: dict[str, Any]) -> Tool:
        schema.pop("$schema", None)
        schema.setdefault("type", "object")

        async def run(args: dict[str, Any]) -> str:
            res = await srv._client.call_tool(t.name, args)  # noqa: SLF001
            parts = [getattr(c, "text", None) or json.dumps(c.model_dump(), default=str) for c in res.content]
            text = "\n".join(parts)
            if getattr(res, "is_error", False) or getattr(res, "isError", False):
                raise RuntimeError(text or "MCP tool failed")
            return text

        return Tool(f"mcp_{server}__{t.name}"[:64], "mcp.use",
                    lambda a, n=t.name, s=server: f"Claude via {s}: {n}",
                    {"description": (t.description or t.name)[:1000], "input_schema": schema}, run)

    async def __aexit__(self, *exc: Any) -> None:
        for _, srv in self.servers:
            try:
                await srv.aclose()
            except Exception:  # noqa: BLE001
                pass


# -------------------------------------------------------------------- loop --
async def run_task(task: str, *, context: Any = None, progress: Progress | None = None,
                   effort: str | None = None, extra_context: str = "") -> BrainResult:
    """Have Claude work through `task` with JARVIS's tools and return its final answer."""
    import anthropic

    from .guard import action

    if not configured():
        raise RuntimeError("Claude isn't set up: add ANTHROPIC_API_KEY to jarvis_pro/.env.")

    async def say(msg: str) -> None:
        if progress:
            try:
                await progress(msg)
            except Exception:  # noqa: BLE001 — progress is cosmetic
                pass

    policy = get_policy()
    async with _MCPTools() as mcp:
        tools = [t for t in _base_tools() + mcp.tools if policy.level(t.capability) != DENY]
        by_name = {t.name: t for t in tools}
        tool_specs: list[dict[str, Any]] = [{"name": t.name, **t.spec} for t in tools]
        if policy.level("claude.web") != DENY:
            tool_specs.append({"type": "web_search_20260209", "name": "web_search", "max_uses": 5})

        system = SYSTEM.format(owner=policy.owner_name)
        today = datetime.now().astimezone().strftime("%A %d %B %Y, %H:%M %Z")
        user = f"Today is {today}.\n\n{extra_context.strip()}\n\nJob: {task}".replace("\n\n\n", "\n\n")
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        result = BrainResult(text="")
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}

        for turn in range(MAX_TURNS):
            result.turns = turn + 1
            await say("Thinking…" if turn == 0 else "Working through the results…")
            try:
                resp = await _client().beta.messages.create(
                    model=model(),
                    max_tokens=16000,
                    system=system,
                    tools=tool_specs,
                    messages=messages,
                    output_config={"effort": effort or config.env("CLAUDE_EFFORT") or "high"},
                    cache_control={"type": "ephemeral"},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except anthropic.AuthenticationError as e:
                raise RuntimeError("The Claude API key was rejected. Check ANTHROPIC_API_KEY in .env.") from e
            except anthropic.RateLimitError as e:
                raise RuntimeError("Claude is rate-limited right now. Try again in a minute.") from e
            except anthropic.APIStatusError as e:
                raise RuntimeError(f"Claude API error {e.status_code}: {e.message}") from e
            except anthropic.APIConnectionError as e:
                raise RuntimeError("Couldn't reach the Claude API (network).") from e

            for k in usage:
                usage[k] += getattr(resp.usage, k, 0) or 0
            result.stop_reason = resp.stop_reason

            if resp.stop_reason == "refusal":
                result.text = "Claude declined this request."
                break
            if resp.stop_reason == "pause_turn":  # long server-tool turn (web search): let it continue
                messages.append({"role": "assistant", "content": resp.content})
                continue

            calls = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not calls:
                result.text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
                if resp.stop_reason == "max_tokens":
                    result.text += "\n\n(Answer cut short: it hit the length limit.)"
                break

            messages.append({"role": "assistant", "content": resp.content})

            async def run_one(block: Any) -> dict[str, Any]:
                tool = by_name.get(block.name)
                args = block.input if isinstance(block.input, dict) else {}
                if tool is None:
                    return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                            "content": f"Unknown tool {block.name}."}
                await say(tool.summary(args))
                try:
                    async with action(context, tool.capability, tool.summary(args),
                                      {"via": "claude", **({"sql": args["sql"]} if "sql" in args else {})}) as act:
                        out = await tool.run(args)
                        act["result"] = f"{len(out)} chars"
                except Exception as e:  # noqa: BLE001 — Claude gets the error and adapts
                    return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                            "content": str(e)[:2000]}
                if len(out) > MAX_TOOL_OUTPUT:
                    out = out[:MAX_TOOL_OUTPUT] + "\n… (cut: too long; narrow the query)"
                return {"type": "tool_result", "tool_use_id": block.id, "content": out or "(empty)"}

            results = await asyncio.gather(*(run_one(b) for b in calls))
            result.tool_calls += len(calls)
            messages.append({"role": "user", "content": list(results)})
        else:
            result.text = result.text or "I ran out of steps before finishing; here is what I had so far."

        result.usage = usage
        logger.info("claude task done: %d turns, %d tools, usage %s", result.turns, result.tool_calls, usage)
        return result
