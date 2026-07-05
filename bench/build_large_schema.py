"""
Build a merged large-schema SQLite benchmark set from Spider train databases.

Written for the 500-table scale point of LargeSchemaEval. The original
50/100/200 sets were produced by an ad-hoc script that no longer exists;
this rebuilds the same recipe from the summary_*.json metadata, with one
critical difference (see bench/repair_gold.py): the original merge rewrote
gold SQL with a naive string replace, which also prefixed *column*
references sharing a table's name. Here gold SQL is rewritten on the AST
with sqlglot — only exp.Table nodes are renamed, never exp.Column — so the
question set is born clean (written directly with the _v2 suffix, no
repair pass needed).

Recipe (matches how merged_50/100/200 were built):
  * Carry over the source DBs of the previous scale point (summary_200),
    then add further Spider train DBs (most tables first) until the merged
    table count reaches the target.
  * Every table of every source DB is copied into one SQLite file as
    "<source_db>_<table>" (schema flattened to column names + types, no
    PK/FK constraints — same style as merged_200). Data is copied inside
    SQLite via ATTACH, so no Python text decoding can corrupt rows.
  * Questions come from spider train_spider.json for each source DB. Gold
    SQL is AST-rewritten, then EXECUTED against the merged DB; anything
    that errors is dropped (counted in the summary). Per-DB question count
    is capped so the total lands in the requested band, sampling
    round-robin across difficulty buckets for a mix.
  * Difficulty uses the same heuristic as the existing sets (verified to
    reproduce all 993 existing labels exactly):
        score = #joins + group_by + having + 2*#nested_selects + 3*#set_ops
        0 -> easy, 1 -> medium, 2-3 -> hard, >=4 -> extra hard

Verification (built in, the script fails if it does not hold):
  * every gold SQL in the emitted question file executes with zero errors;
  * table count is within the accepted band;
  * empty-result golds are reported as warnings only (same as repair_gold).

Outputs (never overwrites the inputs of the smaller sets):
  <dir>/merged_<N>.sqlite, <dir>/questions_<N>_v2.json, <dir>/summary_<N>.json

Usage:
  python bench/build_large_schema.py                # 500-table set, defaults
  python bench/build_large_schema.py --target 500 --min-tables 450 --max-tables 520
"""

import argparse
import json
import random
import re
import sqlite3
from pathlib import Path

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- difficulty
def difficulty(sql: str) -> str:
    """Difficulty heuristic of the existing sets (993/993 label agreement)."""
    s = " " + re.sub(r"\s+", " ", sql.lower()).strip() + " "
    joins = s.count(" join ")
    group = int(" group by " in s)
    having = int(" having " in s)
    setops = sum(s.count(k) for k in (" intersect ", " except ", " union "))
    nested = len(re.findall(r"\bselect\b", s)) - 1 - setops
    score = joins + group + having + 2 * nested + 3 * setops
    if score == 0:
        return "easy"
    if score == 1:
        return "medium"
    if score <= 3:
        return "hard"
    return "extra hard"


# ------------------------------------------------------------- gold rewrite
def prefix_tables(sql: str, source_db: str, source_tables: list[str]) -> str:
    """Rewrite gold SQL on the AST: rename only exp.Table nodes.

    Column nodes are never touched, which is exactly the bug class the
    naive string replace of the original merge introduced (repair_gold.py).
    """
    by_lower = {t.lower(): t for t in source_tables}
    tree = sqlglot.parse_one(sql, dialect="sqlite")
    for tbl in tree.find_all(exp.Table):
        name = tbl.name
        if name and name.lower() in by_lower:
            tbl.this.set("this", f"{source_db}_{by_lower[name.lower()]}")
    return tree.sql(dialect="sqlite")


# ---------------------------------------------------------------- DB merge
def list_tables(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY rowid")]


def quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def copy_db(out: sqlite3.Connection, db_path: Path, source_db: str) -> list[str]:
    """Copy every table of db_path into `out` as <source_db>_<table>.

    Schema is flattened to column names + declared types (no constraints),
    matching the DDL style of merged_50/100/200. Rows are moved with
    ATTACH + INSERT ... SELECT so the bytes never round-trip through Python.
    Returns the list of source table names copied.
    """
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    tables = list_tables(src)
    ddl = {}
    for t in tables:
        cols = src.execute(f"PRAGMA table_info({quote(t)})").fetchall()
        if not cols:
            raise RuntimeError(f"{source_db}.{t}: no column info")
        col_sql = ", ".join(
            f"{quote(c[1])} {c[2]}" if c[2] else quote(c[1]) for c in cols)
        ddl[t] = f"CREATE TABLE {quote(source_db + '_' + t)} ({col_sql})"
    src.close()

    out.execute("ATTACH DATABASE ? AS src", (str(db_path),))
    try:
        for t in tables:
            out.execute(ddl[t])
            out.execute(
                f"INSERT INTO main.{quote(source_db + '_' + t)} "
                f"SELECT * FROM src.{quote(t)}")
        out.commit()
    finally:
        out.execute("DETACH DATABASE src")
    return tables


# ------------------------------------------------------------ DB selection
def select_dbs(spider_db_dir: Path, carry_over: list[str], train_by_db: dict,
               target: int, max_tables: int):
    """Carried-over DBs first, then largest candidates until >= target."""
    def table_count(db):
        p = spider_db_dir / db / f"{db}.sqlite"
        if not p.exists():
            return None
        try:
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            n = len(list_tables(c))
            c.close()
            return n
        except sqlite3.Error:
            return None

    skipped = []
    chosen, total = [], 0
    for db in carry_over:
        n = table_count(db)
        if n is None:
            skipped.append({"db": db, "reason": "carried-over DB unreadable/missing"})
            continue
        chosen.append(db)
        total += n

    candidates = []
    for db in sorted(p.name for p in spider_db_dir.iterdir() if p.is_dir()):
        if db in carry_over:
            continue
        if not train_by_db.get(db):
            skipped.append({"db": db, "reason": "no examples in train_spider.json"})
            continue
        n = table_count(db)
        if n is None:
            skipped.append({"db": db, "reason": "sqlite file missing or unreadable"})
            continue
        candidates.append((db, n))
    candidates.sort(key=lambda x: (-x[1], x[0]))

    for db, n in candidates:
        if total >= target:
            break
        if total + n > max_tables:
            skipped.append({"db": db, "reason": f"would exceed max_tables={max_tables}"})
            continue
        chosen.append(db)
        total += n
    return chosen, total, skipped


# -------------------------------------------------------------- questions
def build_questions(conn, chosen: list[str], train_by_db: dict,
                    tables_by_db: dict, q_min: int, q_max: int, seed: int):
    """AST-rewrite, execute-filter, then cap per DB for a total in band."""
    valid_by_db, dropped = {}, []
    for db in chosen:
        valid = []
        for ex in train_by_db[db]:
            try:
                gold = prefix_tables(ex["query"], db, tables_by_db[db])
                conn.execute(gold).fetchall()
            except Exception as e:  # parse or execution failure -> drop
                dropped.append({"source_db": db, "question": ex["question"],
                                "query": ex["query"], "error": str(e)})
                continue
            valid.append({"question": ex["question"], "gold_sql": gold,
                          "difficulty": difficulty(gold), "source_db": db})
        valid_by_db[db] = valid

    # Largest per-DB cap whose total stays within q_max.
    cap = max(len(v) for v in valid_by_db.values())
    while cap > 1 and sum(min(cap, len(v)) for v in valid_by_db.values()) > q_max:
        cap -= 1
    total = sum(min(cap, len(v)) for v in valid_by_db.values())
    if total < q_min:
        raise SystemExit(f"only {total} questions available (< {q_min}); "
                         f"add more source DBs")

    rng = random.Random(seed)
    picked = []
    order = ["easy", "medium", "hard", "extra hard"]
    for db in chosen:
        valid = valid_by_db[db]
        k = min(cap, len(valid))
        buckets = {d: [q for q in valid if q["difficulty"] == d] for d in order}
        for b in buckets.values():
            rng.shuffle(b)
        sel = []  # round-robin over difficulties for a mix
        while len(sel) < k:
            for d in order:
                if buckets[d] and len(sel) < k:
                    sel.append(buckets[d].pop())
        picked.extend(sel)
    return picked, dropped, cap


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--target", type=int, default=500)
    ap.add_argument("--min-tables", type=int, default=450)
    ap.add_argument("--max-tables", type=int, default=520)
    ap.add_argument("--q-min", type=int, default=250)
    ap.add_argument("--q-max", type=int, default=400)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dir", default=str(ROOT / "large_schema_eval_v2"))
    ap.add_argument("--spider", default=str(ROOT / "spider_download" / "spider"))
    ap.add_argument("--carry-over-summary", default=None,
                    help="summary_*.json whose source DBs are carried over "
                         "(default: summary_200.json in --dir)")
    args = ap.parse_args()

    eval_dir = Path(args.dir)
    spider_db_dir = Path(args.spider) / "database"
    carry_file = Path(args.carry_over_summary) if args.carry_over_summary \
        else eval_dir / "summary_200.json"
    carry_over = json.loads(carry_file.read_text())["source_databases"]

    train = json.loads((Path(args.spider) / "train_spider.json").read_text())
    train_by_db = {}
    for ex in train:
        train_by_db.setdefault(ex["db_id"], []).append(ex)

    chosen, planned_tables, skipped = select_dbs(
        spider_db_dir, carry_over, train_by_db, args.target, args.max_tables)
    print(f"selected {len(chosen)} source DBs, ~{planned_tables} tables planned")

    db_path = eval_dir / f"merged_{args.target}.sqlite"
    if db_path.exists():
        db_path.unlink()
    out = sqlite3.connect(db_path)
    tables_by_db = {}
    for db in list(chosen):
        try:
            tables_by_db[db] = copy_db(out, spider_db_dir / db / f"{db}.sqlite", db)
        except Exception as e:
            out.rollback()
            chosen.remove(db)
            skipped.append({"db": db, "reason": f"copy failed: {e}"})
            print(f"  SKIP {db}: {e}")

    n_tables = len(list_tables(out))
    print(f"merged DB written: {db_path} ({n_tables} tables)")
    if not args.min_tables <= n_tables <= args.max_tables:
        raise SystemExit(f"table count {n_tables} outside "
                         f"[{args.min_tables}, {args.max_tables}]")

    questions, dropped, cap = build_questions(
        out, chosen, train_by_db, tables_by_db, args.q_min, args.q_max, args.seed)
    for q in questions:
        q_ordered = {"question": q["question"], "gold_sql": q["gold_sql"],
                     "db_id": f"merged_{args.target}",
                     "difficulty": q["difficulty"], "source_db": q["source_db"]}
        q.clear()
        q.update(q_ordered)
    out.close()

    # ---- verification against the file on disk, read-only ----
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    failures, empty = [], []
    for i, q in enumerate(questions):
        try:
            rows = conn.execute(q["gold_sql"]).fetchall()
        except Exception as e:
            failures.append({"index": i, "error": str(e), "gold_sql": q["gold_sql"]})
            continue
        if not rows or all(all(v is None for v in r) for r in rows):
            empty.append(i)
    conn.close()
    if failures:
        for f in failures[:10]:
            print("GOLD FAILURE:", f)
        raise SystemExit(f"{len(failures)} gold queries failed verification")
    print(f"verification: {len(questions)} gold queries, 0 failures, "
          f"{len(empty)} empty-result warnings")

    q_path = eval_dir / f"questions_{args.target}_v2.json"
    q_path.write_text(json.dumps(questions, indent=1))

    per_db = {}
    for q in questions:
        per_db[q["source_db"]] = per_db.get(q["source_db"], 0) + 1
    diff_mix = {}
    for q in questions:
        diff_mix[q["difficulty"]] = diff_mix.get(q["difficulty"], 0) + 1
    summary = {
        "target_table_count": args.target,
        "actual_table_count": n_tables,
        "source_databases": chosen,
        "total_questions": len(questions),
        "questions_per_source_db": per_db,
        "difficulty_mix": diff_mix,
        "per_db_question_cap": cap,
        "seed": args.seed,
        "merged_db_path": str(db_path.resolve()),
        "questions_path": str(q_path.resolve()),
        "gold_sql_rewrite": "sqlglot AST, exp.Table nodes only (born clean, "
                            "no repair pass needed)",
        "gold_verification": {"executed": len(questions), "failures": 0,
                              "empty_result_warnings": len(empty)},
        "dropped_unexecutable_examples": len(dropped),
        "skipped_dbs": skipped,
    }
    s_path = eval_dir / f"summary_{args.target}.json"
    s_path.write_text(json.dumps(summary, indent=2))
    print(f"wrote {q_path}\nwrote {s_path}")
    print(f"questions per difficulty: {diff_mix}")


if __name__ == "__main__":
    main()
