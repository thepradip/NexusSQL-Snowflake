"""
Generate blinded annotation sheets for the SQLAS human-agreement study.

Reads the LLM-judged run (results_v2_full_judged.csv), strips every SQLAS
score column (annotators must not see the machine's opinion), shuffles item
order per annotator with a recorded seed, and writes one sheet per
annotator plus a key file mapping item_id -> original row for
compute_agreement.py.

Usage:
    python bench/annotation/make_annotation_sheets.py --annotators alice bob carol
"""

import argparse
import csv
import json
import random
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "results_v2_full_judged.csv"
OUT_DIR = Path(__file__).resolve().parent

SHOW_FIELDS = ["item_id", "question", "difficulty", "source_db", "gold_sql", "generated_sql"]
FILL_FIELDS = ["human_correctness", "human_quality", "human_safety", "human_verdict", "comments"]
BASE_SEED = 20260705


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--annotators", nargs="+", required=True)
    ap.add_argument("--source", default=str(SOURCE))
    args = ap.parse_args()

    with open(args.source, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    items = []
    for i, r in enumerate(rows):
        items.append({
            "item_id": f"Q{i:03d}",
            "question": r["question"],
            "difficulty": r["difficulty"],
            "source_db": r["source_db"],
            "gold_sql": r["gold_sql"],
            "generated_sql": r["generated_sql"],
        })

    key = {
        "source": str(Path(args.source).name),
        "n_items": len(items),
        "base_seed": BASE_SEED,
        "annotators": {},
    }

    for idx, name in enumerate(args.annotators):
        seed = BASE_SEED + idx
        order = list(range(len(items)))
        random.Random(seed).shuffle(order)
        sheet = OUT_DIR / f"annotator_{name}.csv"
        with open(sheet, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=SHOW_FIELDS + FILL_FIELDS)
            w.writeheader()
            for j in order:
                row = dict(items[j])
                row.update({k: "" for k in FILL_FIELDS})
                w.writerow(row)
        key["annotators"][name] = {"seed": seed, "sheet": sheet.name}
        print(f"wrote {sheet} ({len(items)} items, seed {seed})")

    (OUT_DIR / "annotation_key.json").write_text(json.dumps(key, indent=1))
    print(f"wrote {OUT_DIR / 'annotation_key.json'}")


if __name__ == "__main__":
    main()
