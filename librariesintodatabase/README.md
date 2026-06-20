# Lockstep documentation scraper → Supabase

Ingests a large set of libraries into Supabase (Postgres + pgvector) with a
per-library function catalog and **50 use-case tags per library**. The whole
pipeline is **API-free**: keyword extraction and embeddings run on a local
`all-MiniLM-L6-v2` model. No LLM API calls anywhere.

## What it builds

- **`libraries`** — registry, one row per library: `library_id` (`{ecosystem}:{name}`),
  `ecosystem`, `name`, `version`, `summary`, `homepage`, `docs_url`, `tier`,
  `tags text[]` (the 50 tags, mirrored for querying), `function_table`,
  `embedding vector(384)`, `scraped_at`.
- **`tags`** — global tag vocabulary (`tag` pk, `category`).
- **`library_tags`** — join table `(library_id, tag, score)` for cross-library
  selection.
- **`fn_{ecosystem}_{name}`** — one function table per library: `qualified_name`,
  `kind` (function|class|method), `signature`, `summary`, `description`,
  `params jsonb`, `returns`, `source_url`, `embedding vector(384)`. The table's
  `COMMENT` holds the library's 50 tags as JSON.
- HNSW cosine indexes on every `embedding` column.

## Connection model (service-key only)

The pipeline uses `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` and drives **all** DDL
and DML through one RPC, `exec_sql(query text)`, defined in `schema.sql`. The
service key can't run arbitrary DDL through PostgREST, so this RPC is the bridge.

> Set `SUPABASE_DB_URL` instead if you'd rather use a direct psycopg2 connection
> (faster for bulk loads). `db.py` auto-detects it.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env        # fill in SUPABASE_URL + SUPABASE_SERVICE_KEY
```

**One-time bootstrap** — paste `schema.sql` into the Supabase dashboard
(SQL Editor → New query → Run). This enables `pgvector`, installs the `exec_sql`
RPC, and creates the core tables. Re-running it is safe (idempotent).

## Run

```bash
# smoke test (no DB writes): verify extraction on a handful of libs
python scrape.py --dry-run --limit 5

# full run (long-running — start it early)
python scrape.py

# scope it
python scrape.py --ecosystems pypi --tier popular
python scrape.py --only requests numpy pandas
python scrape.py --workers 6
```

Per-library failures (import errors, unreachable docs) are logged and skipped —
a partial corpus is fine, a crashed run is not. Re-running upserts in place and
produces **no duplicates**.

## Cross-library queries (what `recommend_library` calls)

```sql
-- libraries carrying a tag
select library_id from library_tags where tag = 'csv parsing';
select * from libraries_by_tag('csv parsing');

-- nearest libraries to a task string (compute the 384-dim query vector with the
-- same local MiniLM model, then:)
select * from match_libraries('[...384 floats...]'::vector, 10);

-- a library's tags + function count
select tags, function_table from libraries where library_id = 'pypi:pandas';
```

## How the 50 tags are built (no LLM)

Per-library corpus = registry summary + keywords + classifiers (weighted most,
they describe purpose) + README intro + concatenated function-summary lines →
**KeyBERT** (1–3 grams, MMR for diversity) over local MiniLM → normalize
(lowercase, lemmatize, drop stopwords + package/ecosystem name tokens) → dedupe
by stem → merge near-duplicates by MiniLM cosine → score, keep top 50 →
backfill from classifiers/keywords/corpus n-grams if short. **YAKE** is the
zero-model fallback. Tags are upserted into `tags` + `library_tags`, mirrored to
`libraries.tags`, and set as the function table's `COMMENT`.

## Function extraction

- **pypi** — install the pinned version into an isolated venv, then introspect
  with `pkgutil` + `inspect` in a subprocess (public symbols, signatures, first
  docstring line). Fullest coverage.
- **npm** — `npm install` then parse exported `.d.ts` declarations; falls back to
  scraped docs text when types are absent.
- **cargo / go** — best-effort; skipped cleanly (registry row + tags still land).

## Files

| File | Purpose |
|------|---------|
| `scrape.py` | orchestrator over `library_seed_list.yaml` |
| `extract_functions.py` | python introspection + `.d.ts`/doc fallbacks |
| `extract_tags.py` | KeyBERT/YAKE + normalization + dedupe + embeddings |
| `db.py` | Supabase RPC/psycopg, DDL, table-name sanitization, upserts |
| `schema.sql` | `libraries` / `tags` / `library_tags` + `exec_sql` + helpers |
| `dbview.py` | CLI table browser (`python dbview.py` interactive, or `tables`/`show`/`describe`/`query`) |
| `verify_acceptance.py` | runs the acceptance checks against the live DB |
| `.env.example` | `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `BROWSERBASE_API_KEY` |

## Browsing the database

```bash
python dbview.py                          # interactive: lists tables, then prompt
python dbview.py tables                    # list tables + row counts
python dbview.py show libraries --cols library_id,version,tier --limit 20
python dbview.py show library_tags --where "tag='csv parsing'"
python dbview.py query "select ecosystem, count(*) from libraries group by 1"
python verify_acceptance.py                # full acceptance report
```

## Coverage summary

`scrape.py` prints a per-library coverage table at the end (functions + tag
counts, ok/failed, total registry rows). Acceptance targets: ≥150 registry rows,
each popular pypi library with a populated `fn_*` table (non-null summaries),
every library with ≥50 tags, working cross-library tag + vector queries, and no
duplicates on re-run.
