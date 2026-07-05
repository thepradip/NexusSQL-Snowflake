"""Retrieval-ablation re-run driver for the paper.

Runs the LargeSchemaEval v2 question sets through the ariasql FAST pipeline
under the four retrieval modes (full / bm25_only / dense_only / rrf) and
records per-row provenance so a run is only valid if every row's actual
retrieval mode equals the requested mode.

Why the old results_100.csv / results_200.csv are invalid:
  - dense_only silently fell back to BM25 (now a hard RuntimeError in
    ariasql/schema_index.py — this driver must never weaken that).
  - the gold sets had broken queries (repaired in large_schema_eval_v2/*_v2.json).

Design notes (paper integrity):
  - Query cache and result cache are DISABLED via env before Settings load:
    a semantic cache hit would silently reuse SQL generated under a different
    retrieval mode and contaminate the ablation.
  - agentic_mode is DISABLED so every question goes through the fast pipeline
    (matches the old runs; ReAct would confound retrieval attribution).
  - ContextEngine is bypassed (agent._context_engine = None) so retrieval goes
    through SchemaIndex.retrieve_relevant_tables, the only code path that
    honors settings.retrieval_mode and records last_retrieval_mode_actual.
    VectorStore/ContextEngine are stubbed at import time purely to skip their
    (unused) ChromaDB build cost — no package files are modified.
  - Between modes the cached Settings singleton (lru_cache get_settings) is
    mutated in place; every ariasql module holds a reference to that same
    object, and correctness is verified per-row via retrieval_mode_actual.

Examples:
    python bench/run_ablation.py --set 100 --limit 5          # pilot
    python bench/run_ablation.py --set 100                    # full 100-set
    python bench/run_ablation.py --set 200 --modes full rrf
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sqlite3
import sys
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BENCH = REPO / "bench"
EVAL_DIR = REPO / "large_schema_eval_v2"

MODES = ["full", "bm25_only", "dense_only", "rrf"]
DENSE_MODES = {"dense_only", "rrf"}

FIELDNAMES = [
    "index",
    "question",
    "difficulty",
    "source_db",
    "gold_sql",
    "run_name",
    "retrieval_mode_actual",
    "generated_sql",
    "exec_match",
    "gold_rows",
    "pred_rows",
    "schema_f1",
    "tables_retrieved",
    "latency_ms",
    "error",
]


# ── Environment wiring (must happen before any ariasql import) ────────────────

def load_dotenv_file(path: Path) -> None:
    """Load KEY=VALUE lines from .env without overriding the existing env."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def wire_environment(db_path: Path) -> None:
    load_dotenv_file(REPO / ".env")
    # Embeddings: required for dense_only / rrf / full-with-dense. Verified
    # working against the chat endpoint (dim 1536).
    os.environ.setdefault("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
    # Ablation validity — these MUST hold regardless of ambient env:
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["AGENTIC_MODE"] = "false"        # fast pipeline only
    os.environ["CACHE_ENABLED"] = "false"       # no cross-mode SQL reuse
    os.environ["RESULT_CACHE_TTL"] = "0"
    os.environ["MAX_RESULT_ROWS"] = "100000"    # avoid truncation-induced mismatches


def stub_unused_heavy_modules() -> None:
    """Stub VectorStore/ContextEngine before ariasql.agent imports them.

    The ablation bypasses ContextEngine entirely (see run notes above); the
    stubs only prevent init_agent from building an unused ChromaDB index
    (hundreds of embedding calls). Runtime-only — no package files touched.
    """

    class _StubVectorStore:
        def __init__(self, *args, **kwargs):
            pass

        def build(self, *args, **kwargs):
            print("  [ablation] VectorStore build skipped (stubbed, not used).")

    class _StubContextEngine:
        def __init__(self, *args, **kwargs):
            pass

    vs_mod = types.ModuleType("ariasql.vector_store")
    vs_mod.VectorStore = _StubVectorStore
    ce_mod = types.ModuleType("ariasql.context_engine")
    ce_mod.ContextEngine = _StubContextEngine
    sys.modules["ariasql.vector_store"] = vs_mod
    sys.modules["ariasql.context_engine"] = ce_mod


# ── Scoring helpers ────────────────────────────────────────────────────────────

def gold_result_rows(db_path: Path, sql: str, timeout: float = 30.0) -> list[tuple]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=timeout)
    try:
        cur = con.execute(sql)
        return cur.fetchall()
    finally:
        con.close()


def schema_retrieval_f1(retrieved: list[str], gold_tables: set[str]) -> float:
    pred = {t.lower() for t in retrieved}
    gold = {t.lower() for t in gold_tables}
    if not pred and not gold:
        return 1.0
    if not pred or not gold:
        return 0.0
    inter = len(pred & gold)
    if inter == 0:
        return 0.0
    precision = inter / len(pred)
    recall = inter / len(gold)
    return 2 * precision * recall / (precision + recall)


def existing_done(path: Path) -> set[tuple[str, int]]:
    """Resume support: rows already written, keyed by (run_name, index)."""
    if not path.exists():
        return set()
    done: set[tuple[str, int]] = set()
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                done.add((row["run_name"], int(row["index"])))
            except Exception:
                pass
    return done


# ── Main ───────────────────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> None:
    db_path = EVAL_DIR / f"merged_{args.set}.sqlite"
    questions_path = EVAL_DIR / f"questions_{args.set}_v2.json"
    if not db_path.exists():
        raise SystemExit(f"missing database: {db_path}")
    if not questions_path.exists():
        raise SystemExit(f"missing question set: {questions_path}")

    wire_environment(db_path)
    stub_unused_heavy_modules()
    sys.path.insert(0, str(REPO / "ariasql-package"))
    sys.path.insert(0, str(BENCH))

    # Keep .schema_embed_cache.json / mlruns in a stable location.
    os.chdir(REPO)

    from bird_common import extract_tables, result_match  # noqa: E402

    import ariasql.agent as agent_mod  # noqa: E402  (env already wired)
    from ariasql.config import get_settings  # noqa: E402

    settings = get_settings()
    assert settings.agentic_mode is False, "agentic_mode must be off for the ablation"
    assert settings.cache_enabled is False, "query cache must be off for the ablation"
    assert settings.azure_openai_embedding_deployment, "embedding deployment missing"

    # Embedding-client wiring fix: agent.py builds its embeddings client with
    # AzureOpenAI(azure_endpoint=...), which double-prefixes the path on Azure
    # "v1" endpoints (.../openai/v1) and 404s. The chat provider
    # (llm_providers.AzureOpenAIProvider) already special-cases this by using a
    # plain OpenAI client; mirror that here for the embeddings client.
    # Verified: embeddings.create(model=text-embedding-3-small) → dim 1536.
    endpoint = settings.azure_openai_endpoint.rstrip("/")
    if endpoint.endswith("/openai/v1"):
        from openai import OpenAI

        agent_mod.client = OpenAI(
            api_key=settings.azure_openai_api_key, base_url=f"{endpoint}/"
        )

    print(f"DB: {db_path.name} | questions: {questions_path.name} | modes: {args.modes}")
    await agent_mod.init_agent()

    idx = agent_mod._schema_index
    if idx is None:
        raise SystemExit(
            "SchemaIndex was not built — database fell below large_schema_threshold; "
            "the retrieval ablation is undefined for the full-context path."
        )

    # Bypass ContextEngine: retrieval must go through
    # SchemaIndex.retrieve_relevant_tables (the ablation-aware path).
    agent_mod._context_engine = None

    if DENSE_MODES & set(args.modes) and not idx._embeddings:
        raise SystemExit(
            "dense_only/rrf requested but the schema index has no embeddings — "
            "fix AZURE_OPENAI_EMBEDDING_DEPLOYMENT wiring; refusing to run modes "
            "that would hard-error on every row."
        )

    # Record the tables the agent actually retrieves, per query.
    recorder: dict[str, list[str] | None] = {"tables": None}
    original_retrieve = idx.retrieve_relevant_tables

    def recording_retrieve(query: str, top_k=None) -> list[str]:
        tables = original_retrieve(query, top_k)
        recorder["tables"] = list(tables)
        return tables

    idx.retrieve_relevant_tables = recording_retrieve  # instance-level wrap

    questions = json.loads(questions_path.read_text(encoding="utf-8"))
    if args.limit:
        questions = questions[: args.limit]

    out_path = Path(args.out) if args.out else BENCH / f"results_ablation_{args.set}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = existing_done(out_path)
    is_new = not out_path.exists()

    summary: dict[str, dict[str, float]] = {}

    with out_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if is_new:
            writer.writeheader()

        for mode in args.modes:
            get_settings().retrieval_mode = mode  # shared singleton; see notes
            stats = summary.setdefault(
                mode, {"n": 0, "match": 0, "actual_ok": 0, "f1_sum": 0.0, "errors": 0}
            )
            print(f"\n=== mode: {mode} ({len(questions)} questions) ===", flush=True)

            for i, q in enumerate(questions):
                if (mode, i) in done:
                    continue
                question = q["question"]
                gold_sql = q["gold_sql"]

                recorder["tables"] = None
                idx.last_retrieval_mode_actual = None

                start = time.perf_counter()
                err = ""
                generated_sql = ""
                pred_rows: list[tuple] = []
                success = False
                try:
                    result = await agent_mod.run_query(question, [])
                    generated_sql = result.get("sql") or ""
                    success = bool(result.get("success"))
                    if not success:
                        err = (result.get("metrics") or {}).get("error_type") or "pipeline_failed"
                    data = result.get("data") or {}
                    pred_rows = [tuple(r) for r in (data.get("rows") or [])]
                except Exception as exc:
                    err = f"{type(exc).__name__}:{exc}"[:300]
                latency_ms = round((time.perf_counter() - start) * 1000)

                gold_rows: list[tuple] = []
                gold_err = ""
                try:
                    gold_rows = gold_result_rows(db_path, gold_sql)
                except Exception as exc:
                    gold_err = f"gold_exec_failed:{type(exc).__name__}:{exc}"[:200]
                    err = err or gold_err

                exec_match = int(
                    success and not gold_err and result_match(pred_rows, gold_rows)
                )

                retrieved = recorder["tables"] or []
                gold_tables = extract_tables(gold_sql)
                f1 = schema_retrieval_f1(retrieved, gold_tables)
                actual = idx.last_retrieval_mode_actual or ""

                writer.writerow(
                    {
                        "index": i,
                        "question": question,
                        "difficulty": q.get("difficulty", ""),
                        "source_db": q.get("source_db", ""),
                        "gold_sql": gold_sql,
                        "run_name": mode,
                        "retrieval_mode_actual": actual,
                        "generated_sql": generated_sql,
                        "exec_match": exec_match,
                        "gold_rows": len(gold_rows),
                        "pred_rows": len(pred_rows),
                        "schema_f1": round(f1, 4),
                        "tables_retrieved": json.dumps(retrieved),
                        "latency_ms": latency_ms,
                        "error": err,
                    }
                )
                f.flush()

                stats["n"] += 1
                stats["match"] += exec_match
                stats["actual_ok"] += int(actual == mode)
                stats["f1_sum"] += f1
                stats["errors"] += int(bool(err))

                status = "ERR" if err else "OK"
                mode_flag = "" if actual == mode else f" MODE-MISMATCH(actual={actual!r})"
                print(
                    f"[{mode} {i + 1}/{len(questions)} idx={i} {q.get('source_db', '?')} "
                    f"{q.get('difficulty', '?')}] {status} match={exec_match} "
                    f"f1={f1:.2f} actual={actual} {latency_ms}ms{mode_flag}",
                    flush=True,
                )

    print("\n=== summary ===")
    invalid = False
    for mode, s in summary.items():
        n = int(s["n"])
        if n == 0:
            print(f"{mode:>10}: all rows already done (resumed)")
            continue
        avg_f1 = s["f1_sum"] / n
        ok = int(s["actual_ok"]) == n
        invalid = invalid or not ok
        print(
            f"{mode:>10}: n={n} exec_match={int(s['match'])} "
            f"avg_f1={avg_f1:.3f} errors={int(s['errors'])} "
            f"actual_mode_ok={int(s['actual_ok'])}/{n}"
            + ("" if ok else "  << RUN INVALID: actual mode != requested")
        )
    if invalid:
        raise SystemExit("run invalid: at least one row executed under the wrong retrieval mode")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--set", type=int, default=100, choices=[50, 100, 200, 500])
    parser.add_argument("--modes", nargs="+", default=list(MODES), choices=MODES)
    parser.add_argument("--limit", type=int, default=0, help="first N questions (pilot)")
    parser.add_argument("--out", default="", help="default: bench/results_ablation_<set>.csv")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
