#!/usr/bin/env python3
"""make_paper_tables.py — deterministic generator for every ICDE paper results table.

Generates LaTeX tabular fragments (booktabs) into paper_tables/ from the files
marked CITABLE: yes in bench/RESULTS_INDEX.md. Pure stdlib, byte-deterministic:
same inputs -> byte-identical outputs (everything is sorted, no timestamps).

Inputs (all CITABLE per bench/RESULTS_INDEX.md):
  bench/results_bird_gpt55_full_dev.scored.csv   (1,534 rows, BIRD full dev)
  bench/results_ablation_100.csv                 (1,120 rows, 4 modes x 280q, 107 tables)
  bench/results_ablation_500.csv                 (1,440 rows, 4 modes x 360q, 502 tables)
  results_v2_full_judged.csv                     (100 rows, LLM-judged)
  bench/results_repeat_full_r{1,2,3}.csv         (280 rows each; table skipped until complete)

Outputs (paper_tables/):
  bird_headline.tex, bird_by_difficulty.tex, bird_by_db.tex,
  ablation_modes.tex, judged_pass.tex, repeats.tex (only when repeats complete),
  totals.tex, totals.json

Usage:
  python3 bench/make_paper_tables.py           # (re)generate paper_tables/
  python3 bench/make_paper_tables.py --check   # regenerate to a temp dir, exit 1 on any diff
"""

import csv
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "bench"
OUT_DIR = ROOT / "paper_tables"

BIRD_CSV = BENCH / "results_bird_gpt55_full_dev.scored.csv"
ABLATION_CSVS = {
    107: BENCH / "results_ablation_100.csv",
    502: BENCH / "results_ablation_500.csv",
}
JUDGED_CSV = ROOT / "results_v2_full_judged.csv"
REPEAT_CSVS = [BENCH / f"results_repeat_full_r{i}.csv" for i in (1, 2, 3)]
REPEAT_EXPECTED_ROWS = 280

MODE_ORDER = ["full", "bm25_only", "dense_only", "rrf"]  # full first (baseline), then alphabetical
DIFFICULTY_ORDER = ["simple", "moderate", "challenging"]

BOOTSTRAP_SEED = 20260705
BOOTSTRAP_RESAMPLES = 10000

Z95 = 1.959963984540054  # two-sided 95% normal quantile


# ---------------------------------------------------------------- helpers

def wilson_ci(k, n, z=Z95):
    """Wilson score 95% CI for a binomial proportion. Returns (lo, hi) as fractions."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def pct(x):
    """Format a fraction as a percentage with 1 decimal (deterministic)."""
    return f"{x * 100.0:.1f}"


def fmt_ci(k, n):
    lo, hi = wilson_ci(k, n)
    return f"[{pct(lo)}, {pct(hi)}]"


def mcnemar_exact_p(b, c):
    """Exact two-sided binomial McNemar p-value from discordant pair counts.

    b = baseline correct / other wrong;  c = baseline wrong / other correct.
    p = 2 * P(X <= min(b,c)) with X ~ Binomial(b+c, 0.5), capped at 1.0.
    """
    n = b + c
    if n == 0:
        return 1.0
    m = min(b, c)
    tail = sum(math.comb(n, k) for k in range(m + 1)) / (2.0 ** n)
    return min(1.0, 2.0 * tail)


def fmt_p(p):
    if p >= 0.995:
        return "1.00"
    if p >= 0.10:
        return f"{p:.2f}"
    if p >= 0.001:
        return f"{p:.3f}"
    return "$<$0.001"


def latex_escape(s):
    return s.replace("_", r"\_")


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def mean(vals):
    return sum(vals) / len(vals) if vals else 0.0


# ---------------------------------------------------------------- tables

def gen_bird_headline(rows):
    n = len(rows)
    k = sum(1 for r in rows if r["execution_match"] == "1")
    ves = mean([float(r["ves"]) for r in rows])
    lines = [
        r"\begin{tabular}{lrrcc}",
        r"\toprule",
        r"Benchmark & $n$ & Exec.\ match & 95\% CI (Wilson) & VES \\",
        r"\midrule",
        f"BIRD dev (full) & {n} & {k}/{n} = {pct(k / n)}\\% & {fmt_ci(k, n)} & {ves:.3f} \\\\",
        r"\bottomrule",
        r"\end{tabular}",
    ]
    return "\n".join(lines) + "\n", {"n": n, "correct": k, "pct": pct(k / n), "ci": fmt_ci(k, n), "ves": round(ves, 3)}


def gen_bird_by_difficulty(rows):
    lines = [
        r"\begin{tabular}{lrrcc}",
        r"\toprule",
        r"Difficulty & $n$ & Exec.\ match & 95\% CI (Wilson) & VES \\",
        r"\midrule",
    ]
    stats = {}
    for diff in DIFFICULTY_ORDER:
        sub = [r for r in rows if r["difficulty"] == diff]
        n = len(sub)
        k = sum(1 for r in sub if r["execution_match"] == "1")
        ves = mean([float(r["ves"]) for r in sub])
        lines.append(
            f"{diff.capitalize()} & {n} & {k}/{n} = {pct(k / n)}\\% & {fmt_ci(k, n)} & {ves:.3f} \\\\"
        )
        stats[diff] = {"n": n, "correct": k, "pct": pct(k / n)}
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines) + "\n", stats


def gen_bird_by_db(rows):
    dbs = sorted({r["db"] for r in rows})
    lines = [
        r"\begin{tabular}{lrrcc}",
        r"\toprule",
        r"Database & $n$ & Exec.\ match & 95\% CI (Wilson) & VES \\",
        r"\midrule",
    ]
    stats = {}
    for db in dbs:
        sub = [r for r in rows if r["db"] == db]
        n = len(sub)
        k = sum(1 for r in sub if r["execution_match"] == "1")
        ves = mean([float(r["ves"]) for r in sub])
        lines.append(
            f"{latex_escape(db)} & {n} & {k}/{n} = {pct(k / n)}\\% & {fmt_ci(k, n)} & {ves:.3f} \\\\"
        )
        stats[db] = {"n": n, "correct": k, "pct": pct(k / n)}
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines) + "\n", stats


def gen_ablation(rows_by_scale):
    """4 retrieval modes x scales, exec-match % + Wilson CI + exact McNemar p vs full."""
    scales = sorted(rows_by_scale)
    header_cells = " & ".join(
        f"\\multicolumn{{3}}{{c}}{{{s} tables}}" for s in scales
    )
    sub_cells = " & ".join(
        r"Exec.\ match & 95\% CI & $p$ (vs.\ full)" for _ in scales
    )
    cmid = " ".join(
        f"\\cmidrule(lr){{{2 + 3 * i}-{4 + 3 * i}}}" for i in range(len(scales))
    )
    lines = [
        r"\begin{tabular}{l" + "rcc" * len(scales) + "}",
        r"\toprule",
        f"Retrieval mode & {header_cells} \\\\",
        cmid,
        f" & {sub_cells} \\\\",
        r"\midrule",
    ]

    # Precompute per-scale paired maps: question index -> exec_match per mode.
    per_scale = {}
    for scale in scales:
        rows = rows_by_scale[scale]
        by_mode = {}
        for r in rows:
            by_mode.setdefault(r["retrieval_mode_actual"], {})[r["index"]] = (
                r["exec_match"] == "1"
            )
        per_scale[scale] = by_mode

    stats = {}
    for mode in MODE_ORDER:
        cells = []
        for scale in scales:
            by_mode = per_scale[scale]
            res = by_mode[mode]
            n = len(res)
            k = sum(1 for v in res.values() if v)
            if mode == "full":
                p_str = "---"
                p_val = None
            else:
                full = by_mode["full"]
                common = sorted(set(full) & set(res))
                b = sum(1 for q in common if full[q] and not res[q])
                c = sum(1 for q in common if not full[q] and res[q])
                p_val = mcnemar_exact_p(b, c)
                p_str = fmt_p(p_val)
            cells.append(f"{k}/{n} = {pct(k / n)}\\% & {fmt_ci(k, n)} & {p_str}")
            stats[f"{mode}@{scale}"] = {
                "n": n,
                "correct": k,
                "pct": pct(k / n),
                "mcnemar_p_vs_full": None if p_val is None else round(p_val, 4),
            }
        lines.append(f"{latex_escape(mode)} & " + " & ".join(cells) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines) + "\n", stats


def gen_judged(rows):
    n = len(rows)
    verdicts = {}
    for r in rows:
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
    n_pass = verdicts.get("PASS", 0)
    quality = mean([float(r["quality_score"]) for r in rows])
    safety = mean([float(r["safety_composite_score"]) for r in rows])
    exec_acc = mean([float(r["execution_accuracy"]) for r in rows])
    lines = [
        r"\begin{tabular}{lr}",
        r"\toprule",
        r"Metric & Value \\",
        r"\midrule",
        f"PASS rate & {n_pass}/{n} = {pct(n_pass / n)}\\% \\\\",
        f"\\quad 95\\% CI (Wilson) & {fmt_ci(n_pass, n)} \\\\",
    ]
    for verdict in sorted(verdicts):
        cnt = verdicts[verdict]
        lines.append(
            f"\\quad {latex_escape(verdict)} & {cnt}/{n} = {pct(cnt / n)}\\% \\\\"
        )
    lines += [
        r"\midrule",
        f"Mean execution accuracy & {exec_acc:.3f} \\\\",
        f"Mean quality score & {quality:.3f} \\\\",
        f"Mean safety composite score & {safety:.3f} \\\\",
        r"\bottomrule",
        r"\end{tabular}",
    ]
    stats = {
        "n": n,
        "pass": n_pass,
        "pass_pct": pct(n_pass / n),
        "verdicts": dict(sorted(verdicts.items())),
        "mean_execution_accuracy": round(exec_acc, 3),
        "mean_quality": round(quality, 3),
        "mean_safety": round(safety, 3),
    }
    return "\n".join(lines) + "\n", stats


def load_repeats():
    """Return (list of row-lists, list of paths) if all 3 repeat files are complete, else None."""
    all_rows = []
    for path in REPEAT_CSVS:
        if not path.exists():
            print(
                f"WARNING: {path.name} missing -- repeats table SKIPPED "
                f"(need 3 files x {REPEAT_EXPECTED_ROWS} rows).",
                file=sys.stderr,
            )
            return None
        rows = read_csv(path)
        if len(rows) != REPEAT_EXPECTED_ROWS:
            print(
                f"WARNING: {path.name} has {len(rows)} rows "
                f"(expected {REPEAT_EXPECTED_ROWS}) -- repeats table SKIPPED.",
                file=sys.stderr,
            )
            return None
        all_rows.append(rows)
    return all_rows


def gen_repeats(repeat_rows):
    """Per-repeat exec-match, mean +/- sample SD, bootstrap 95% CI over questions."""
    accs = []
    per_question = {}  # index -> list of 0/1 across repeats
    lines = [
        r"\begin{tabular}{lrr}",
        r"\toprule",
        r"Repeat & $n$ & Exec.\ match \\",
        r"\midrule",
    ]
    for i, rows in enumerate(repeat_rows, start=1):
        n = len(rows)
        k = sum(1 for r in rows if r["exec_match"] == "1")
        accs.append(k / n)
        for r in rows:
            per_question.setdefault(r["index"], []).append(
                1.0 if r["exec_match"] == "1" else 0.0
            )
        lines.append(f"Run {i} & {n} & {k}/{n} = {pct(k / n)}\\% \\\\")

    m = mean(accs)
    var = sum((a - m) ** 2 for a in accs) / (len(accs) - 1)
    sd = math.sqrt(var)

    # Bootstrap over questions: resample question indices with replacement,
    # statistic = mean over resampled questions of their across-repeat mean.
    q_means = [mean(per_question[q]) for q in sorted(per_question)]
    rng = random.Random(BOOTSTRAP_SEED)
    nq = len(q_means)
    boot = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        boot.append(mean([q_means[rng.randrange(nq)] for _ in range(nq)]))
    boot.sort()
    lo = boot[int(0.025 * BOOTSTRAP_RESAMPLES)]
    hi = boot[min(BOOTSTRAP_RESAMPLES - 1, int(0.975 * BOOTSTRAP_RESAMPLES))]

    lines += [
        r"\midrule",
        f"Mean $\\pm$ SD & --- & {pct(m)}\\% $\\pm$ {sd * 100.0:.1f} \\\\",
        f"Bootstrap 95\\% CI ({BOOTSTRAP_RESAMPLES} resamples) & --- & "
        f"[{pct(lo)}\\%, {pct(hi)}\\%] \\\\",
        r"\bottomrule",
        r"\end{tabular}",
    ]
    stats = {
        "per_repeat_pct": [pct(a) for a in accs],
        "mean_pct": pct(m),
        "sd_pct": f"{sd * 100.0:.1f}",
        "bootstrap_ci_pct": [pct(lo), pct(hi)],
    }
    return "\n".join(lines) + "\n", stats


def gen_totals(counts):
    """counts: sorted-by-key dict of display-path -> row count."""
    total = sum(counts.values())
    lines = [
        r"\begin{tabular}{lr}",
        r"\toprule",
        r"Result file & Evaluated queries \\",
        r"\midrule",
    ]
    for name in sorted(counts):
        lines.append(f"\\texttt{{{latex_escape(name)}}} & {counts[name]} \\\\")
    lines += [
        r"\midrule",
        f"\\textbf{{Total}} & \\textbf{{{total}}} \\\\",
        r"\bottomrule",
        r"\end{tabular}",
    ]
    payload = {
        "citable_files": {k: counts[k] for k in sorted(counts)},
        "total_evaluated_queries": total,
    }
    return "\n".join(lines) + "\n", payload


# ---------------------------------------------------------------- driver

def generate(out_dir):
    """Generate all outputs into out_dir. Returns dict of filename -> bytes."""
    outputs = {}

    bird = read_csv(BIRD_CSV)
    tex, _ = gen_bird_headline(bird)
    outputs["bird_headline.tex"] = tex
    tex, _ = gen_bird_by_difficulty(bird)
    outputs["bird_by_difficulty.tex"] = tex
    tex, _ = gen_bird_by_db(bird)
    outputs["bird_by_db.tex"] = tex

    rows_by_scale = {scale: read_csv(path) for scale, path in ABLATION_CSVS.items()}
    tex, _ = gen_ablation(rows_by_scale)
    outputs["ablation_modes.tex"] = tex

    judged = read_csv(JUDGED_CSV)
    tex, _ = gen_judged(judged)
    outputs["judged_pass.tex"] = tex

    counts = {
        "bench/results_bird_gpt55_full_dev.scored.csv": len(bird),
        "bench/results_ablation_100.csv": len(rows_by_scale[107]),
        "bench/results_ablation_500.csv": len(rows_by_scale[502]),
        "results_v2_full_judged.csv": len(judged),
    }

    repeats = load_repeats()
    if repeats is not None:
        tex, _ = gen_repeats(repeats)
        outputs["repeats.tex"] = tex
        for i, rows in enumerate(repeats, start=1):
            counts[f"bench/results_repeat_full_r{i}.csv"] = len(rows)

    tex, payload = gen_totals(counts)
    outputs["totals.tex"] = tex
    outputs["totals.json"] = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, content in sorted(outputs.items()):
        (out_dir / name).write_bytes(content.encode("utf-8"))
    return {name: content.encode("utf-8") for name, content in outputs.items()}


def check():
    """Regenerate into a temp dir; exit 1 if anything differs from paper_tables/."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        fresh = generate(Path(tmp))
    if not OUT_DIR.is_dir():
        print(f"CHECK FAILED: {OUT_DIR} does not exist.", file=sys.stderr)
        return 1
    existing = {p.name: p.read_bytes() for p in sorted(OUT_DIR.iterdir()) if p.is_file()}
    rc = 0
    for name in sorted(set(fresh) | set(existing)):
        if name not in existing:
            print(f"CHECK FAILED: {name} missing from {OUT_DIR}.", file=sys.stderr)
            rc = 1
        elif name not in fresh:
            print(f"CHECK FAILED: stale file {name} present in {OUT_DIR}.", file=sys.stderr)
            rc = 1
        elif fresh[name] != existing[name]:
            print(f"CHECK FAILED: {name} differs from regenerated output.", file=sys.stderr)
            rc = 1
    if rc == 0:
        print(f"CHECK OK: {len(fresh)} files in {OUT_DIR} are up to date.")
    return rc


def main(argv):
    if "--check" in argv:
        return check()
    outputs = generate(OUT_DIR)
    for name in sorted(outputs):
        print(f"wrote {OUT_DIR / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
