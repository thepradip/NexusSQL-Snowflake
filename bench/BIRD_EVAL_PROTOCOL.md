# BIRD Evaluation Protocol

This repository uses BIRD dev `dev_20240627` for paper-grade text-to-SQL
evaluation. Smoke-test scores are not reported as benchmark scores.

## Publishable Claims

- Full BIRD dev: report as `BIRD dev execution accuracy` and VES-style mean.
- Fixed subset: report only as `fixed stratified BIRD dev subset`, including the
  manifest filename, seed, exact question count, and manifest checksum.
- Never report a 22/55-question smoke result as the BIRD score.

## Reproducible Inputs

Generate manifests:

```bash
python3 bench/make_bird_manifest.py --kind full
python3 bench/make_bird_manifest.py --kind stratified --size 300 --seed 20260614
```

The manifest records:

- BIRD source path
- source `dev.json` SHA-256
- question ids in fixed order
- database and difficulty distribution
- seed and selection algorithm

## Run

Stratified 300:

```bash
python3 bench/run_bird_model.py \
  --manifest bench/manifests/bird_dev_stratified300_seed20260614.json \
  --out bench/results_bird_gpt55_stratified300.csv \
  --max-repairs 1 \
  --timeout 30
```

Full dev:

```bash
python3 bench/run_bird_model.py \
  --manifest bench/manifests/bird_dev_full.json \
  --out bench/results_bird_gpt55_full_dev.csv \
  --max-repairs 1 \
  --timeout 30
```

Both runs are resumable by `question_id`.

## Score

```bash
python3 bench/score_bird.py \
  bench/results_bird_gpt55_stratified300.csv \
  --out bench/results_bird_gpt55_stratified300.scored.csv \
  --repeats 3
```

```bash
python3 bench/score_bird.py \
  bench/results_bird_gpt55_full_dev.csv \
  --out bench/results_bird_gpt55_full_dev.scored.csv \
  --repeats 3
```

## Audit Requirements

Keep all of these artifacts:

- manifest JSON
- raw generation CSV
- scored CSV
- exact command lines
- model/deployment name
- scorer version / git commit if available
- miss analysis by database and difficulty

The generation harness must not include gold SQL in prompts. The runner stores
gold SQL only after generation so the scorer can compare execution results.

## Frozen Configuration (2026-07-05, pre-submission campaign)

All paper-cited runs from this date use exactly this configuration:

- Model: the Azure deployment named by `AZURE_OPENAI_DEPLOYMENT_NAME`
  (currently `gpt-5.5`, API version `2025-01-01-preview`). The paper must
  cite this deployment string — one canonical model name everywhere; no
  informal variants.
- Generation: `run_bird_model.py` defaults — `max_tokens` 2200 for SQL
  generation, `--max-repairs 1`, `--timeout 30`. Temperature is the
  deployment default (not set by the harness).
- Scoring: `score_bird.py --repeats 3` (VES timing uses 3 repeats;
  SQL generation itself is single-shot).
- Code state: runs are valid only from the `paper-acceptance` branch at or
  after the commit "Fix benchmark-gating bugs in the runtime layer"
  (single SQL execution per tool call; strict ablation retrieval modes).
  Record `git rev-parse HEAD` alongside every results CSV.
- LargeSchemaEval runs use the repaired `questions_*_v2.json` gold sets
  (see `bench/repair_gold.py` and `gold_repair_changelog_*.json`);
  the v1 sets contain broken gold and are not citable.
- Every completed run must be registered in `bench/RESULTS_INDEX.md` with
  its config and citable status before its numbers appear in any document.
- Retrieval-ablation runs must log `retrieval_mode_actual` per row; a row
  whose actual mode differs from the requested mode invalidates the run.
- 10-query harness pilot (2026-07-05, post-fix): 9/10 execution match,
  VES 0.8145 (`bench/results_bird_pilot_postfix.csv` — smoke, not citable).
