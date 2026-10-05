"""Local real-database integration; this adapter has no Snowflake certification."""
import asyncio
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from tests.test_governed_runtime import Provider


def test_real_adapter_filters_metadata_executes_bound_values_and_refreshes_narration(tmp_path):
    import sqlite3
    from adapters import LegacyAdapter
    from execution_context import ExecutionContext
    from governed_runtime import GovernedRuntime
    from governed_store import GovernedStore
    path=tmp_path/'fixture.db'
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE orders (id INTEGER, amount INTEGER, tenant_id TEXT); CREATE TABLE secret (password TEXT); INSERT INTO orders VALUES (1,350,'acme'),(2,999,'beta');")
    intent=json.dumps(dict(metrics=[],dimensions=[],objects=['orders'],expressions=['SUM(amount) AS total'],
        filters=[{'field':'tenant_id','value':'acme'}],time_boundaries={},grain='total',unresolved=[]))
    sql="SELECT SUM(amount) AS total FROM orders WHERE tenant_id='acme'"
    async def run():
        adapter=LegacyAdapter(f'sqlite+aiosqlite:///{path}')
        store=GovernedStore(str(tmp_path/'s.db'))
        provider=Provider([intent,sql,'350',intent,'400'])
        runtime=GovernedRuntime(provider,adapter,store)
        def ctx(): return ExecutionContext('alice','acme','sqlite','','','','v1',frozenset({'orders'}),dialect='sqlite')
        metadata=await adapter.metadata(ctx())
        assert set(metadata)=={'orders'}
        first=await runtime.query('sum amount for acme',ctx(),'first')
        assert first['status']=='answered' and first['data']['rows']==[[350]]
        with sqlite3.connect(path) as conn: conn.execute("UPDATE orders SET amount=400 WHERE tenant_id='acme'")
        second=await runtime.query('sum amount for acme',ctx(),'second')
        assert second['status']=='answered' and second['data']['rows']==[[400]]
        assert second['response']=='400' and second['metrics']['cache_hit']
        assert 'secret' not in provider.calls[0][0]['content']
        await adapter.engine.dispose()
    asyncio.run(run())
