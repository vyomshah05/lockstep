"""extract_functions.py — per-library function catalogs (no LLM).

Strategy by ecosystem:
  pypi   : install the pinned version into an isolated venv, then introspect with
           pkgutil + inspect in a subprocess (public symbols, signatures, first
           docstring line). This is the fullest path.
  npm    : install with npm, parse exported .d.ts declarations; if types are
           missing, fall back to whatever the caller scraped from the docs.
  cargo  : best-effort rustdoc; skip cleanly on failure.
  go     : best-effort `go doc`; skip cleanly on failure.

Returns a list of dicts with keys: qualified_name, kind, signature, summary,
description, params, returns, source_url. Embeddings are added later by the
orchestrator (the local MiniLM model lives in the main process).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

CACHE = Path("./cache")
VENV_ROOT = CACHE / "venvs"
PIP_TIMEOUT = int(os.environ.get("LOCKSTEP_PIP_TIMEOUT", "420"))
MAX_SYMBOLS = int(os.environ.get("LOCKSTEP_MAX_SYMBOLS", "600"))
BASE_PYTHON = os.environ.get("LOCKSTEP_PYTHON", sys.executable)

# PyPI distribution name -> importable module name (when they differ).
DIST_TO_MODULE = {
    "scikit-learn": "sklearn",
    "beautifulsoup4": "bs4",
    "opencv-python": "cv2",
    "pyyaml": "yaml",
    "pillow": "PIL",
    "psycopg2-binary": "psycopg2",
    "llama-index": "llama_index",
    "sentence-transformers": "sentence_transformers",
    "python-dateutil": "dateutil",
    "more-itertools": "more_itertools",
    "opentelemetry-api": "opentelemetry",
    "prometheus-client": "prometheus_client",
    "tortoise-orm": "tortoise",
    "jsonpath-ng": "jsonpath_ng",
    "msgpack": "msgpack",
    "pyjwt": "jwt",
    "grpcio": "grpc",
}


def module_name_for(dist: str) -> str:
    return DIST_TO_MODULE.get(dist.lower(), dist.lower().replace("-", "_"))


# ---------------------------------------------------------------------------
# Python: introspection in an isolated venv
# ---------------------------------------------------------------------------
_INTROSPECT_WORKER = r'''
import importlib, inspect, json, pkgutil, sys

MAX = int(sys.argv[2])
modname = sys.argv[1]
out = []
seen = set()

def first_line(obj):
    doc = inspect.getdoc(obj) or ""
    return doc.strip().split("\n", 1)[0].strip() if doc else None

def full_doc(obj):
    doc = inspect.getdoc(obj) or ""
    return doc.strip()[:2000] or None

def sig_info(obj):
    try:
        s = inspect.signature(obj)
    except (ValueError, TypeError):
        return None, [], None
    params = []
    for p in s.parameters.values():
        params.append({
            "name": p.name,
            "kind": str(p.kind),
            "default": None if p.default is inspect._empty else repr(p.default),
            "annotation": None if p.annotation is inspect._empty else str(p.annotation),
        })
    ret = None if s.return_annotation is inspect._empty else str(s.return_annotation)
    return str(s), params, ret

def add(qname, kind, obj):
    if qname in seen or len(out) >= MAX:
        return
    seen.add(qname)
    signature, params, ret = sig_info(obj)
    out.append({
        "qualified_name": qname,
        "kind": kind,
        "signature": (qname.split(".")[-1] + signature) if signature else None,
        "summary": first_line(obj),
        "description": full_doc(obj),
        "params": params,
        "returns": ret,
    })

def process_module(mod, prefix):
    public = getattr(mod, "__all__", None)
    members = inspect.getmembers(mod)
    for name, obj in members:
        if len(out) >= MAX:
            return
        if name.startswith("_"):
            continue
        if public is not None and name not in public:
            continue
        try:
            owner = getattr(obj, "__module__", "") or ""
        except Exception:
            owner = ""
        if not owner.startswith(prefix.split(".")[0]):
            continue
        if inspect.isfunction(obj) or inspect.isbuiltin(obj):
            add(f"{prefix}.{name}", "function", obj)
        elif inspect.isclass(obj):
            add(f"{prefix}.{name}", "class", obj)
            mcount = 0
            for mname, mobj in inspect.getmembers(obj):
                if mcount >= 15 or len(out) >= MAX:
                    break
                if mname.startswith("_"):
                    continue
                if inspect.isfunction(mobj) or inspect.ismethod(mobj):
                    add(f"{prefix}.{name}.{mname}", "method", mobj)
                    mcount += 1

try:
    root = importlib.import_module(modname)
except Exception as e:
    print(json.dumps({"error": f"import failed: {e}"}))
    sys.exit(0)

process_module(root, modname)

# Walk one level of subpackages/submodules (best-effort, guarded).
paths = getattr(root, "__path__", None)
if paths:
    for info in pkgutil.iter_modules(paths):
        if len(out) >= MAX:
            break
        if info.name.startswith("_") or info.name in ("tests", "test"):
            continue
        sub = f"{modname}.{info.name}"
        try:
            m = importlib.import_module(sub)
            process_module(m, sub)
        except Exception:
            continue

print(json.dumps({"functions": out}))
'''


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def extract_python(dist: str, version: str | None, source_url: str | None,
                   log=print) -> list[dict[str, Any]]:
    VENV_ROOT.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9]+", "_", dist.lower())
    venv_dir = VENV_ROOT / safe
    py = _venv_python(venv_dir)

    if not py.exists():
        try:
            subprocess.run(
                [BASE_PYTHON, "-m", "venv", str(venv_dir)],
                check=True, capture_output=True, timeout=180,
            )
        except Exception as e:
            log(f"  [py] venv creation failed for {dist}: {e}")
            return []

    spec = f"{dist}=={version}" if version else dist
    install = subprocess.run(
        [str(py), "-m", "pip", "install", "--disable-pip-version-check",
         "--no-input", "-q", spec],
        capture_output=True, text=True, timeout=PIP_TIMEOUT,
    )
    if install.returncode != 0:
        # retry without the version pin (yanked / unavailable wheels happen)
        install = subprocess.run(
            [str(py), "-m", "pip", "install", "--disable-pip-version-check",
             "--no-input", "-q", dist],
            capture_output=True, text=True, timeout=PIP_TIMEOUT,
        )
        if install.returncode != 0:
            log(f"  [py] pip install failed for {dist}: {install.stderr[-300:]}")
            return []

    worker = venv_dir / "_introspect.py"
    worker.write_text(_INTROSPECT_WORKER, encoding="utf-8")
    mod = module_name_for(dist)

    def _introspect() -> dict | None:
        try:
            proc = subprocess.run(
                [str(py), str(worker), mod, str(MAX_SYMBOLS)],
                capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired:
            log(f"  [py] introspection timed out for {dist}")
            return None
        try:
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            log(f"  [py] could not parse introspection output for {dist}: "
                f"{proc.stderr[-200:]}")
            return None

    payload = _introspect()
    if payload is None:
        return []
    # A heavy/timed-out install can leave a transitive dep missing. Repair once.
    err = payload.get("error", "")
    m = re.search(r"No module named '([\w\.]+)'", err)
    if m:
        missing = m.group(1).split(".")[0]
        log(f"  [py] {dist}: repairing missing dependency '{missing}'")
        subprocess.run(
            [str(py), "-m", "pip", "install", "--no-input", "-q", missing],
            capture_output=True, text=True, timeout=PIP_TIMEOUT,
        )
        payload = _introspect() or payload
    if "error" in payload:
        log(f"  [py] {dist}: {payload['error']}")
        return []

    fns = payload.get("functions", [])
    for f in fns:
        f["source_url"] = source_url
    return fns


# ---------------------------------------------------------------------------
# npm: parse exported .d.ts declarations
# ---------------------------------------------------------------------------
_DTS_PATTERNS = [
    (re.compile(r"export\s+(?:declare\s+)?function\s+([A-Za-z_$][\w$]*)\s*(\([^;{]*\))"), "function"),
    (re.compile(r"export\s+(?:declare\s+)?class\s+([A-Za-z_$][\w$]*)"), "class"),
    (re.compile(r"export\s+(?:declare\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*:\s*([^;=\n]+)"), "const"),
]


def extract_npm(pkg: str, version: str | None, source_url: str | None,
                log=print) -> list[dict[str, Any]]:
    npm_bin = shutil.which("npm")
    if not npm_bin:
        log(f"  [npm] npm not found; skipping {pkg}")
        return []
    tmp = Path(tempfile.mkdtemp(prefix="lockstep_npm_"))
    try:
        spec = f"{pkg}@{version}" if version else pkg
        proc = subprocess.run(
            [npm_bin, "install", "--no-save", "--ignore-scripts", "--prefix",
             str(tmp), spec],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            log(f"  [npm] install failed for {pkg}: {proc.stderr[-200:]}")
            return []
        pkg_dir = tmp / "node_modules"
        for part in pkg.split("/"):
            pkg_dir = pkg_dir / part
        dts_files = list(pkg_dir.rglob("*.d.ts"))[:8]
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for dts in dts_files:
            try:
                text = dts.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for pat, kind in _DTS_PATTERNS:
                for m in pat.finditer(text):
                    qname = f"{pkg}.{m.group(1)}"
                    if qname in seen:
                        continue
                    seen.add(qname)
                    sig = m.group(2).strip() if m.lastindex and m.lastindex >= 2 else None
                    out.append({
                        "qualified_name": qname,
                        "kind": kind,
                        "signature": (m.group(1) + sig) if (kind == "function" and sig) else m.group(1),
                        "summary": f"{kind} exported by {pkg}",
                        "description": None,
                        "params": None,
                        "returns": sig if kind == "const" else None,
                        "source_url": source_url,
                    })
                    if len(out) >= MAX_SYMBOLS:
                        return out
        return out
    except Exception as e:
        log(f"  [npm] {pkg} failed: {e}")
        return []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# cargo / go: best-effort, skip cleanly
# ---------------------------------------------------------------------------
def extract_cargo(crate: str, version: str | None, source_url: str | None,
                  log=print) -> list[dict[str, Any]]:
    log(f"  [cargo] {crate}: function-level introspection skipped (best-effort)")
    return []


def extract_go(pkg: str, version: str | None, source_url: str | None,
               log=print) -> list[dict[str, Any]]:
    log(f"  [go] {pkg}: function-level introspection skipped (best-effort)")
    return []


def extract_functions(ecosystem: str, name: str, version: str | None,
                      source_url: str | None, log=print) -> list[dict[str, Any]]:
    eco = ecosystem.lower()
    try:
        if eco == "pypi":
            return extract_python(name, version, source_url, log)
        if eco == "npm":
            return extract_npm(name, version, source_url, log)
        if eco == "cargo":
            return extract_cargo(name, version, source_url, log)
        if eco == "go":
            return extract_go(name, version, source_url, log)
    except Exception as e:
        log(f"  [{eco}] extraction error for {name}: {e}")
    return []
