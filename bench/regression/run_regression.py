"""
SQLAS scoring regression gate.

Scores a fixed set of (gold, generated) SQL pairs against the deterministic
fixture DB using the offline SQLAS metrics (execution accuracy, syntax,
read-only compliance, SQL-injection) and compares every score to
baseline.json. A drift larger than TOLERANCE on any metric fails the gate —
catching accidental changes to metric semantics before they corrupt
paper numbers.

Usage:
    python bench/regression/run_regression.py                    # gate (CI)
    python bench/regression/run_regression.py --update-baseline  # re-baseline
                                              (only after an INTENTIONAL
                                               metric change, in the same PR)
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from bench.regression.build_fixture import build  # noqa: E402
from sqlas.correctness import execution_accuracy, syntax_valid  # noqa: E402
from sqlas.safety import read_only_compliance, sql_injection_score  # noqa: E402

TOLERANCE = 0.05
BASELINE = HERE / "baseline.json"
CASES = HERE / "fixture_cases.json"


def score_all() -> dict:
    db = str(build())
    out = {}
    for case in json.loads(CASES.read_text()):
        exec_acc, details = execution_accuracy(case["generated_sql"], case["gold_sql"], db_path=db)
        inj, _ = sql_injection_score(case["generated_sql"])
        out[case["id"]] = {
            "execution_accuracy": round(exec_acc, 4),
            "output_score": round(details.get("output_score", 0.0), 4),
            "syntax_valid": syntax_valid(case["generated_sql"]),
            "read_only": read_only_compliance(case["generated_sql"]),
            "sql_injection": round(inj, 4),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args()

    scores = score_all()

    if args.update_baseline or not BASELINE.exists():
        BASELINE.write_text(json.dumps(scores, indent=1))
        print(f"baseline written: {BASELINE}")
        return

    baseline = json.loads(BASELINE.read_text())
    failures = []
    for cid, metrics in scores.items():
        base = baseline.get(cid)
        if base is None:
            failures.append(f"{cid}: no baseline entry (run --update-baseline)")
            continue
        for m, v in metrics.items():
            b = base.get(m)
            if b is None or abs(v - b) > TOLERANCE:
                failures.append(f"{cid}.{m}: baseline {b} -> now {v}")
    for cid in baseline:
        if cid not in scores:
            failures.append(f"{cid}: case removed but still in baseline")

    if failures:
        print("REGRESSION GATE FAILED:")
        for f in failures:
            print("  ", f)
        sys.exit(1)
    print(f"regression gate OK ({len(scores)} cases, tolerance {TOLERANCE})")


if __name__ == "__main__":
    main()
