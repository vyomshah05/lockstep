-- ============================================================================
-- Lockstep documentation scraper — Supabase schema (idempotent)
-- ============================================================================
-- This file is the ONE-TIME bootstrap. Paste it into the Supabase SQL editor
-- (Dashboard -> SQL Editor -> New query -> Run) before running scrape.py.
--
-- Why a bootstrap: the pipeline connects with the service-role key only (no
-- direct Postgres connection). The service key cannot run arbitrary DDL through
-- PostgREST, so we install a single SECURITY DEFINER RPC, exec_sql(), that runs
-- arbitrary SQL. After this paste, the entire pipeline (per-library CREATE TABLE,
-- COMMENT, inserts, upserts) is driven purely via the service key calling
-- exec_sql() over HTTPS. Re-running this file is safe (everything is IF NOT
-- EXISTS / OR REPLACE).
-- ============================================================================

create extension if not exists vector;

-- ----------------------------------------------------------------------------
-- exec_sql: run arbitrary SQL and return rows as JSON.
--   select exec_sql('select 1 as x')  ->  [{"x": 1}]
-- DDL / statements with no result set return [].
-- SECURITY DEFINER so the service role can run DDL. Lock down by only granting
-- to service_role (PostgREST authenticates the service key as service_role).
-- ----------------------------------------------------------------------------
create or replace function public.exec_sql(query text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  result jsonb;
begin
  execute 'select coalesce(jsonb_agg(_exec_row), ''[]''::jsonb) from ('
          || query || ') _exec_row'
    into result;
  return result;
exception
  when others then
    -- Non-SELECT statements (DDL/DML without RETURNING) can't be wrapped in a
    -- subquery; run them directly and return an empty result set.
    execute query;
    return '[]'::jsonb;
end;
$$;

revoke all on function public.exec_sql(text) from public;
grant execute on function public.exec_sql(text) to service_role;

-- ----------------------------------------------------------------------------
-- Core registry tables
-- ----------------------------------------------------------------------------

-- libraries: one row per library (the registry).
create table if not exists public.libraries (
  library_id     text primary key,           -- {ecosystem}:{name}
  ecosystem      text not null,              -- pypi | npm | cargo | go
  name           text not null,
  version        text,
  summary        text,
  homepage       text,
  docs_url       text,
  tier           text,                       -- popular | niche
  tags           text[] default '{}',        -- the 50 tags, mirrored for querying
  function_table text,                       -- name of the per-library fn_* table
  embedding      vector(384),
  scraped_at     timestamptz default now()
);

-- tags: global tag vocabulary, shared across libraries.
create table if not exists public.tags (
  tag      text primary key,
  category text
);

-- library_tags: join table for cross-library selection.
create table if not exists public.library_tags (
  library_id text not null references public.libraries(library_id) on delete cascade,
  tag        text not null references public.tags(tag) on delete cascade,
  score      real,
  primary key (library_id, tag)
);

-- Indexes for cross-library queries.
create index if not exists library_tags_tag_idx on public.library_tags (tag);
create index if not exists libraries_ecosystem_idx on public.libraries (ecosystem);
create index if not exists libraries_tier_idx on public.libraries (tier);
create index if not exists libraries_tags_gin on public.libraries using gin (tags);

-- pgvector cosine index on the registry embedding (HNSW).
create index if not exists libraries_embedding_idx
  on public.libraries using hnsw (embedding vector_cosine_ops);

-- ----------------------------------------------------------------------------
-- Fallback normalized table for function rows.
-- Used only if the per-library-table approach ever exceeds Postgres limits;
-- the pipeline writes to per-library fn_* tables by default. Kept here so the
-- cross-library shape is always available.
-- ----------------------------------------------------------------------------
create table if not exists public.functions (
  id             bigserial primary key,
  library_id     text not null references public.libraries(library_id) on delete cascade,
  qualified_name text not null,
  kind           text,
  signature      text,
  summary        text,
  description    text,
  params         jsonb,
  returns        text,
  source_url     text,
  embedding      vector(384),
  unique (library_id, qualified_name)
);
create index if not exists functions_library_idx on public.functions (library_id);
create index if not exists functions_embedding_idx
  on public.functions using hnsw (embedding vector_cosine_ops);

-- ----------------------------------------------------------------------------
-- Helper functions for the recommend_library use case.
-- ----------------------------------------------------------------------------

-- Cross-library tag lookup: libraries carrying a given tag, best score first.
create or replace function public.libraries_by_tag(p_tag text)
returns table (library_id text, score real)
language sql stable
as $$
  select lt.library_id, lt.score
  from public.library_tags lt
  where lt.tag = p_tag
  order by lt.score desc nulls last;
$$;

-- Vector search over library embeddings (cosine). Pass a 384-dim query vector.
create or replace function public.match_libraries(
  query_embedding vector(384),
  match_count int default 10
)
returns table (library_id text, name text, summary text, similarity real)
language sql stable
as $$
  select l.library_id, l.name, l.summary,
         (1 - (l.embedding <=> query_embedding))::real as similarity
  from public.libraries l
  where l.embedding is not null
  order by l.embedding <=> query_embedding
  limit match_count;
$$;
