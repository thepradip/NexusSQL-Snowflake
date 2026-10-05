# NexusSQL Snowflake implementation ledger
Source: user supplied Robust NL2SQL for Snowflake plan, 2026-10-05.
Base: b3351269. Branch: feature/snowflake-trusted-boundary.
Tasks: trusted context/auth; isolated adapters; semantic grounding; adaptive validation; scoped reuse; API/UI; offline acceptance and rollout.
Pre-flight: all stages consume ExecutionContext. Legacy global schema/tools/cache cannot safely serve authenticated requests.
Ruling: introduce a governed runtime for API queries, retaining legacy modules for offline experiments. All API generation strategies share the same runtime boundary; production cannot opt back into legacy global execution.
Ruling: business adjudication and live staging cannot be invented. Ship reproducible fixtures and pending adjudication manifest; certify only after external acceptance.
Git: direct CommandLineTools binary works; original checkout has existing changes and is preserved.

Implementation record (2026-10-05):
- Trusted execution context/auth/admin boundary complete. Legacy API global execution removed; original experiment modules remain unchanged.
- Snowflake adapter implemented and connector contract/cancellation tests passed. Isolated role sessions, bound literals, metadata/EXPLAIN/submission polling/cancel, bounded workers/rows/bytes and deadlines implemented.
- Reviewed semantic templates, authorized SchemaIndex retrieval, scoped clarification and versioned conversation history implemented. Unknown/ambiguous business definitions clarify. Generic query source/projection/predicate/date/grain/distinct/limit contracts fail closed.
- Validation covers all governed strategies. ReAct structured results are local to request; completion without execution blocks; narration uses actual returned data. Positive function allowlist and reviewed join/lineage checks enforced.
- SQL reuse and reviewed examples are scoped. No application result caching. Revalidate/reexecute/renarrate on exact reuse. Single-entry invalidation; feedback does not certify examples.
- API/query-stream contract and UI clarification/definitions/table/truncation/freshness implemented. Plain/SSE disconnect cancellation tested. Stale UI schema requests aborted.
- 200-case deterministic fixture/holdout plus opt-in SQLAS staging harness shipped. Cross-dialect offline reference-result sanity check passed. Human adjudication remains pending; no live Snowflake acceptance was run.
- Independent review found join Boolean bypass, missing generic intent checks, incomplete cancellation and parser-known function gaps. Regression tests reproduced each; fixes verified.
- Ruling: API legacy evaluation/drift paths now return explicit pending configuration status instead of allowing legacy unscoped execution. Governed live drift baseline remains a staging prerequisite.
- Ruling: generic complex semantics block; reviewed exact templates can preserve approved preaggregation join keys with verified physical-column lineage. Accuracy/coverage gates must be measured live; no certification inferred from unit tests.
- Final validation: python -m pytest -q --timeout=30 => 283 passed, 2 existing/framework warnings. python -m compileall -q backend bench/snowflake => passed. npm --prefix frontend run build => passed. git diff --check => passed.
- Browser fixture checks passed: clarification choices/apply, authenticated conversation ID continuation, table/definitions/truncation/freshness, access key, no horizontal overflow at 320/768/1024/1440. See output/browser-dom-tester/snowflake/report.md. No live backend/model/Snowflake proof.
- External prerequisites: real Snowflake service credentials; reviewed roles, row/masking policies, dedicated warehouse and cost controls; business-approved definitions and adjudicated holdout; staging/shadow/canary gates.
