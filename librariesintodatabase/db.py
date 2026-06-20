"""db.py — Supabase access layer (service-key only).

Everything goes through a single SECURITY DEFINER RPC, ``exec_sql(query text)``,
which runs arbitrary SQL and returns rows as JSON. This is the only way to do
per-library DDL (CREATE TABLE / COMMENT / HNSW indexes) with the service-role
key and no direct Postgres connection.

Bootstrap once: paste ``schema.sql`` into the Supabase SQL editor. After that,
this module drives the whole pipeline over HTTPS.

If ``SUPABASE_DB_URL`` is set, a direct psycopg2 path is used instead (faster
for bulk loads); the SQL we generate is identical either way.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Iterable, Sequence

import requests

try:  # optional faster path
    import psycopg2  # type: ignore
    import psycopg2.extras  # type: ignore
    _HAS_PSYCOPG = True
except Exception:  # pragma: no cover
    _HAS_PSYCOPG = False


SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SUPABASE_DB_URL = os.environ.get("SUPABASE_DB_URL", "")

EMBED_DIM = 384

# Lazily-opened psycopg2 connection for the direct path.
_conn = None


# ---------------------------------------------------------------------------
# Connectivity
# ---------------------------------------------------------------------------
def _rpc_headers() -> dict[str, str]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set (see .env.example)."
        )
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def _use_psycopg() -> bool:
    return bool(SUPABASE_DB_URL) and _HAS_PSYCOPG


def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(SUPABASE_DB_URL)
        _conn.autocommit = True
    return _conn


def exec_sql(query: str, retries: int = 6) -> list[dict[str, Any]]:
    """Run SQL. Returns a list of row dicts (empty for DDL / no result set)."""
    if _use_psycopg():
        conn = _get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query)
            if cur.description:
                return [dict(r) for r in cur.fetchall()]
            return []

    url = f"{SUPABASE_URL}/rest/v1/rpc/exec_sql"
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.post(
                url, headers=_rpc_headers(), json={"query": query}, timeout=120
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"exec_sql HTTP {resp.status_code}: {resp.text}")
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:  # network blips / DNS drops -> retry w/ backoff
            last_err = e
            time.sleep(min(30.0, 2.0 * (attempt + 1) ** 2))
    raise RuntimeError(f"exec_sql failed after {retries} tries: {last_err}")


def ensure_ready() -> None:
    """Verify the bootstrap (exec_sql + core tables) is in place."""
    try:
        exec_sql("select 1 as ok")
    except Exception as e:
        raise RuntimeError(
            "Cannot reach exec_sql RPC. Run the one-time bootstrap first:\n"
            "  open Supabase Dashboard -> SQL Editor -> paste schema.sql -> Run.\n"
            f"Underlying error: {e}"
        )
    # Make sure core tables exist (idempotent safety net mirroring schema.sql).
    for stmt in _CORE_DDL:
        exec_sql(stmt)


# ---------------------------------------------------------------------------
# SQL literal builders (we construct full statements, so escape carefully)
# ---------------------------------------------------------------------------
def q(val: Any) -> str:
    """Quote a scalar as a Postgres literal."""
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return repr(val)
    s = str(val).replace("'", "''")
    return f"'{s}'"


def array_lit(items: Sequence[Any] | None) -> str:
    """Build a text[] literal."""
    if not items:
        return "'{}'::text[]"
    inner = ",".join(q(str(x)) for x in items)
    return f"array[{inner}]::text[]"


def jsonb_lit(obj: Any) -> str:
    if obj is None:
        return "NULL"
    return f"{q(json.dumps(obj))}::jsonb"


def vec_lit(embedding: Sequence[float] | None) -> str:
    if embedding is None:
        return "NULL"
    body = "[" + ",".join(f"{float(x):.6f}" for x in embedding) + "]"
    return f"{q(body)}::vector"


# ---------------------------------------------------------------------------
# Table-name sanitization
# ---------------------------------------------------------------------------
def sanitize_table_name(ecosystem: str, name: str) -> str:
    """fn_{ecosystem}_{sanitized}; @scope/pkg -> scope_pkg; <=63 chars."""
    n = name.lower()
    n = n.replace("@", "").replace("/", "_")  # @scope/pkg -> scope_pkg
    n = re.sub(r"[^a-z0-9]+", "_", n).strip("_")
    table = f"fn_{ecosystem.lower()}_{n}"
    return table[:63].rstrip("_")


# ---------------------------------------------------------------------------
# DDL / upserts
# ---------------------------------------------------------------------------
_CORE_DDL = [
    "create extension if not exists vector",
    """create table if not exists public.libraries (
        library_id text primary key, ecosystem text not null, name text not null,
        version text, summary text, homepage text, docs_url text, tier text,
        tags text[] default '{}', function_table text, embedding vector(384),
        scraped_at timestamptz default now())""",
    "create table if not exists public.tags (tag text primary key, category text)",
    """create table if not exists public.library_tags (
        library_id text not null references public.libraries(library_id) on delete cascade,
        tag text not null references public.tags(tag) on delete cascade,
        score real, primary key (library_id, tag))""",
    "create index if not exists library_tags_tag_idx on public.library_tags (tag)",
    "create index if not exists libraries_tags_gin on public.libraries using gin (tags)",
    """create index if not exists libraries_embedding_idx
        on public.libraries using hnsw (embedding vector_cosine_ops)""",
]


def create_function_table(table: str, tags: list[str]) -> None:
    """Create a per-library fn_* table (idempotent), HNSW index, tags COMMENT."""
    exec_sql(
        f"""create table if not exists public.{table} (
            id bigserial primary key,
            qualified_name text unique,
            kind text,
            signature text,
            summary text,
            description text,
            params jsonb,
            returns text,
            source_url text,
            embedding vector(384))"""
    )
    exec_sql(
        f"""create index if not exists {table}_embedding_idx
            on public.{table} using hnsw (embedding vector_cosine_ops)"""
    )
    # "the table has the tags" — store the 50 tags as JSON in the table COMMENT.
    comment = json.dumps({"tags": tags})
    exec_sql(f"comment on table public.{table} is {q(comment)}")


def upsert_library(row: dict[str, Any]) -> None:
    cols = [
        "library_id", "ecosystem", "name", "version", "summary", "homepage",
        "docs_url", "tier", "tags", "function_table", "embedding", "scraped_at",
    ]
    vals = {
        "library_id": q(row["library_id"]),
        "ecosystem": q(row["ecosystem"]),
        "name": q(row["name"]),
        "version": q(row.get("version")),
        "summary": q(row.get("summary")),
        "homepage": q(row.get("homepage")),
        "docs_url": q(row.get("docs_url")),
        "tier": q(row.get("tier")),
        "tags": array_lit(row.get("tags")),
        "function_table": q(row.get("function_table")),
        "embedding": vec_lit(row.get("embedding")),
        "scraped_at": "now()",
    }
    set_clause = ", ".join(
        f"{c}=excluded.{c}" for c in cols if c != "library_id"
    )
    exec_sql(
        f"insert into public.libraries ({', '.join(cols)}) "
        f"values ({', '.join(vals[c] for c in cols)}) "
        f"on conflict (library_id) do update set {set_clause}"
    )


def upsert_functions(table: str, rows: Iterable[dict[str, Any]], batch: int = 50) -> int:
    rows = list(rows)
    if not rows:
        return 0
    cols = [
        "qualified_name", "kind", "signature", "summary", "description",
        "params", "returns", "source_url", "embedding",
    ]
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "qualified_name")
    total = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        values = []
        for r in chunk:
            values.append(
                "(" + ", ".join([
                    q(r["qualified_name"]),
                    q(r.get("kind")),
                    q(r.get("signature")),
                    q(r.get("summary")),
                    q(r.get("description")),
                    jsonb_lit(r.get("params")),
                    q(r.get("returns")),
                    q(r.get("source_url")),
                    vec_lit(r.get("embedding")),
                ]) + ")"
            )
        exec_sql(
            f"insert into public.{table} ({', '.join(cols)}) values "
            + ", ".join(values)
            + f" on conflict (qualified_name) do update set {set_clause}"
        )
        total += len(chunk)
    return total


def upsert_tags(library_id: str, tag_scores: list[tuple[str, float]]) -> None:
    """Upsert into tags + library_tags. Mirroring to libraries.tags and the
    table COMMENT is handled by the caller (it knows the table name)."""
    if not tag_scores:
        return
    tags = [t for t, _ in tag_scores]
    # global vocabulary
    tag_values = ", ".join(f"({q(t)})" for t in tags)
    exec_sql(
        f"insert into public.tags (tag) values {tag_values} "
        f"on conflict (tag) do nothing"
    )
    # join rows
    lt_values = ", ".join(
        f"({q(library_id)}, {q(t)}, {q(float(s))})" for t, s in tag_scores
    )
    exec_sql(
        f"insert into public.library_tags (library_id, tag, score) values {lt_values} "
        f"on conflict (library_id, tag) do update set score=excluded.score"
    )


def set_library_tags_array(library_id: str, tags: list[str]) -> None:
    exec_sql(
        f"update public.libraries set tags={array_lit(tags)} "
        f"where library_id={q(library_id)}"
    )


def coverage_summary() -> list[dict[str, Any]]:
    """Per-library function + tag counts for the final report."""
    return exec_sql(
        """
        select l.library_id, l.ecosystem, l.tier, l.function_table,
               coalesce(array_length(l.tags, 1), 0) as tag_count,
               (select count(*) from public.library_tags lt
                  where lt.library_id = l.library_id) as join_tag_count
        from public.libraries l
        order by l.ecosystem, l.tier, l.name
        """
    )


def function_row_count(table: str) -> int:
    try:
        res = exec_sql(f"select count(*) as n from public.{table}")
        return int(res[0]["n"]) if res else 0
    except Exception:
        return 0
