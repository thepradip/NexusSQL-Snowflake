# Results Index — Authoritative Map of Every Result File

**Rule: a number goes into the paper ONLY if its source file is marked
`CITABLE: yes` here.** Update this table whenever a run completes.
(Created 2026-07-05 during the pre-submission audit; row counts and metrics
were recomputed from the files, not copied from prior reports.)

## Top-level CSVs (SQLAS merged large-schema evaluation, synthetic Spider-derived DBs)

| File | Rows | Config | Headline metric | CITABLE | Notes |
|---|---|---|---|---|---|
| `../results_100.csv` | 1,200 | 4 retrieval modes × 300q, merged_100 (107 tables) | mean exec_acc 0.663 | partial | exec_acc / latency / schema-F1 / safety columns OK. `verdict`+`quality_score` NOT LLM-judged (structural proxy ≤0.23 → all FAIL_QUALITY). Gold set has 48/281 broken queries → re-run planned on merged_100_v2 |
| `../results_200.csv` | 2,248 | 4 retrieval modes × 562q, merged_200 (202 tables) | mean exec_acc 0.679 | partial | same caveats as results_100.csv |
| `../results_full_schema_injection_200.csv` | 200 | full-schema-injection baseline, 200 tables | exec_acc 0.827 | yes | the valid FSI baseline |
| `archive/INVALID_results_full_schema_200.csv` | 200 | failed FSI run | exec_acc 0.000 (all FAIL_NO_SQL) | **NO** | broken run, quarantined — see `archive/README.md` |
| `../results_v2_full_judged.csv` | 100 | `full` mode, LLM-judged (SQLAS 3-dim verdict) | PASS 76/100 (76.0%), exec_acc 0.744 | yes | the ONLY top-level LLM-judged run; source for the paper's PASS-rate claims. Reconcile the "100 vs 57" denominator in the tex against THIS file |

## Top-level BIRD CSVs (older 12-column harness)

| File | Rows | Config | Metric | CITABLE | Notes |
|---|---|---|---|---|---|
| `../results_bird_correct.csv` | 238 | BIRD dev subset, 30/DB, zero-shot | exec_acc 0.425, schema-F1 0.860 | superseded | replaced by bench/ stratified-300 (0.607) and the pending full-dev run |
| `../results_bird_full.csv` | 210 | BIRD dev subset | exec_acc 0.420 | superseded | |
| `../results_bird_full.csv.partial` | 210 | — | — | **NO** | byte-identical to results_bird_full.csv; interrupted-run artifact |

## bench/ CSVs (protocol-driven BIRD dev, gpt-5.2 via Azure; scored by `score_bird.py`)

| File | Rows | Config | execution_match | VES | CITABLE | Notes |
|---|---|---|---|---|---|---|
| `results_bird_gpt55_stratified300.scored.csv` | 300 | stratified BIRD dev sample, manifest-pinned | 182/300 = 60.7% | 0.643 | yes (until full-dev lands) | current best BIRD number |
| `results_bird_gpt55_stratified300.partial.scored.csv` | 251 | mid-run snapshot | 61.4% | 0.665 | **NO** | superseded by the 300-row final |
| `results_bird_gpt55_balanced55.scored.csv` | 55 | smoke | 76.4% | 0.767 | **NO** | per BIRD_EVAL_PROTOCOL.md, small smoke runs are never reported as the BIRD score |
| `results_bird_gpt55_balanced22.scored.csv` | 22 | smoke | 68.2% | 0.664 | **NO** | smoke |
| `results_bird_gpt55_balanced22_v2.scored.csv` | 22 | smoke | 86.4% | 0.838 | **NO** | smoke |
| `results_bird_gpt55_pilot*` / `miss_rerun*` | 8–22 | debug re-runs | — | — | **NO** | iterative debugging artifacts |
| results_bird_gpt55_full_dev.scored.csv | 1,534 | **PLANNED** — manifest `manifests/bird_dev_full.json` (sha256-pinned) | — | — | pending | Phase 3.1: replaces every BIRD number above |

## bench/ baseline agents (shared 100-query set on `large_schema_eval_v2/merged_100.sqlite`, scored by `score.py`, n=233 after excluding 48 broken gold)

| File | Agent | Exec acc | CITABLE | Notes |
|---|---|---|---|---|
| `results_nexus_100.csv` | NexusSQL fast | 95/233 = 40.8% | after gold repair | re-score on merged_100_v2 |
| `results_nexus_agentic_100.csv` | NexusSQL ReAct | 96/233 = 41.2% | after gold repair | re-score on merged_100_v2 |
| `results_nexus_fewshot_100.csv` | NexusSQL few-shot | 98/233 = 42.1% | after gold repair | re-score on merged_100_v2 |
| `results_vanna_100.csv` | Vanna 2.0 | 118/233 = 50.6% | after gold repair | honest secondary baseline; paper reports this with the scale-crossover experiment (Phase 3.4) |

## Post-fix campaign runs (2026-07-05, paper-acceptance branch)

| File | Rows | Config | Headline | CITABLE | Notes |
|---|---|---|---|---|---|
| `results_ablation_100.csv` | 1,120 | 4 retrieval modes × 280q, merged_100 v2 golds, fast pipeline, caches off, gpt-5.5 | strict exec-match: full 51.8%, rrf 51.8%, dense_only 51.4%, bm25_only 51.1%; median latency ≈ 5.5–6.0s | **yes** | REPLACES results_100.csv. 0 provenance violations (`retrieval_mode_actual` == requested on all rows), 5 error rows (bad generated SQL, counted as miss). Metric is strict binary execution match — NOT comparable to the old partial-credit execution_accuracy. schema_f1 (0.25–0.33) is over the untrimmed top-8 retrieval, not the adaptive-threshold set — do not compare to the old ~0.89 figure. Code state: `results_ablation_100.gitrev` |
| `results_bird_gpt55_full_dev.csv` | RUNNING | BIRD full dev 1,534q, manifest-pinned | — | pending | replaces every earlier BIRD number when scored |

## Canonical query totals

Do **not** hand-sum totals into the paper ("3,686 vs 3,448" bug). The total
evaluated-query count must be computed by `make_paper_tables.py` (Phase 4)
from the rows of CITABLE files only.
