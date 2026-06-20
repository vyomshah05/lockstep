"""scrape.py — Lockstep documentation scraper orchestrator.

For each library in library_seed_list.yaml:
  resolve metadata from the registry -> scrape docs (cache raw) -> extract
  functions (introspection-first) -> extract 50 tags -> create per-library
  function table -> upsert registry row, function rows, tags.

Idempotent, concurrent (small worker pool), throttled, and tolerant of
per-library failure (logs and continues). Zero LLM API calls.

Usage:
  python scrape.py                      # full run
  python scrape.py --limit 5            # first 5 libraries
  python scrape.py --ecosystems pypi    # only pypi
  python scrape.py --only requests numpy
  python scrape.py --dry-run            # no DB writes (extraction smoke test)
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import traceback
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import db
import extract_functions as ef
import extract_tags as et

CACHE = Path("./cache")
RAW = CACHE / "raw"
USER_AGENT = "LockstepDocsScraper/1.0 (+https://github.com/lockstep; respectful)"
THROTTLE = float(__import__("os").environ.get("LOCKSTEP_THROTTLE", "0.5"))

_print_lock = threading.Lock()
_robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}
_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})

# go import-path resolution for the short seed names.
GO_MODULES = {
    "gin": "github.com/gin-gonic/gin",
    "echo": "github.com/labstack/echo/v4",
    "fiber": "github.com/gofiber/fiber/v2",
    "gorm": "gorm.io/gorm",
    "cobra": "github.com/spf13/cobra",
    "viper": "github.com/spf13/viper",
    "zap": "go.uber.org/zap",
    "testify": "github.com/stretchr/testify",
    "samber/lo": "github.com/samber/lo",
    "sourcegraph/conc": "github.com/sourcegraph/conc",
    "uber-go/fx": "go.uber.org/fx",
    "rs/zerolog": "github.com/rs/zerolog",
    "spf13/afero": "github.com/spf13/afero",
}


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _humanize_name(qualified_name: str) -> str:
    """'pkg.addDays' / 'pkg.parse_iso' -> 'add days' / 'parse iso'."""
    import re
    base = qualified_name.split(".")[-1]
    base = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", base)  # split camelCase
    return base.replace("_", " ").strip().lower()


# ---------------------------------------------------------------------------
# Polite fetching with on-disk cache + robots.txt
# ---------------------------------------------------------------------------
def _robots_ok(url: str) -> bool:
    try:
        parts = urlparse(url)
        base = f"{parts.scheme}://{parts.netloc}"
        rp = _robots_cache.get(base)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(urljoin(base, "/robots.txt"))
            try:
                rp.read()
            except Exception:
                rp = None  # robots unreachable -> allow
            _robots_cache[base] = rp  # type: ignore
        if rp is None:
            return True
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def fetch(url: str, cache_key: str | None = None, as_text: bool = True) -> str | None:
    if not url:
        return None
    RAW.mkdir(parents=True, exist_ok=True)
    if cache_key:
        cached = RAW / cache_key
        if cached.exists():
            return cached.read_text(encoding="utf-8", errors="ignore")
    if not _robots_ok(url):
        log(f"  robots.txt disallows {url}")
        return None
    try:
        time.sleep(THROTTLE)
        resp = _session.get(url, timeout=30)
        if resp.status_code != 200:
            return None
        text = resp.text
        if cache_key:
            (RAW / cache_key).write_text(text, encoding="utf-8", errors="ignore")
        return text
    except Exception:
        return None


def extract_main_text(html: str | None) -> str:
    if not html:
        return ""
    try:
        import trafilatura
        out = trafilatura.extract(html, include_comments=False, include_tables=False)
        if out:
            return out
    except Exception:
        pass
    # crude fallback: strip tags
    import re
    return re.sub(r"<[^>]+>", " ", html)[:8000]


def find_llms_txt(docs_url: str | None, homepage: str | None, key: str) -> str:
    """Prefer a site's llms-full.txt / llms.txt when present."""
    for base in filter(None, [docs_url, homepage]):
        parts = urlparse(base)
        root = f"{parts.scheme}://{parts.netloc}"
        for fname in ("llms-full.txt", "llms.txt"):
            txt = fetch(urljoin(root + "/", fname), f"{key}.{fname}")
            if txt and len(txt) > 200 and "<html" not in txt[:200].lower():
                return txt
    return ""


# ---------------------------------------------------------------------------
# Registry metadata resolution
# ---------------------------------------------------------------------------
def meta_pypi(name: str) -> dict:
    try:
        r = _session.get(f"https://pypi.org/pypi/{name}/json", timeout=30)
        if r.status_code != 200:
            return {}
        info = r.json().get("info", {})
        urls = info.get("project_urls") or {}
        docs = None
        for k, v in urls.items():
            if "doc" in k.lower():
                docs = v
                break
        return {
            "version": info.get("version"),
            "summary": info.get("summary") or "",
            "homepage": info.get("home_page") or urls.get("Homepage"),
            "docs_url": docs or info.get("home_page") or urls.get("Homepage"),
            "readme": (info.get("description") or "")[:6000],
            "classifiers": info.get("classifiers") or [],
            "keywords": _split_keywords(info.get("keywords")),
        }
    except Exception:
        return {}


def meta_npm(name: str) -> dict:
    try:
        r = _session.get(f"https://registry.npmjs.org/{name}", timeout=30)
        if r.status_code != 200:
            return {}
        data = r.json()
        latest = (data.get("dist-tags") or {}).get("latest")
        ver = data.get("versions", {}).get(latest, {}) if latest else {}
        homepage = ver.get("homepage") or data.get("homepage")
        readme = (data.get("readme") or "")
        # npm often serves an empty readme for popular packages; fall back to the
        # repository's raw README (richest corpus, robots-allowed on GitHub).
        if len(readme) < 500:
            gh = github_readme(ver.get("repository") or data.get("repository"), name)
            if gh:
                readme = gh
        return {
            "version": latest,
            "summary": ver.get("description") or data.get("description") or "",
            "homepage": homepage,
            "docs_url": homepage,
            "readme": readme[:8000],
            "classifiers": [],
            "keywords": ver.get("keywords") or data.get("keywords") or [],
        }
    except Exception:
        return {}


def github_readme(repository, name: str) -> str:
    """Fetch a repo's raw README from GitHub given a package 'repository' field."""
    import re
    url = repository.get("url") if isinstance(repository, dict) else repository
    if not url:
        return ""
    m = re.search(r"github\.com[:/]+([^/]+)/([^/.#]+)", url)
    if not m:
        return ""
    owner, repo = m.group(1), m.group(2)
    key = db.sanitize_table_name("npm", name)
    for fname in ("README.md", "readme.md", "Readme.md", "README.markdown"):
        raw = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{fname}"
        txt = fetch(raw, f"{key}.gh.md")
        if txt and len(txt) > 200:
            return txt
    return ""


def meta_cargo(name: str) -> dict:
    try:
        r = _session.get(f"https://crates.io/api/v1/crates/{name}", timeout=30)
        if r.status_code != 200:
            return {}
        data = r.json()
        crate = data.get("crate", {})
        return {
            "version": crate.get("max_stable_version") or crate.get("newest_version"),
            "summary": crate.get("description") or "",
            "homepage": crate.get("homepage") or crate.get("repository"),
            "docs_url": crate.get("documentation") or f"https://docs.rs/{name}",
            "readme": "",
            "classifiers": [],
            "keywords": crate.get("keywords") or [],
        }
    except Exception:
        return {}


def meta_go(name: str) -> dict:
    module = GO_MODULES.get(name, name if "." in name else f"github.com/{name}")
    docs_url = f"https://pkg.go.dev/{module}"
    version = None
    try:
        r = _session.get(
            f"https://api.deps.dev/v3/systems/go/packages/{requests.utils.quote(module, safe='')}",
            timeout=30,
        )
        if r.status_code == 200:
            versions = r.json().get("versions", [])
            if versions:
                version = versions[-1].get("versionKey", {}).get("version")
    except Exception:
        pass
    return {
        "version": version,
        "summary": "",
        "homepage": f"https://{module}" if "." in module else docs_url,
        "docs_url": docs_url,
        "readme": "",
        "classifiers": [],
        "keywords": [],
        "module": module,
    }


def _split_keywords(kw) -> list[str]:
    if not kw:
        return []
    if isinstance(kw, list):
        return [str(k).strip() for k in kw if str(k).strip()]
    import re
    return [k.strip() for k in re.split(r"[,\s]+", str(kw)) if k.strip()]


META_RESOLVERS = {
    "pypi": meta_pypi,
    "npm": meta_npm,
    "cargo": meta_cargo,
    "go": meta_go,
}


# ---------------------------------------------------------------------------
# Per-library pipeline
# ---------------------------------------------------------------------------
def process_library(ecosystem: str, name: str, tier: str, dry_run: bool) -> dict:
    library_id = f"{ecosystem}:{name}"
    key = db.sanitize_table_name(ecosystem, name)
    result = {"library_id": library_id, "ok": False, "functions": 0, "tags": 0, "error": None}
    try:
        meta = META_RESOLVERS[ecosystem](name)
        if not meta:
            raise RuntimeError("registry metadata not found")

        docs_url = meta.get("docs_url")
        homepage = meta.get("homepage")

        # Docs: prefer llms.txt, else scrape + extract main text. Cache raw.
        readme = meta.get("readme") or ""
        llms = find_llms_txt(docs_url, homepage, key)
        if llms:
            readme = (readme + "\n" + llms)[:8000]
        elif not readme and docs_url:
            html = fetch(docs_url, f"{key}.html")
            readme = extract_main_text(html)[:8000]
        # Enrich a thin corpus from the docs page so tag extraction can reach 50.
        if not llms and len(readme) < 1200 and docs_url:
            extra = extract_main_text(fetch(docs_url, f"{key}.html"))
            if extra:
                readme = (readme + "\n" + extra)[:8000]

        # Functions (introspection-first).
        functions = ef.extract_functions(
            ecosystem, name, meta.get("version"), docs_url, log=log
        )
        fn_summaries = [f.get("summary") for f in functions if f.get("summary")]
        # Humanized function names ("addDays" -> "add days") are strong use-case
        # signal, especially for npm where .d.ts summaries are generic.
        fn_summaries += [_humanize_name(f["qualified_name"]) for f in functions]

        # 50 tags.
        tag_scores = et.extract_tags(
            name=name,
            ecosystem=ecosystem,
            summary=meta.get("summary", ""),
            readme=readme,
            function_summaries=fn_summaries,
            classifiers=meta.get("classifiers", []),
            keywords=meta.get("keywords", []),
        )
        tags = [t for t, _ in tag_scores]

        # Embeddings (local MiniLM, API-free).
        lib_text = " . ".join(filter(None, [meta.get("summary", "")] + tags[:15]
                                     + fn_summaries[:10]))
        lib_embedding = et.embed_text(lib_text)
        if functions:
            fn_texts = [
                f"{f['qualified_name']}: {f.get('summary') or ''}" for f in functions
            ]
            fn_embeddings = et.embed_texts(fn_texts)
            for f, emb in zip(functions, fn_embeddings):
                f["embedding"] = emb

        result["functions"] = len(functions)
        result["tags"] = len(tags)

        if dry_run:
            result["ok"] = True
            log(f"[dry] {library_id}: {len(functions)} fns, {len(tags)} tags "
                f"(e.g. {tags[:5]})")
            return result

        # Persist (idempotent upserts).
        db.upsert_library({
            "library_id": library_id,
            "ecosystem": ecosystem,
            "name": name,
            "version": meta.get("version"),
            "summary": meta.get("summary"),
            "homepage": homepage,
            "docs_url": docs_url,
            "tier": tier,
            "tags": tags,
            "function_table": key,
            "embedding": lib_embedding,
        })
        db.create_function_table(key, tags)
        if functions:
            db.upsert_functions(key, functions)
        db.upsert_tags(library_id, tag_scores)
        db.set_library_tags_array(library_id, tags)
        # Refresh the table COMMENT now that final tags are known.
        db.create_function_table(key, tags)

        result["ok"] = True
        log(f"[ok] {library_id}: {len(functions)} fns, {len(tags)} tags")
        return result
    except Exception as e:
        result["error"] = str(e)
        log(f"[FAIL] {library_id}: {e}")
        if "--trace" in sys.argv:
            traceback.print_exc()
        return result


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def load_seed(path: str) -> list[tuple[str, str, str]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    libs: list[tuple[str, str, str]] = []
    for ecosystem, tiers in data.items():
        for tier, names in (tiers or {}).items():
            for name in names:
                libs.append((ecosystem, str(name), tier))
    return libs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", default="library_seed_list.yaml")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--ecosystems", nargs="*", default=None)
    ap.add_argument("--tier", default=None, choices=[None, "popular", "niche"])
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--trace", action="store_true")
    args = ap.parse_args()

    libs = load_seed(args.seed)
    if args.ecosystems:
        libs = [l for l in libs if l[0] in args.ecosystems]
    if args.tier:
        libs = [l for l in libs if l[2] == args.tier]
    if args.only:
        wanted = set(args.only)
        libs = [l for l in libs if l[1] in wanted]
    if args.limit:
        libs = libs[: args.limit]

    log(f"Lockstep scraper: {len(libs)} libraries, {args.workers} workers, "
        f"dry_run={args.dry_run}")

    if not args.dry_run:
        db.ensure_ready()
        log("Schema ready (exec_sql + core tables verified).")

    # Warm the local model once before fanning out.
    et.get_model()

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_library, eco, name, tier, args.dry_run): (eco, name)
            for eco, name, tier in libs
        }
        for fut in as_completed(futures):
            results.append(fut.result())

    print_summary(results, args.dry_run)
    return 0


def print_summary(results: list[dict], dry_run: bool) -> None:
    ok = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    print("\n" + "=" * 72)
    print("COVERAGE SUMMARY (zero LLM API calls)")
    print("=" * 72)
    for r in sorted(results, key=lambda x: x["library_id"]):
        status = "ok " if r["ok"] else "ERR"
        extra = f"  <- {r['error']}" if r["error"] else ""
        print(f"  [{status}] {r['library_id']:<32} fns={r['functions']:>4}  "
              f"tags={r['tags']:>3}{extra}")
    print("-" * 72)
    print(f"  libraries ok: {len(ok)} / {len(results)}   failed: {len(failed)}")
    total_fns = sum(r["functions"] for r in results)
    libs_50_tags = sum(1 for r in results if r["tags"] >= 50)
    print(f"  total functions: {total_fns}   libraries with >=50 tags: {libs_50_tags}")

    if not dry_run:
        try:
            rows = db.coverage_summary()
            print(f"  registry rows in DB: {len(rows)}")
        except Exception as e:
            print(f"  (could not read DB coverage: {e})")
    print("=" * 72)


if __name__ == "__main__":
    raise SystemExit(main())
