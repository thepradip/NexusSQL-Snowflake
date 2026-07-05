"""
Snapshot live result CSVs every N new rows.

Polls the watched files; whenever a file has grown by at least N data rows
since its last snapshot, copies it to bench/checkpoints/<stem>.rows<count>.csv.
Snapshots are plain copies — safe to open/inspect while the run continues.

Usage:
    python bench/checkpoint_watcher.py \
        bench/results_bird_gpt55_full_dev.csv bench/results_ablation_100.csv \
        --every 20
Stops on its own when the watched files stop growing for --idle-minutes.
"""

import argparse
import csv
import shutil
import time
from pathlib import Path

CHECKPOINT_DIR = Path(__file__).resolve().parent / "checkpoints"


def row_count(path: Path) -> int:
    try:
        with path.open(newline="", encoding="utf-8") as f:
            return sum(1 for _ in csv.DictReader(f))
    except FileNotFoundError:
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--every", type=int, default=20)
    ap.add_argument("--poll-seconds", type=int, default=30)
    ap.add_argument("--idle-minutes", type=int, default=30)
    args = ap.parse_args()

    CHECKPOINT_DIR.mkdir(exist_ok=True)
    files = [Path(f) for f in args.files]
    last_saved = {f: 0 for f in files}
    last_growth = time.time()

    while True:
        grew = False
        for f in files:
            n = row_count(f)
            if n >= last_saved[f] + args.every:
                snap = CHECKPOINT_DIR / f"{f.stem}.rows{n:04d}.csv"
                shutil.copy2(f, snap)
                last_saved[f] = n
                grew = True
                print(f"[{time.strftime('%H:%M:%S')}] snapshot {snap.name} ({n} rows)")
        if grew:
            last_growth = time.time()
        elif time.time() - last_growth > args.idle_minutes * 60:
            print("watched files idle — watcher exiting")
            return
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
