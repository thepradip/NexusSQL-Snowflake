"""
Compute human-agreement statistics for the SQLAS annotation study.

Inputs: completed annotator sheets (annotator_<name>_completed.csv) and the
original LLM-judged run (results_v2_full_judged.csv). Items are matched by
item_id (assigned by make_annotation_sheets.py in original row order).

Reports:
  * Inter-annotator reliability: pairwise Cohen's kappa on human_verdict,
    and mean pairwise Spearman rho on human_correctness / human_quality.
  * SQLAS-vs-human agreement: Cohen's kappa between SQLAS verdict
    (PASS vs any FAIL) and the human majority verdict; Spearman rho
    between SQLAS correctness_score/quality_score and mean human scores.

All statistics are implemented inline (no scipy dependency); ties are
handled with average ranks. Report whatever the numbers come out to be —
the paper cites these values directly.

Usage:
    python bench/annotation/compute_agreement.py annotator_a_completed.csv annotator_b_completed.csv
"""

import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "results_v2_full_judged.csv"


def cohens_kappa(a: list, b: list) -> float:
    assert len(a) == len(b) and a
    n = len(a)
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb.get(k, 0) for k in ca) / (n * n)
    return 1.0 if pe == 1.0 else (po - pe) / (1 - pe)


def _avg_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman_rho(a: list[float], b: list[float]) -> float:
    ra, rb = _avg_ranks(a), _avg_ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mb) ** 2 for y in rb))
    return cov / (va * vb) if va and vb else 0.0


def load_sheet(path: str) -> dict[str, dict]:
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r.get("human_verdict", "").strip():
                continue  # skipped/blank rows are excluded per instructions
            out[r["item_id"]] = {
                "correctness": float(r["human_correctness"]),
                "quality": float(r["human_quality"]),
                "safety": r["human_safety"].strip().upper(),
                "verdict": r["human_verdict"].strip().upper(),
            }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sheets", nargs="+", help="completed annotator CSVs")
    ap.add_argument("--source", default=str(SOURCE))
    args = ap.parse_args()

    annotators = {Path(p).stem: load_sheet(p) for p in args.sheets}
    names = list(annotators)

    with open(args.source, newline="", encoding="utf-8") as f:
        sqlas_rows = list(csv.DictReader(f))
    sqlas = {f"Q{i:03d}": r for i, r in enumerate(sqlas_rows)}

    common = set(sqlas)
    for sheet in annotators.values():
        common &= set(sheet)
    common = sorted(common)
    print(f"items annotated by all {len(names)} annotators: {len(common)}")

    report = {"n_items": len(common), "annotators": names,
              "inter_annotator": {}, "sqlas_vs_human": {}}

    # ── Inter-annotator reliability ────────────────────────────────────────
    kappas, rho_c, rho_q = [], [], []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = annotators[names[i]], annotators[names[j]]
            k = cohens_kappa([a[q]["verdict"] for q in common],
                             [b[q]["verdict"] for q in common])
            kappas.append(k)
            rho_c.append(spearman_rho([a[q]["correctness"] for q in common],
                                      [b[q]["correctness"] for q in common]))
            rho_q.append(spearman_rho([a[q]["quality"] for q in common],
                                      [b[q]["quality"] for q in common]))
            print(f"kappa(verdict) {names[i]} vs {names[j]}: {k:.3f}")
    if kappas:
        report["inter_annotator"] = {
            "mean_pairwise_kappa_verdict": round(sum(kappas) / len(kappas), 3),
            "mean_pairwise_spearman_correctness": round(sum(rho_c) / len(rho_c), 3),
            "mean_pairwise_spearman_quality": round(sum(rho_q) / len(rho_q), 3),
        }

    # ── SQLAS vs human ─────────────────────────────────────────────────────
    def majority_verdict(q):
        votes = [annotators[n][q]["verdict"] for n in names]
        return Counter(votes).most_common(1)[0][0]

    human_verdicts = [majority_verdict(q) for q in common]
    sqlas_verdicts = ["PASS" if sqlas[q]["verdict"].strip().upper() == "PASS" else "FAIL"
                      for q in common]
    report["sqlas_vs_human"]["kappa_verdict_vs_majority"] = round(
        cohens_kappa(sqlas_verdicts, human_verdicts), 3)

    mean_h_corr = [sum(annotators[n][q]["correctness"] for n in names) / len(names)
                   for q in common]
    mean_h_qual = [sum(annotators[n][q]["quality"] for n in names) / len(names)
                   for q in common]
    report["sqlas_vs_human"]["spearman_correctness"] = round(
        spearman_rho([float(sqlas[q]["correctness_score"]) for q in common], mean_h_corr), 3)
    report["sqlas_vs_human"]["spearman_quality"] = round(
        spearman_rho([float(sqlas[q]["quality_score"]) for q in common], mean_h_qual), 3)

    agree = sum(1 for s, h in zip(sqlas_verdicts, human_verdicts) if s == h)
    report["sqlas_vs_human"]["raw_verdict_agreement"] = round(agree / len(common), 3)

    out = Path(__file__).resolve().parent / "agreement_report.json"
    out.write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
