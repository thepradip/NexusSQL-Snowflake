"""Run Vanna 2.0 (autonomous agentic mode) over merged_100 questions -> results_vanna_100.csv (resumable).
Vanna explores schema via run_sql; we capture the final answer SQL (last non-introspection SELECT)."""
import os, sys, csv, json, asyncio, time
from dotenv import load_dotenv

REPO = "/Users/pradip/Desktop/Learning/Claude/Infogain"
# BENCH_SET selects the LargeSchemaEval scale point; v2 = repaired gold sets
SET = os.environ.get("BENCH_SET", "100")
DB = f"{REPO}/large_schema_eval_v2/merged_{SET}.sqlite"
load_dotenv(f"{REPO}/.env", override=False)
LIMIT = int(os.environ.get("BENCH_LIMIT", "0"))
OUT = os.environ.get("BENCH_OUT", f"{REPO}/bench/results_vanna_{SET}_v2.csv")

from vanna import Agent, AgentConfig, ToolRegistry, User
from vanna.core.user.resolver import UserResolver
from vanna.core.user.request_context import RequestContext
from vanna.core.system_prompt.default import DefaultSystemPromptBuilder
from vanna.integrations.azureopenai import AzureOpenAILlmService
from vanna.integrations.sqlite import SqliteRunner
from vanna.integrations.local.agent_memory import DemoAgentMemory
from vanna.tools import RunSqlTool

EXECUTED = []
_orig = SqliteRunner.run_sql
def _patched(self, args, context):
    EXECUTED.append(getattr(args, "sql", None) or str(args))
    return _orig(self, args, context)
SqliteRunner.run_sql = _patched

def is_introspect(sql):
    s = (sql or "").lower()
    return "sqlite_master" in s or s.strip().startswith("pragma")

class FixedResolver(UserResolver):
    async def resolve_user(self, request_context):
        return User(id="u1", username="bench", email="b@x.com", group_memberships=["admin","user"])

BASE_PROMPT = (
    "You are an expert SQL analyst for a SQLite database. NEVER ask the user clarifying "
    "questions. To answer a question, FIRST inspect the schema using the run_sql tool "
    "(e.g. `SELECT sql FROM sqlite_master WHERE type='table'` to see every table and its "
    "columns), THEN run exactly one final SELECT that answers the question. Table names are "
    "prefixed like '<sourcedb>_<table>' (e.g. sakila_1_customer). Always finish by running the "
    "answer query with run_sql; do not ask for confirmation."
)

def build_agent():
    # Vanna's Azure integration speaks classic Azure style; a v1-style
    # endpoint (.../openai/v1) must be reduced to the resource root or
    # every call 404s.
    _ep = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    if _ep.endswith("/openai/v1"):
        _ep = _ep[: -len("/openai/v1")]
    llm = AzureOpenAILlmService(
        model=os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        azure_endpoint=_ep,
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
    )
    reg = ToolRegistry()
    reg.register_local_tool(RunSqlTool(sql_runner=SqliteRunner(database_path=DB)), access_groups=["admin","user"])
    return Agent(llm_service=llm, tool_registry=reg, user_resolver=FixedResolver(),
                 agent_memory=DemoAgentMemory(max_items=10000),
                 system_prompt_builder=DefaultSystemPromptBuilder(base_prompt=BASE_PROMPT),
                 config=AgentConfig(stream_responses=False, include_thinking_indicators=False))

async def main():
    agent = build_agent()
    qs = json.load(open(f"{REPO}/large_schema_eval_v2/questions_{SET}_v2.json"))
    if LIMIT: qs = qs[:LIMIT]
    done = set()
    if os.path.exists(OUT):
        for r in csv.DictReader(open(OUT)): done.add(int(r["idx"]))
    new = not os.path.exists(OUT)
    f = open(OUT, "a", newline=""); w = csv.writer(f)
    if new: w.writerow(["idx","source_db","difficulty","question","gold_sql","gen_sql","n_sql","error","latency_ms"]); f.flush()
    for i, q in enumerate(qs):
        if i in done: continue
        EXECUTED.clear(); t = time.time(); err = ""
        try:
            async def run():
                async for _ in agent.send_message(RequestContext(metadata={}, remote_addr="127.0.0.1"),
                                                   q["question"], conversation_id=f"q{i}"):
                    pass
            await asyncio.wait_for(run(), timeout=120)
        except Exception as e:
            err = f"EXC:{type(e).__name__}:{e}"[:200]
        answers = [s for s in EXECUTED if not is_introspect(s)]
        gen = answers[-1] if answers else ""
        w.writerow([i, q.get("source_db"), q.get("difficulty"), q["question"], q["gold_sql"], gen, len(EXECUTED), err, round((time.time()-t)*1000)]); f.flush()
        print(f"[vanna {i+1}/{len(qs)}] {q.get('difficulty')} nsql={len(EXECUTED)} ans={'Y' if gen else 'N'} err={'Y' if err else 'N'}", flush=True)
    f.close()
    print("VANNA_DONE", flush=True)

asyncio.run(main())
