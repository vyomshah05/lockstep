"""dbview.py — browse the Lockstep Supabase database from the command line.

Read-only. Uses the same exec_sql RPC path as the pipeline (db.py).

Interactive (default):
    python dbview.py
        -> lists tables, then prompts. Type a table name to view it,
           `tables` to relist, `\\d <table>` to describe, a `select ...`
           query to run it, or `q` / `quit` to exit.

One-shot subcommands:
    python dbview.py tables                 # list tables + row counts
    python dbview.py show libraries         # show rows (default 20)
    python dbview.py show fn_pypi_requests --limit 50
    python dbview.py show library_tags --where "tag='csv parsing'"
    python dbview.py show libraries --cols library_id,version,tier
    python dbview.py describe libraries     # column names + types
    python dbview.py query "select ecosystem, count(*) from libraries group by 1"
"""
from __future__ import annotations

import argparse
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from tabulate import tabulate

import db

MAX_CELL = 48  # truncate wide cell values for readability


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _fmt_cell(col: str, val) -> str:
    if val is None:
        return ""
    if "embedding" in col.lower():
        return "<vec384>"
    s = str(val)
    s = s.replace("\n", " ").replace("\r", " ")
    if len(s) > MAX_CELL:
        s = s[: MAX_CELL - 1] + "…"
    return s


def render(rows: list[dict], title: str | None = None) -> None:
    if title:
        print(f"\n{title}")
    if not rows:
        print("  (no rows)")
        return
    headers = list(rows[0].keys())
    table = [[_fmt_cell(h, r.get(h)) for h in headers] for r in rows]
    print(tabulate(table, headers=headers, tablefmt="psql"))
    print(f"  {len(rows)} row(s)")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def list_tables() -> None:
    rows = db.exec_sql(
        """
        select relname as table, n_live_tup as approx_rows
        from pg_stat_user_tables
        where schemaname = 'public'
        order by relname
        """
    )
    render(rows, title="Tables in public schema (approx row counts):")


def describe(table: str) -> None:
    rows = db.exec_sql(
        f"""
        select column_name, data_type, is_nullable
        from information_schema.columns
        where table_schema='public' and table_name={db.q(table)}
        order by ordinal_position
        """
    )
    if not rows:
        print(f"  no such table: {table}")
        return
    render(rows, title=f"Columns of {table}:")


def show(table: str, limit: int = 20, where: str | None = None,
         cols: str | None = None) -> None:
    select = cols if cols else "*"
    sql = f"select {select} from public.{table}"
    if where:
        sql += f" where {where}"
    sql += f" limit {int(limit)}"
    try:
        rows = db.exec_sql(sql)
    except Exception as e:
        print(f"  error: {e}")
        return
    render(rows, title=f"{table} (limit {limit}{', where ' + where if where else ''}):")


def run_query(sql: str) -> None:
    try:
        rows = db.exec_sql(sql)
    except Exception as e:
        print(f"  error: {e}")
        return
    render(rows, title="query result:")


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------
def repl() -> None:
    print("Lockstep DB viewer (read-only). Type a table name to view it.")
    print("Commands: tables | \\d <table> | <select ...> | q")
    list_tables()
    known = {
        r["table"]
        for r in db.exec_sql(
            "select relname as table from pg_stat_user_tables where schemaname='public'"
        )
    }
    while True:
        try:
            line = input("\ndbview> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        low = line.lower()
        if low in ("q", "quit", "exit"):
            break
        if low == "tables":
            list_tables()
            continue
        if low.startswith("\\d ") or low.startswith("describe "):
            describe(line.split(None, 1)[1].strip())
            continue
        if low.startswith(("select ", "with ")):
            run_query(line)
            continue
        # otherwise treat the input as a table name (optionally "name N")
        parts = line.split()
        name = parts[0]
        limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20
        if name not in known and name not in (
            r["table"] for r in db.exec_sql(
                "select relname as table from pg_stat_user_tables where schemaname='public'"
            )
        ):
            print(f"  unknown table: {name}  (type 'tables' to list)")
            continue
        show(name, limit=limit)


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Browse the Lockstep Supabase DB.")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("tables", help="list tables + row counts")

    s = sub.add_parser("show", help="show rows of a table")
    s.add_argument("table")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--where", default=None)
    s.add_argument("--cols", default=None)

    d = sub.add_parser("describe", help="describe a table's columns")
    d.add_argument("table")

    qy = sub.add_parser("query", help="run an arbitrary SELECT")
    qy.add_argument("sql")

    args = ap.parse_args()

    if args.cmd is None:
        repl()
    elif args.cmd == "tables":
        list_tables()
    elif args.cmd == "show":
        show(args.table, args.limit, args.where, args.cols)
    elif args.cmd == "describe":
        describe(args.table)
    elif args.cmd == "query":
        run_query(args.sql)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
