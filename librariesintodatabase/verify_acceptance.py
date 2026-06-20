"""verify_acceptance.py — check the Lockstep corpus against the acceptance criteria.

    python verify_acceptance.py
"""
from __future__ import annotations

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import librariesintodatabase.db as db
import librariesintodatabase.extract_tags as et


def main() -> int:
    passed = True

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal passed
        passed = passed and ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))

    print("=" * 70)
    print("LOCKSTEP ACCEPTANCE CHECKS")
    print("=" * 70)

    total = db.exec_sql("select count(*) n from libraries")[0]["n"]
    check(">=150 registry rows", total >= 150, f"({total} libraries)")

    by_eco = db.exec_sql(
        "select ecosystem, count(*) n from libraries group by ecosystem order by 1"
    )
    print("       by ecosystem: " + ", ".join(f"{r['ecosystem']}={r['n']}" for r in by_eco))

    under = db.exec_sql(
        "select count(*) n from libraries where coalesce(array_length(tags,1),0) < 50"
    )[0]["n"]
    check("every library has >=50 tags", under == 0, f"({under} below 50)")

    # popular pypi function tables populated with non-null summaries
    pop = db.exec_sql(
        "select library_id, function_table from libraries "
        "where ecosystem='pypi' and tier='popular'"
    )
    empty, nosum = [], []
    for r in pop:
        ft = r["function_table"]
        n = db.function_row_count(ft)
        if n == 0:
            empty.append(r["library_id"])
        else:
            s = db.exec_sql(
                f"select count(*) n from public.{ft} where summary is not null"
            )[0]["n"]
            if s == 0:
                nosum.append(r["library_id"])
    check("popular pypi fn_* tables populated", len(empty) == 0,
          f"({len(pop)} libs; empty: {empty or 'none'})")
    check("popular pypi tables have non-null summaries", len(nosum) == 0,
          f"(no-summary: {nosum or 'none'})")

    # cross-library tag query returns multiple libraries
    xl = db.exec_sql(
        "select library_id from library_tags where tag = 'programming language'"
    )
    check("cross-library tag query returns >1 library", len(xl) > 1,
          f"('programming language' -> {len(xl)} libs)")

    # shared vocabulary
    ntags = db.exec_sql("select count(*) n from tags")[0]["n"]
    nlt = db.exec_sql("select count(*) n from library_tags")[0]["n"]
    print(f"       shared vocab: {ntags} distinct tags, {nlt} library_tags rows")

    # pgvector cosine search returns sensible neighbors
    qv = et.embed_text("parse and validate data schemas")
    if qv:
        vs = db.exec_sql(
            f"select library_id, round(similarity::numeric,3) s "
            f"from match_libraries({db.vec_lit(qv)}, 5)"
        )
        check("pgvector search returns neighbors", len(vs) > 0)
        print("       'parse and validate data schemas' ->")
        for r in vs:
            print(f"          {r['library_id']:<28} {r['s']}")
    else:
        check("pgvector search returns neighbors", False, "(model unavailable)")

    print("-" * 70)
    print(f"  RESULT: {'ALL CHECKS PASS' if passed else 'SOME CHECKS FAILED'}")
    print("=" * 70)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
