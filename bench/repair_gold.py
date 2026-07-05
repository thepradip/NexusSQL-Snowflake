"""
Repair broken gold SQL in the LargeSchemaEval question sets.

Root cause: the Spider-DB merge script prefixed table names with
"<source_db>_" using a naive string replace. That also rewrote *column*
references whose name equals a table name (sakila address.address,
baseball_1 salary.salary, hospital_1 Prescribes.Patient, ...) and missed
prefixing at least one table reference entirely (Ref_Company_Types).

Fix strategy — driven only by the SQLite error, applied on the AST
(sqlglot), never by string replace:
  * "no such column: [alias.]<prefix>_<name>"  → rename matching Column
    nodes back to <name> (table references are distinct AST nodes and are
    untouched).
  * "no such table: <name>"                    → rename matching Table
    nodes to "<source_db>_<name>" when that table exists in the DB.

Queries are re-executed after each fix; anything still failing after
MAX_PASSES is reported as unfixable and left unmodified. A query whose
referenced table does not exist in the merged DB under any prefix (the
merge dropped it — e.g. assets_maintenance Ref_Company_Types) is
unanswerable against this data and is REMOVED from the v2 set, recorded
under "removed" in the changelog. Gold queries that execute but return
empty results are reported as warnings, not modified.

Output per set N: questions_N_v2.json + gold_repair_changelog_N.json,
written next to the originals. Originals are never modified.

Usage:  python bench/repair_gold.py [--dir large_schema_eval_v2] [--sets 50 100 200]
"""

import argparse
import json
import re
import sqlite3
from pathlib import Path

import sqlglot
from sqlglot import exp

MAX_PASSES = 5

NO_COLUMN = re.compile(r"no such column: (?:\w+\.)?(\S+)")
NO_TABLE = re.compile(r"no such table: (\S+)")


def _rename_columns(sql: str, bad: str, good: str) -> str:
    tree = sqlglot.parse_one(sql, dialect="sqlite")
    for col in tree.find_all(exp.Column):
        if col.name.lower() == bad.lower():
            col.this.set("this", good)
    return tree.sql(dialect="sqlite")


def _rename_tables(sql: str, bad: str, good: str) -> str:
    tree = sqlglot.parse_one(sql, dialect="sqlite")
    for tbl in tree.find_all(exp.Table):
        if tbl.name.lower() == bad.lower():
            tbl.this.set("this", good)
    return tree.sql(dialect="sqlite")


def repair_one(conn, sql: str, source_db: str, db_tables: set[str]):
    """Return (fixed_sql, fixes, error). fixed_sql is None if unfixable."""
    fixes = []
    current = sql
    for _ in range(MAX_PASSES):
        try:
            conn.execute(current).fetchall()
            return current, fixes, None
        except Exception as e:
            msg = str(e)

        m = NO_COLUMN.search(msg)
        if m:
            bad = m.group(1)
            prefix = f"{source_db}_"
            if bad.lower().startswith(prefix.lower()):
                good = bad[len(prefix):]
                current = _rename_columns(current, bad, good)
                fixes.append(f"column {bad} -> {good}")
                continue
            return None, fixes, msg

        m = NO_TABLE.search(msg)
        if m:
            bad = m.group(1)
            candidate = f"{source_db}_{bad}"
            match = next((t for t in db_tables if t.lower() == candidate.lower()), None)
            if match:
                current = _rename_tables(current, bad, match)
                fixes.append(f"table {bad} -> {match}")
                continue
            # Table absent under any prefix: the merge never copied it,
            # so the question is unanswerable against this DB.
            if not any(t.lower().endswith(f"_{bad.lower()}") for t in db_tables):
                return None, fixes, f"UNANSWERABLE: table {bad} not in merged DB"
            return None, fixes, msg

        return None, fixes, msg
    return None, fixes, "exceeded MAX_PASSES"


def repair_set(eval_dir: Path, n: int) -> dict:
    qfile = eval_dir / f"questions_{n}.json"
    db = eval_dir / f"merged_{n}.sqlite"
    questions = json.loads(qfile.read_text())
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    db_tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    changelog, unfixable, removed, empty_warn = [], [], [], []
    kept = []
    already_ok = 0
    for i, q in enumerate(questions):
        fixed, fixes, err = repair_one(conn, q["gold_sql"], q["source_db"], db_tables)
        if fixed is None:
            record = {"index": i, "question": q["question"],
                      "gold_sql": q["gold_sql"], "error": err,
                      "partial_fixes": fixes}
            if err and err.startswith("UNANSWERABLE"):
                removed.append(record)
            else:
                unfixable.append(record)
                kept.append(q)
            continue
        if fixes:
            changelog.append({"index": i, "question": q["question"],
                              "old": q["gold_sql"], "new": fixed, "fixes": fixes})
            q["gold_sql"] = fixed
        else:
            already_ok += 1
        kept.append(q)
        rows = conn.execute(fixed).fetchall()
        if not rows or all(all(v is None for v in r) for r in rows):
            empty_warn.append({"index": i, "question": q["question"]})

    out_q = eval_dir / f"questions_{n}_v2.json"
    out_log = eval_dir / f"gold_repair_changelog_{n}.json"
    out_q.write_text(json.dumps(kept, indent=1))
    out_log.write_text(json.dumps({
        "source": qfile.name,
        "repaired": changelog,
        "removed_unanswerable": removed,
        "unfixable": unfixable,
        "empty_result_warnings": empty_warn,
    }, indent=1))

    summary = {
        "set": n, "total": len(questions), "kept": len(kept),
        "already_ok": already_ok, "repaired": len(changelog),
        "removed": len(removed), "unfixable": len(unfixable),
        "empty_results": len(empty_warn),
    }
    print(f"merged_{n}: {summary['already_ok']} ok, {summary['repaired']} repaired, "
          f"{summary['removed']} removed as unanswerable, {summary['unfixable']} unfixable, "
          f"{summary['empty_results']} empty-result warnings -> {out_q.name} ({summary['kept']} questions)")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="large_schema_eval_v2")
    ap.add_argument("--sets", nargs="*", type=int, default=[50, 100, 200])
    args = ap.parse_args()
    eval_dir = Path(args.dir)
    results = [repair_set(eval_dir, n) for n in args.sets]
    total_unfixable = sum(r["unfixable"] for r in results)
    print(f"\nDone. {total_unfixable} unfixable queries across all sets "
          f"(see gold_repair_changelog_*.json).")


if __name__ == "__main__":
    main()
