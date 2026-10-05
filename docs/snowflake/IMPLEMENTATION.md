# NexusSQL Snowflake governed runtime

The API now uses a server-created execution context throughout understanding, authorized metadata retrieval, clarification, generation, validation, compilation, execution, result verification and narration. The original experimental modules remain for offline experiments; API requests cannot select their global executor.

## Verified implementation and limits

Implemented: credential rejection, explicit local anonymous mode, separate administration key, tenant/principal conversations with generated IDs, scoped definitions/examples/SQL cache/feedback, schema and definition versions, request budgets, Snowflake connector sessions, bound literals, asynchronous query submission and polling, owned-ID cancellation (including metadata/EXPLAIN), HTTP disconnect cancellation, typed status responses and genuine completed-stage SSE. Snowflake queries never use application result caching. SQL cache hits are revalidated, executed and narrated from fresh results. Failed cached entries are individually invalidated. Thumbs-up feedback does not approve examples.

Metadata, examples and training evidence cannot escape the effective tenant/principal/connection/role/policy scope. Authorized schema retrieval reuses the existing SchemaIndex, hybrid retrieval when an embedding deployment is configured, and reviewed relationship edges. Disk embedding caching is disabled in this runtime. Every selected table retains all columns. Required definition objects, including bridge tables, survive context reduction. Statistics and raw sample rows are never retrieved. Unreviewed training DDL/documents are stored as untrusted evidence and excluded from generation; `/semantics` and `/examples/approve` are the explicit review paths.

One AST validator applies to initial SQL, cached SQL, repairs and agent tools. It uses Snowflake identifier rules, resolves column/CTE/alias scope, accepts read-only CTEs/set operations, and rejects writes/session commands, stage access, functions outside an explicit approved builtin allowlist (including parser-known cross-dialect functions), unauthorized objects, unreviewed joins and Boolean join-key bypasses. The SQL is never silently rewritten to remove LIMIT or change business meaning. Execution binds literal values only after validation.

Semantic support is deliberately conservative. Business metrics must match a reviewed parameterized SQL template that fixes formula, grain, dimensions, joins, NULL behavior, dates, timezone and fiscal calendar. Generic single-table projections/aggregations have deterministic projection, population, date, grain, distinctness and limit checks against resolved intent. Derived-table join keys are traced to physical columns and may be used within exact reviewed templates, including reviewed preaggregation. Generic fanout aggregations and unresolvable join lineage block. Complex semantic cases need a reviewed template. Parser support for SQL syntax alone does not mean those cases can be answered. Arbitrary natural-language correctness is not established by model-extracted intent or successful execution.

The ReAct path uses structured local tool results and the same validator/executor. Completion without successful execution blocks. Final tool text and SQL cannot replace the executed result. Narration is generated independently from the executed data, definitions and boundaries. For truncated results or more than 50 rows, narration is a deterministic subset notice; no model-generated whole-population claims are produced. Empty results are valid. Verification currently checks result shape and size; richer metric-specific invariants remain a staging extension.

Existing response fields remain. New fields include `status`, `conversation_id`, `clarification`, `resolved_assumptions`, versions, `validation_summary`, `snowflake_query_id`, and `result_freshness`. Only `answered` has `success=true`. Both query endpoints return the same final envelope. Client-supplied credentials, raw connector exceptions, private planning and reasoning are excluded from responses.

## Setup

Use Python 3.11+ and an isolated environment. Do not copy the original project's `.env` into this worktree.

```bash
cd /Users/pradip/Desktop/Learning/Codex/Research/NexusSQL-Snowflake
python -m venv .venv
.venv/bin/pip install -r backend/requirements.txt pytest
npm --prefix frontend ci
```

Create a server-only profiles file from `profiles.example.json`, restrict it to the service account, and set its absolute path in `CONNECTION_PROFILES_FILE`. `credentials_env` maps connector credential options to environment variable names. Key-pair/OAuth credential options may be supplied by the reviewed server profile; no query body can supply credentials, destinations or roles. Example environment:

```dotenv
APP_ENVIRONMENT=production
ALLOW_ANONYMOUS_LOCAL=false
ADMIN_API_KEY=<random administrator secret>
CONNECTION_PROFILES_FILE=/absolute/server-only/profiles.json
NEXUS_SNOWFLAKE_PASSWORD=<service password from secret manager>
LLM_PROVIDER=azure
AZURE_OPENAI_ENDPOINT=<configured endpoint>
AZURE_OPENAI_API_KEY=<configured model key>
AZURE_OPENAI_DEPLOYMENT_NAME=<existing deployment>
```

Alternative current providers remain selectable with `LLM_PROVIDER` and their existing configuration. No model migration or fine-tuning is included. Azure configuration fields remain required by the existing Settings contract.

Prepare a dedicated staging warehouse, database and fresh `FIXTURE_V1` schema. Review and execute `bench/snowflake/fixture_v1.sql` as a fixture administrator. Review `staging_security.sql` and attach row policies to every tenant table before enabling service credentials. Apply reviewed masking policies to sensitive real columns. Use only warehouse/database/schema USAGE and SELECT grants for tenant roles, with secondary roles disabled. Database row/masking policies are mandatory for shared production data; the application does not inject best-effort row filters. Registration with legacy `row_filters` is rejected.

Start the backend from its own directory, then the UI:

```bash
cd backend
../.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
# In another terminal at the repository root:
npm --prefix frontend run dev
```

The UI access key stays in component memory. HTTPS and an authenticated reverse proxy are required for remote use. Use `/tenants` with `X-Admin-Key` and the tenant example to register server-owned principal, role, connection and object scope. Invalid keys never fall back to anonymous. Anonymous local experimentation requires both `APP_ENVIRONMENT=local` and `ALLOW_ANONYMOUS_LOCAL=true`, plus `LOCAL_ALLOWED_TABLES` as an explicit comma-separated allowlist; administration always requires an admin key.

Submit administrator-reviewed definitions to `/semantics` using `definition.example.json`, and approved examples to `/examples/approve`. Definitions require a review intent so the server can validate a representative rendered template. Add collected revenue with the same `revenue` synonym to produce competing clarification choices. Missing definitions produce a clarification instead of an invented metric. Unsupported grain, date or filter semantics block. Clarification answers must be tied to the original authenticated conversation and current policy/schema/definition versions; stale or foreign IDs are rejected.

```json
{"query":"What is revenue for cohort 1?"}
```

A clarification response contains the server-created conversation and clarification IDs. Submit:

```json
{"query":"What is revenue for cohort 1?","conversation_id":"<returned>","clarification_id":"<returned>","clarification_answers":{"meaning":"booked revenue"}}
```

## Budgets and observability

Defaults: 30 seconds per statement, 10 seconds queue timeout, 90 seconds request deadline, three analytical submissions, 500 returned rows, 10 MiB complete response. Metadata and EXPLAIN do not count as analytical submissions but share statement/request deadlines. Bounded workers limit connector/model concurrency to eight and hold capacity until the actual worker exits. SDK clients receive per-call deadlines without mutating shared provider state. Disconnects set cancellation signals; connector network calls can delay physical cancellation up to the configured network timeout. Connection failures during session initialization close their sessions.

MLflow records stages, tenant/cache scope hashes, request IDs, query IDs, statuses, repair counts, submissions, warehouse wait/execution/fetch latency and cache hits. OpenAI autologging supplies provider-reported token usage where supported; no token-count claim is made for providers that do not expose it. Application request IDs and MLflow trace IDs are separate; returned trace IDs identify the recorded trace and feedback ownership. Tracing is server-internal and needs its own access/retention policy. Model confidence is not reported as measured correctness. No EXPLAIN plan is labeled as a guaranteed dollar estimate.

## Acceptance and rollout

Offline verification commands:

```bash
python -m pytest -q
python bench/snowflake/build_fixture.py
npm --prefix frontend run build
```

`holdout_v1.json` contains 200 reproducible synthetic cases across metrics, joins, fanout, dates/fiscal periods, windows, NULLs, semi-structured data, empty results, ambiguity and follow-ups. Reference results are hand-derived from the synthetic fixture. **Business-owner adjudication is pending**; this is not the requested human-adjudicated or production-certified holdout. Freeze a reviewed version separately from training examples. Do not approve holdout questions as few-shot examples.

The staging harness calls the real `/query` endpoint only when explicitly invoked with a configured API key. It reports accuracy among answered supported questions, coverage of answerable questions, ambiguity handling and SQLAS result similarity separately. Scoring uses captured results and does not execute SQL again. It cannot certify unreviewed cases or deployment safety.

```bash
NEXUS_EVAL_API_KEY=<staging tenant key> python bench/snowflake/evaluate.py \
  --base-url http://127.0.0.1:8000 --output /absolute/evaluation-results.json
```

Required live gates: all isolation and forbidden-operation tests pass against real roles/policies; at least 90% answered correctness, 80% answerable coverage, 95% ambiguity handling; cancellation/concurrency verified; warehouse resource monitor, auto-suspend, usage budget and masking controls reviewed. Conservative current support can reduce coverage; do not lower gates to hide this.

Then use opt-in shadow evaluation and tenant canaries at 5%, 25%, 100%. Measure correctness, failure/clarification/abstention rates, latency, repairs, tokens and warehouse usage at every gate. Roll back generation strategy through the governed pipeline/ReAct router; keep the shared authorization and execution boundary. No Snowflake connection, role/policy deployment, shadow run or canary was performed in this implementation session.

The previous `/evaluate` and drift configuration paths now return explicit pending/not-configured statuses; they do not run legacy unscoped evaluation/execution. Governed drift needs an adjudicated staging baseline. Existing package adapters and experiment modules retain their previous offline behavior and receive no new certification claim.

## Sources

Adapter/session/cancellation design follows [Snowflake Python connector examples](https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-example). Database policy requirements follow [row access policies](https://docs.snowflake.com/en/user-guide/security-row-intro). Production warehouse prerequisites follow [warehouse cost controls](https://docs.snowflake.com/en/user-guide/cost-controlling-controls). The builtin review boundary is grounded in the [Snowflake function catalog](https://docs.snowflake.com/en/sql-reference/functions-all). These sources guide implementation; they do not prove live acceptance.
