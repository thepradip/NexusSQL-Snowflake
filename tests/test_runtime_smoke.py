"""
Smoke tests for the runtime agent layer (backend/ + ariasql-package).

These pin the bug fixes that gate the paper's benchmark validity:
  1. Ablation retrieval modes must never silently fall back to another mode
     (dense_only/rrf previously degraded to BM25 with only a log line).
  2. execute_sql must hit the database exactly once per tool call
     (the ReAct loop previously re-executed every query for visualization).
  3. A stale cache hit must flush the cache, not permanently disable it.
  4. The Azure max_completion_tokens fallback must trigger only on a 400
     that actually identifies the parameter as unsupported.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
ARIASQL = ROOT / "ariasql-package" / "ariasql"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Fakes ──────────────────────────────────────────────────────────────────────

class FakeSettings:
    max_context_tables = 2
    retrieval_mode = "full"
    disable_fk_expansion = False
    azure_openai_embedding_deployment = "fake-embed"
    max_columns_per_table = 30


class FakeEmbeddingsClient:
    """Deterministic embeddings: users→x-axis, orders→y-axis, products→z-axis."""

    _AXES = {"users": [1.0, 0.0, 0.0], "orders": [0.0, 1.0, 0.0], "products": [0.0, 0.0, 1.0]}

    def __init__(self, query_vector):
        self._query_vector = query_vector
        self.embeddings = self

    def create(self, model, input):
        # Table docs (from _describe_table) start with "table <name>";
        # anything else is a user query and gets the configured query vector.
        for key, vec in self._AXES.items():
            if input.startswith(f"table {key}"):
                return types.SimpleNamespace(data=[types.SimpleNamespace(embedding=vec)])
        return types.SimpleNamespace(data=[types.SimpleNamespace(embedding=self._query_vector)])


SCHEMA = {
    "users": {
        "columns": [{"name": "id", "type": "INT", "nullable": False},
                    {"name": "name", "type": "TEXT", "nullable": True}],
        "foreign_keys": [],
        "primary_key": ["id"],
        "row_count": 10,
    },
    "orders": {
        "columns": [{"name": "id", "type": "INT", "nullable": False},
                    {"name": "user_id", "type": "INT", "nullable": False}],
        "foreign_keys": [{"columns": ["user_id"], "referred_table": "users",
                          "referred_columns": ["id"]}],
        "primary_key": ["id"],
        "row_count": 50,
    },
    "products": {
        "columns": [{"name": "id", "type": "INT", "nullable": False},
                    {"name": "price", "type": "REAL", "nullable": True}],
        "foreign_keys": [],
        "primary_key": ["id"],
        "row_count": 5,
    },
}


def _build_index(monkeypatch, tmp_path, query_vector=None, embeddings=True):
    pytest.importorskip("networkx")
    monkeypatch.chdir(tmp_path)  # keep .schema_embed_cache.json out of the repo
    schema_index = _load_module(ARIASQL / "schema_index.py", "ariasql_schema_index_smoke")
    settings = FakeSettings()
    if not embeddings:
        settings.azure_openai_embedding_deployment = None
    client = FakeEmbeddingsClient(query_vector or [0.0, 1.0, 0.0])
    idx = schema_index.SchemaIndex(client, settings)
    asyncio.run(idx.build(SCHEMA, {}))
    return idx, settings


# ── 1. Retrieval ablation modes (ariasql-package, canonical for experiments) ──

def test_bm25_only_ranks_by_lexical_match(monkeypatch, tmp_path):
    idx, settings = _build_index(monkeypatch, tmp_path)
    settings.retrieval_mode = "bm25_only"
    tables = idx.retrieve_relevant_tables("show all users by name")
    assert tables[0] == "users"
    assert idx.last_retrieval_mode_actual == "bm25_only"


def test_dense_only_uses_embeddings_not_bm25(monkeypatch, tmp_path):
    # Query embedding points at "orders" while its text matches "users":
    # a real dense ranking must disagree with BM25.
    idx, settings = _build_index(monkeypatch, tmp_path, query_vector=[0.0, 1.0, 0.0])
    settings.retrieval_mode = "dense_only"
    tables = idx.retrieve_relevant_tables("show all users by name")
    assert tables[0] == "orders"
    assert idx.last_retrieval_mode_actual == "dense_only"

    settings.retrieval_mode = "bm25_only"
    assert idx.retrieve_relevant_tables("show all users by name")[0] == "users"


def test_dense_only_without_embeddings_is_a_hard_error(monkeypatch, tmp_path):
    idx, settings = _build_index(monkeypatch, tmp_path, embeddings=False)
    settings.retrieval_mode = "dense_only"
    with pytest.raises(RuntimeError, match="dense retrieval requested"):
        idx.retrieve_relevant_tables("show all users")


def test_rrf_without_embeddings_is_a_hard_error(monkeypatch, tmp_path):
    idx, settings = _build_index(monkeypatch, tmp_path, embeddings=False)
    settings.retrieval_mode = "rrf"
    with pytest.raises(RuntimeError, match="dense retrieval requested"):
        idx.retrieve_relevant_tables("show all users")


def test_rrf_fuses_and_skips_fk_expansion(monkeypatch, tmp_path):
    idx, settings = _build_index(monkeypatch, tmp_path, query_vector=[0.0, 1.0, 0.0])
    settings.retrieval_mode = "rrf"
    tables = idx.retrieve_relevant_tables("show all users by name", top_k=2)
    assert set(tables) == {"users", "orders"}
    assert idx.last_retrieval_mode_actual == "rrf"


def test_full_mode_expands_fk_neighbors(monkeypatch, tmp_path):
    idx, settings = _build_index(monkeypatch, tmp_path, query_vector=[0.0, 1.0, 0.0])
    tables = idx.retrieve_relevant_tables("orders placed", top_k=1)
    assert "users" in tables  # FK neighbor of orders
    assert idx.last_retrieval_mode_actual == "full"


def test_full_mode_records_bm25_fallback_provenance(monkeypatch, tmp_path):
    idx, settings = _build_index(monkeypatch, tmp_path, embeddings=False)
    tables = idx.retrieve_relevant_tables("show all users")
    assert tables
    assert idx.last_retrieval_mode_actual == "full[bm25_fallback]"


def test_full_mode_respects_disable_fk_expansion(monkeypatch, tmp_path):
    idx, settings = _build_index(monkeypatch, tmp_path, embeddings=False)
    settings.disable_fk_expansion = True
    tables = idx.retrieve_relevant_tables("orders placed", top_k=1)
    assert tables == ["orders"]


def test_retrieve_async_delegates_to_sync(monkeypatch, tmp_path):
    idx, settings = _build_index(monkeypatch, tmp_path, query_vector=[0.0, 1.0, 0.0])
    settings.retrieval_mode = "dense_only"
    tables = asyncio.run(idx.retrieve_async("show all users"))
    assert tables[0] == "orders"


# ── 2. Single SQL execution per execute_sql tool call (backend/) ──────────────

@pytest.fixture()
def backend_tools(monkeypatch):
    calls = []

    async def fake_execute_readonly_query(sql):
        calls.append(sql)
        return {"execution_time_ms": 1, "row_count": 2, "columns": ["a"],
                "rows": [[1], [2]], "truncated": False}

    async def fake_get_full_schema():
        return SCHEMA

    async def fake_get_all_col_stats_cached(schema):
        return {}

    fake_db = types.ModuleType("database")
    fake_db.execute_readonly_query = fake_execute_readonly_query
    fake_db.get_full_schema = fake_get_full_schema
    fake_db.get_all_col_stats_cached = fake_get_all_col_stats_cached
    monkeypatch.setitem(sys.modules, "database", fake_db)

    fake_viz = types.ModuleType("visualization_client")

    async def infer_visualization(user_query, result, response):
        return None

    fake_viz.infer_visualization = infer_visualization
    monkeypatch.setitem(sys.modules, "visualization_client", fake_viz)

    monkeypatch.syspath_prepend(str(BACKEND))
    for name in ("tools", "react_agent", "llm_providers"):
        sys.modules.pop(name, None)
    import tools  # noqa: F401  (backend/tools.py via injected fakes)
    yield tools, calls
    for name in ("tools", "react_agent", "llm_providers"):
        sys.modules.pop(name, None)


def test_execute_sql_tool_hits_db_once_and_captures_result(backend_tools):
    tools, calls = backend_tools
    out = asyncio.run(tools.execute_tool("execute_sql", {"sql": "SELECT 1"}))
    assert "2 rows" in out
    assert calls == ["SELECT 1"]
    assert tools.last_sql_result["row_count"] == 2


def test_execute_sql_tool_clears_stale_result_on_empty_sql(backend_tools):
    tools, calls = backend_tools
    asyncio.run(tools.execute_tool("execute_sql", {"sql": "SELECT 1"}))
    asyncio.run(tools.execute_tool("execute_sql", {"sql": ""}))
    assert tools.last_sql_result is None
    assert calls == ["SELECT 1"]


def test_react_loop_executes_sql_exactly_once(backend_tools):
    pytest.importorskip("mlflow")
    tools, calls = backend_tools
    from react_agent import run_react_query

    class FakeProvider:
        name = "fake/test"
        supports_tool_calling = True

        def __init__(self):
            self.turn = 0

        def complete_with_tools(self, messages, tool_defs, max_tokens=2000):
            self.turn += 1
            if self.turn == 1:
                return ({"role": "assistant", "content": ""},
                        [{"id": "1", "name": "create_plan",
                          "arguments": {"tables_needed": ["users"], "sql_approach": "count"}}])
            if self.turn == 2:
                return ({"role": "assistant", "content": ""},
                        [{"id": "2", "name": "execute_sql",
                          "arguments": {"sql": "SELECT COUNT(*) FROM users"}}])
            return ({"role": "assistant", "content": ""},
                    [{"id": "3", "name": "final_answer",
                      "arguments": {"answer": "10 users", "sql": "SELECT COUNT(*) FROM users"}}])

    result = asyncio.run(run_react_query(
        user_query="how many users?",
        provider=FakeProvider(),
        schema_overview="",
        conversation_history=[],
    ))
    assert result["success"] is True
    assert calls == ["SELECT COUNT(*) FROM users"]  # exactly once, not twice
    assert result["data"] is not None
    assert result["data"]["row_count"] == 2


# ── 3. Query cache survives a stale hit (clear_all keeps it usable) ───────────

def test_query_cache_clear_all_keeps_cache_alive(tmp_path):
    query_cache = _load_module(BACKEND / "query_cache.py", "backend_query_cache_smoke")
    cache = query_cache.QueryCache(db_path=str(tmp_path / "qc.db"))

    cache.store("how many users?", "SELECT COUNT(*) FROM users", "10 users")
    assert cache.lookup("how many users?") is not None

    cache.clear_all()
    assert cache.size() == 0
    assert cache.lookup("how many users?") is None

    cache.store("how many users?", "SELECT COUNT(*) FROM users_v2", "12 users")
    hit = cache.lookup("how many users?")
    assert hit is not None and hit.sql == "SELECT COUNT(*) FROM users_v2"


# ── 4. Azure max_completion_tokens fallback triggers only on real signal ──────

@pytest.fixture()
def azure_provider_cls():
    llm_providers = _load_module(BACKEND / "llm_providers.py", "backend_llm_providers_smoke")
    return llm_providers.AzureOpenAIProvider


class _FakeHTTPError(Exception):
    def __init__(self, message, body=None):
        super().__init__(message)
        self.body = body


def test_rejects_max_completion_tokens_via_structured_param(azure_provider_cls):
    exc = _FakeHTTPError("bad request", body={"error": {
        "code": "unsupported_parameter", "param": "max_completion_tokens",
        "message": "Unsupported parameter"}})
    assert azure_provider_cls._rejects_max_completion_tokens(exc) is True


def test_rejects_max_completion_tokens_via_message_fallback(azure_provider_cls):
    exc = _FakeHTTPError(
        "Unsupported parameter: 'max_completion_tokens' is not supported with this model.")
    assert azure_provider_cls._rejects_max_completion_tokens(exc) is True


def test_unrelated_bad_request_is_not_treated_as_fallback(azure_provider_cls):
    exc = _FakeHTTPError("bad request", body={"error": {
        "code": "context_length_exceeded", "param": "messages",
        "message": "Your input is too long"}})
    assert azure_provider_cls._rejects_max_completion_tokens(exc) is False


def test_chat_create_retries_with_max_tokens_on_unsupported_param(azure_provider_cls):
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")

    request = httpx.Request("POST", "https://example.test/chat/completions")
    response = httpx.Response(400, request=request)
    body = {"error": {"code": "unsupported_parameter",
                      "param": "max_completion_tokens", "message": "Unsupported parameter"}}
    err = openai.BadRequestError("Unsupported parameter", response=response, body=body)

    seen = []

    class FakeCompletions:
        def create(self, **kwargs):
            seen.append(dict(kwargs))
            if "max_completion_tokens" in kwargs:
                raise err
            return types.SimpleNamespace(choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content="ok", tool_calls=None))])

    provider = object.__new__(azure_provider_cls)
    provider._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=FakeCompletions()))
    provider._deployment = "test-deploy"

    result = provider.complete([{"role": "user", "content": "hi"}], max_tokens=123)
    assert result == "ok"
    assert len(seen) == 2
    assert seen[0]["max_completion_tokens"] == 123
    assert "max_completion_tokens" not in seen[1]
    assert seen[1]["max_tokens"] == 123


def test_chat_create_does_not_retry_network_style_errors(azure_provider_cls):
    class FakeCompletions:
        def create(self, **kwargs):
            raise ConnectionError("connection reset (mentions max_completion_tokens!)")

    provider = object.__new__(azure_provider_cls)
    provider._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=FakeCompletions()))
    provider._deployment = "test-deploy"

    with pytest.raises(ConnectionError):
        provider.complete([{"role": "user", "content": "hi"}])
