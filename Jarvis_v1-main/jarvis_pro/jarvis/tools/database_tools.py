"""Read-only reports from your PostgreSQL database (see jarvis/database.py)."""

from __future__ import annotations

import asyncio

from livekit.agents import RunContext, ToolError, function_tool

from .. import database
from ..guard import action


@function_tool
async def database_tables(context: RunContext, refresh: bool = False) -> str:
    """List the tables in the connected database with their columns, types and rough row counts.
    Call this FIRST whenever you need data from the database and don't already know the exact
    table and column names, then write the query with database_query.

    Args:
        refresh: true to re-read the structure (after the database changed); normally false.
    """
    async with action(context, "database.schema", "Read the database structure") as act:
        try:
            text = await asyncio.to_thread(database.schema_text, refresh)
        except database.DatabaseError as e:
            raise ToolError(str(e)) from e
        act["result"] = f"{text.count(chr(10)) + 1} tables"
    return text


@function_tool
async def database_query(context: RunContext, sql: str, question: str) -> str:
    """Run ONE read-only PostgreSQL SELECT and get the exact rows back. Use it for any report,
    update, total, count, list or lookup the user asks for from the database.

    How to write the query:
      - Only use table and column names you saw in database_tables. Never guess.
      - Do the maths in SQL (COUNT, SUM, AVG, GROUP BY, date filters) so the answer is exact;
        never estimate or calculate from a partial list yourself.
      - For "today", "yesterday", "this month" use CURRENT_DATE / date_trunc in SQL.
      - Select only the columns you need (no SELECT * on tables with personal data); add ORDER BY
        and a LIMIT for lists.
    How to answer: give the exact figures from the result, with their dates/units. Do not round,
    invent or "approximately" a number. If the result is empty, say there is no matching data.
    If the result says "first N rows only", refine the query instead of guessing the rest.

    Args:
        sql: A single SELECT (or WITH ... SELECT) statement for PostgreSQL.
        question: The user's request in a few words, e.g. "sales total for 5 Nov 2026".
    """
    async with action(context, "database.query", f"Database report: {question}", {"sql": sql}) as act:
        try:
            res = await asyncio.to_thread(database.run_query, sql)
        except database.DatabaseError as e:
            raise ToolError(f"{e} Fix the query and try again if it makes sense.") from e
        act["result"] = f"{res['row_count']} rows" + (" (truncated)" if res["truncated"] else "")
    return database.format_result(res)


TOOLS = [
    ("database.schema", database_tables),
    ("database.query", database_query),
]
