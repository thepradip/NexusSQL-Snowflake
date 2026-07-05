# Archived / Invalid Result Files

Files in this directory must **never** be cited in the paper or reports.

## INVALID_results_full_schema_200.csv

Originally `results_full_schema_200.csv` (repo root). A fully **failed**
full-schema-injection (FSI) run: all 200 rows are `FAIL_NO_SQL` with
`execution_accuracy = 0.0` and ~366 ms latency — the model returned no SQL
at all (misconfigured run). The **valid, citable FSI baseline** is
`results_full_schema_injection_200.csv` (repo root; exec_acc ≈ 0.827).

## Known non-authoritative columns in live files

- `results_100.csv` and `results_200.csv` (repo root): the `verdict` and
  `quality_score` columns are **not LLM-judged**. Quality there is a
  structural proxy capped at ~0.23, which is always below the 0.6 PASS
  threshold, so every row reads `FAIL_QUALITY*`. Only `execution_accuracy`,
  latency, schema-retrieval, and safety columns from those files are usable.
  The only top-level LLM-judged run is `results_v2_full_judged.csv`.
- `results_bird_full.csv.partial` (repo root) is byte-identical to
  `results_bird_full.csv` — an interrupted-run artifact, not extra data.

See `bench/RESULTS_INDEX.md` for the authoritative citable-results map.
