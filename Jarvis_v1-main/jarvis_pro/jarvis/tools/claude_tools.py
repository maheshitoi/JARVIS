"""Hand hard jobs to Claude (jarvis/claude_brain.py): answer now, or run in the background."""

from __future__ import annotations

from livekit.agents import RunContext, ToolError, function_tool, get_job_context

from .. import claude_brain, tasks, workspace
from ..guard import action

_EFFORT = {"quick": "low", "normal": "medium", "deep": "high"}


def _room():
    try:
        return get_job_context().room
    except RuntimeError:
        return None


@function_tool
async def ask_claude(context: RunContext, job: str, depth: str = "normal") -> str:
    """Hand a job that needs real thinking to Claude, JARVIS's deep-reasoning brain, and wait for
    the answer (usually 10-60 seconds; say "give me a moment" first). Claude can use the database,
    website status, long-term memory, the MCP project tools and web search.

    Use it for: reports and analysis across several tables or periods, comparisons, "why" questions
    about the data, plans, research, and writing (emails, documents, summaries). Do NOT use it for
    simple lookups, single numbers, chit-chat, or controlling the PC; do those yourself.
    For jobs that will take minutes, use start_background_task instead.

    After it returns: read only the short spoken summary at the top aloud; the full answer is shown
    on the screen automatically.

    Args:
        job: The complete job in one or two sentences, with all details the user gave
             (dates, names, filters, format), e.g. "Compare successful vs failed payments per day for
             the last 14 days and explain any spikes".
        depth: "quick", "normal" or "deep" (deep = most thorough, slower).
    """
    if not claude_brain.configured():
        raise ToolError("Claude isn't connected: ANTHROPIC_API_KEY is missing in .env. Tell Boss.")
    room = _room()
    call = getattr(context, "function_call", None)

    async def progress(msg: str) -> None:
        if call is not None:
            await workspace.show(room, call.call_id, call.name, state="progress", message=msg)

    async with action(context, "claude.ask", f"Claude: {job}"[:160], {"depth": depth}) as act:
        try:
            res = await claude_brain.run_task(job, context=context, progress=progress,
                                              effort=_EFFORT.get(depth, "medium"))
        except RuntimeError as e:
            raise ToolError(str(e)) from e
        act["result"] = f"{res.tool_calls} tool calls, {res.turns} turns"
    return res.text


@function_tool
async def start_background_task(context: RunContext, job: str, title: str) -> str:
    """Start a long job that Claude works on in the background while you keep talking with the user.
    It gets its own screen with live progress, and you will be told to announce it when it is done.
    Use it when the user says "in the background", "let me know when it's ready", or for big jobs
    (full weekly/monthly reports, research with many sources, analysing lots of data).

    Args:
        job: The complete job with every detail the user gave.
        title: A 2-5 word name for it, e.g. "Weekly payments report".
    """
    if not claude_brain.configured():
        raise ToolError("Claude isn't connected: ANTHROPIC_API_KEY is missing in .env. Tell Boss.")
    async with action(context, "claude.task", f"Background task: {title}", {"job": job[:300]}) as act:
        try:
            t = tasks.start(job, title, context=context, session=context.session, room=_room())
        except RuntimeError as e:
            raise ToolError(str(e)) from e
        act["result"] = f"task {t.id} started"
    return (f"Started background task '{t.title}' (id {t.id}). Tell the user it is running and you'll "
            "let them know when it's done; then carry on with the conversation.")


@function_tool
async def list_background_tasks(context: RunContext) -> str:
    """Show the background tasks: which are running, done or failed, with results of finished ones."""
    async with action(context, "claude.task", "List background tasks"):
        items = tasks.recent(10)
    if not items:
        return "No background tasks yet."
    lines = []
    for t in items:
        line = f"- {t.title} (id {t.id}): {t.status}"
        if t.status == "done":
            line += f" — {t.result[:300]}"
        elif t.status == "failed":
            line += f" — {t.error[:200]}"
        lines.append(line)
    return "\n".join(lines)


@function_tool
async def cancel_background_task(context: RunContext, task: str) -> str:
    """Stop a running background task.

    Args:
        task: Its id or part of its title.
    """
    async with action(context, "claude.task", f"Cancel background task: {task}"):
        t = tasks.find(task)
        if t is None:
            raise ToolError(f"No background task matches '{task}'.")
        ok = tasks.cancel(t)
    return f"Cancelled '{t.title}'." if ok else f"'{t.title}' isn't running ({t.status})."


TOOLS = [
    ("claude.ask", ask_claude),
    ("claude.task", start_background_task),
    ("claude.task", list_background_tasks),
    ("claude.task", cancel_background_task),
]
