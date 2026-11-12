"""Read-only access to your PostgreSQL database, so JARVIS can answer "give me the report for X"
with the exact figures.

Connection:   JARVIS_DB_URL in .env, e.g. postgresql://jarvis_ro:secret@localhost:5432/mydb
Settings:     access.yaml -> capabilities.database (schemas, hidden columns, row limit, timeout)

Safety, in layers (use all of them):
  1. A read-only database login (see README: "Database reports"). This is the real protection.
  2. Every query runs in a READ ONLY transaction with a statement timeout, so INSERT/UPDATE/
     DELETE/DDL fail inside PostgreSQL even if the login could write.
  3. Only one SELECT / WITH statement per call; admin functions (pg_sleep, pg_read_file, dblink,
     COPY, ...) and hidden columns (passwords, tokens) are refused before anything is sent.
"""

from __future__ import annotations

import datetime as dt
import decimal
import re
import threading
import time
from typing import Any

from . import config
from .access import get_policy

_SCHEMA_TTL_S = 300
_schema_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()

DEFAULT_HIDDEN = ["password", "passwd", "password_hash", "token", "access_token", "refresh_token",
                  "secret", "api_key", "otp", "salt"]
# Never allowed in a query, whatever the database login could do.
_BLOCKED = re.compile(
    r"\b(pg_sleep\w*|pg_read_\w+|pg_ls_\w+|pg_stat_file|lo_\w+|dblink\w*|pg_terminate_backend|"
    r"pg_cancel_backend|pg_reload_conf|set_config|current_setting|copy|pg_shadow|pg_authid|"
    r"pg_user_mapping\w*|into)\b", re.I)


class DatabaseError(Exception):
    """A readable problem to tell the user (bad query, not configured, ...)."""


# ------------------------------------------------------------- settings --
def settings() -> dict[str, Any]:
    return get_policy().section("database")


def configured() -> bool:
    return bool(config.env("JARVIS_DB_URL"))


def _schemas() -> list[str]:
    return [str(s) for s in (settings().get("schemas") or ["public"])]


def _hidden_columns() -> set[str]:
    extra = settings().get("hide_columns")
    return {c.lower() for c in (DEFAULT_HIDDEN if extra is None else extra)}


def _max_rows() -> int:
    return max(1, min(int(settings().get("max_rows", 200)), 2000))


def _timeout_ms() -> int:
    return max(1, int(settings().get("timeout_seconds", 15))) * 1000


def _connect():
    import psycopg  # imported here so JARVIS still starts without the driver installed

    url = config.env("JARVIS_DB_URL")
    if not url:
        raise DatabaseError("No database is configured. Set JARVIS_DB_URL in jarvis_pro/.env.")
    try:
        return psycopg.connect(
            url, connect_timeout=10, autocommit=False,
            options=f"-c default_transaction_read_only=on -c statement_timeout={_timeout_ms()}",
        )
    except psycopg.OperationalError as e:
        # Never echo the URL (it holds the password).
        raise DatabaseError(f"Could not connect to the database: {str(e).splitlines()[0]}") from e


# --------------------------------------------------------------- schema --
def schema_text(refresh: bool = False) -> str:
    """Tables, their columns and types, and approximate row counts, for JARVIS to plan a query."""
    key = ",".join(_schemas())
    with _lock:
        hit = _schema_cache.get(key)
        if hit and not refresh and time.time() - hit[0] < _SCHEMA_TTL_S:
            return hit[1]
    hidden = _hidden_columns()
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.table_schema, c.table_name, c.column_name, c.data_type
            FROM information_schema.columns c
            JOIN information_schema.tables t
              ON t.table_schema = c.table_schema AND t.table_name = c.table_name
            WHERE c.table_schema = ANY(%s) AND t.table_type IN ('BASE TABLE', 'VIEW')
            ORDER BY c.table_schema, c.table_name, c.ordinal_position
            """,
            (_schemas(),),
        )
        cols = cur.fetchall()
        cur.execute(
            """
            SELECT n.nspname, c.relname, GREATEST(c.reltuples, 0)::bigint
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p')
            """,
            (_schemas(),),
        )
        counts = {(s, t): n for s, t, n in cur.fetchall()}
        conn.rollback()
    tables: dict[tuple[str, str], list[str]] = {}
    for schema, table, column, dtype in cols:
        if column.lower() in hidden:
            continue
        tables.setdefault((schema, table), []).append(f"{column} {dtype}")
    if not tables:
        text = f"No tables visible in schema(s) {', '.join(_schemas())} for this database login."
    else:
        lines = []
        for (schema, table), columns in tables.items():
            name = table if schema == "public" else f"{schema}.{table}"
            rows = counts.get((schema, table))
            lines.append(f"{name} (~{rows} rows): " + ", ".join(columns) if rows is not None
                         else f"{name} (view): " + ", ".join(columns))
        text = "\n".join(lines)
    with _lock:
        _schema_cache[key] = (time.time(), text)
    return text


# ---------------------------------------------------------------- query --
def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)


def check_sql(sql: str) -> str:
    """Return the cleaned single statement, or raise DatabaseError explaining why it's refused."""
    clean = _strip_comments(sql).strip().rstrip(";").strip()
    if not clean:
        raise DatabaseError("The query is empty.")
    # Ignore semicolons inside string literals when counting statements.
    no_strings = re.sub(r"'(?:[^']|'')*'", "''", clean)
    if ";" in no_strings:
        raise DatabaseError("Only one statement per query.")
    if not re.match(r"(?is)^\s*(select|with)\b", no_strings):
        raise DatabaseError("Only read-only SELECT (or WITH ... SELECT) queries are allowed.")
    blocked = _BLOCKED.search(no_strings)
    if blocked:
        raise DatabaseError(f"'{blocked.group(0)}' isn't allowed in a JARVIS query.")
    hidden = [c for c in _hidden_columns() if re.search(rf"\b{re.escape(c)}\b", no_strings, re.I)]
    if hidden:
        raise DatabaseError(f"Column(s) {', '.join(sorted(hidden))} are hidden from JARVIS.")
    return clean


def _cell(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat(sep=" ") if isinstance(v, dt.datetime) else v.isoformat()
    if isinstance(v, decimal.Decimal):
        return format(v, "f")  # exact, never rounded or in scientific notation
    if isinstance(v, (bytes, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    text = str(v).replace("\n", " ")
    return text if len(text) <= 300 else text[:300] + "…"


def run_query(sql: str) -> dict[str, Any]:
    """Run one read-only query. Returns columns, rows (as strings), row_count and truncated."""
    clean = check_sql(sql)
    limit = _max_rows()
    hidden = _hidden_columns()
    import psycopg

    with _connect() as conn, conn.cursor() as cur:
        try:
            cur.execute("SET TRANSACTION READ ONLY")  # first statement of psycopg's implicit transaction
            cur.execute(clean)  # type: ignore[arg-type]  (validated above; runs read-only)
            if cur.description is None:
                raise DatabaseError("That statement returned no rows.")
            columns = [d.name for d in cur.description]
            if any(c.lower() in hidden for c in columns):  # e.g. SELECT * on a users table
                raise DatabaseError("The result includes a hidden column (like a password or token). "
                                    "List the columns you need instead of using *.")
            rows = cur.fetchmany(limit + 1)
        except psycopg.errors.QueryCanceled as e:
            raise DatabaseError(f"The query took longer than {_timeout_ms() // 1000} s and was stopped.") from e
        except psycopg.errors.ReadOnlySqlTransaction as e:
            raise DatabaseError("That query tries to change data. JARVIS can only read.") from e
        except psycopg.Error as e:
            raise DatabaseError(f"The database said: {str(e).splitlines()[0]}") from e
        finally:
            conn.rollback()
    truncated = len(rows) > limit
    rows = rows[:limit]
    return {
        "columns": columns,
        "rows": [[_cell(v) for v in r] for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
    }


def format_result(res: dict[str, Any]) -> str:
    """Plain pipe-separated table: exact values, easy for the model to read back."""
    if not res["rows"]:
        return "The query returned no rows."
    lines = [" | ".join(res["columns"])]
    lines += [" | ".join(r) for r in res["rows"]]
    tail = (f"\n(first {res['row_count']} rows only; there are more. Add filters, GROUP BY or a LIMIT "
            "to get the exact figure you need.)" if res["truncated"] else f"\n({res['row_count']} rows)")
    return "\n".join(lines) + tail
