import asyncio
import sys
import threading
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from tests.test_trusted_execution import context

class Cursor:
    sfqid = 'query-fixture'
    description = [('TOTAL',)]
    def __init__(self, conn): self.conn = conn
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, params=None): self.conn.executed.append((sql,params))
    def execute_async(self, sql, params=None): self.conn.submitted.append((sql,params))
    def get_results_from_sfqid(self, qid): self.conn.results.append(qid)
    def fetchmany(self, limit):
        self.conn.fetch_sizes.append(limit)
        return [(i,) for i in range(min(limit, 600))]

class Connection:
    def __init__(self):
        self.executed=[]; self.submitted=[]; self.results=[]; self.fetch_sizes=[]; self.closed=False
        self.running = False
    def cursor(self): return Cursor(self)
    def get_query_status_throw_if_error(self, qid): return self.running
    def is_still_running(self, status): return status
    def close(self): self.closed=True


def test_isolated_sessions_role_timeouts_bound_values_and_fetch():
    from adapters import SnowflakeAdapter
    connections, configurations = [], []
    def connect(**kwargs):
        configurations.append(kwargs); conn=Connection(); connections.append(conn); return conn
    adapter = SnowflakeAdapter({'main':{'account':'fixture','warehouse':'DEDICATED','allowed_roles':['ANALYST']}},connect)
    result=asyncio.run(adapter.execute('SELECT %(value)s',context(),{'value':'US'}))
    asyncio.run(adapter.execute('SELECT 2',context()))
    assert len(connections)==2 and all(c.closed for c in connections)
    assert connections[0].submitted == [('SELECT %(value)s',{'value':'US'})]
    assert configurations[0]['role']=='ANALYST'
    assert configurations[0]['session_parameters']['STATEMENT_TIMEOUT_IN_SECONDS']==30
    assert configurations[0]['session_parameters']['STATEMENT_QUEUED_TIMEOUT_IN_SECONDS']==10
    assert connections[0].executed[0]==('USE SECONDARY ROLES NONE',None)
    assert connections[0].fetch_sizes==[501]
    assert result['query_id']=='query-fixture' and result['truncated'] and result['row_count']==500


def test_statement_deadline_cancels_owned_query_and_closes_session():
    from adapters import SnowflakeAdapter
    from execution_context import RequestBudget, BudgetExceeded
    conn=Connection(); conn.running=True
    adapter=SnowflakeAdapter({'main':{}},lambda **kw:conn)
    ctx=context(budget=RequestBudget(statement_seconds=0))
    with pytest.raises(BudgetExceeded): asyncio.run(adapter.execute('SELECT 1',ctx))
    assert ('SELECT SYSTEM$CANCEL_QUERY(%s)',('query-fixture',)) in conn.executed
    assert conn.closed


def test_request_cancellation_cancels_query_without_reusing_session():
    from adapters import SnowflakeAdapter
    conn=Connection(); conn.running=True
    adapter=SnowflakeAdapter({'main':{}},lambda **kw:conn)
    async def run():
        task=asyncio.create_task(adapter.execute('SELECT 1',context()))
        while not conn.submitted: await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        for _ in range(100):
            if conn.closed: break
            await asyncio.sleep(.01)
        assert conn.closed
    asyncio.run(run())
    assert ('SELECT SYSTEM$CANCEL_QUERY(%s)',('query-fixture',)) in conn.executed


def test_concurrent_requests_keep_query_results_separate(tmp_path):
    from tests.test_governed_runtime import Adapter, Provider, INTENT
    from governed_runtime import GovernedRuntime
    from governed_store import GovernedStore
    class IndependentAdapter(Adapter):
        async def execute(self, sql, ctx, params=None):
            await asyncio.sleep(.01)
            data = await super().execute(sql,ctx,params)
            data['rows']=[[ctx.tenant]]
            return data
    async def run():
        store=GovernedStore(str(tmp_path/'s.db')); adapter=IndependentAdapter()
        a=GovernedRuntime(Provider([INTENT,'SELECT id FROM orders','acme']),adapter,store)
        b=GovernedRuntime(Provider([INTENT,'SELECT id FROM orders','beta']),adapter,store)
        first,second=await asyncio.gather(a.query('ids',context(),'same'),b.query('ids',context(tenant='beta'),'same'))
        assert first['data']['rows']==[['acme']] and second['data']['rows']==[['beta']]
        assert store.history(context(schema_version=first['schema_version'],semantic_version=first['semantic_version']),'same')[1]['content']=='acme'
        assert store.history(context(tenant='beta',schema_version=second['schema_version'],semantic_version=second['semantic_version']),'same')[1]['content']=='beta'
    asyncio.run(run())

@pytest.mark.parametrize('operation',['metadata','compile'])
def test_cancellation_covers_metadata_and_compilation(operation):
    from adapters import SnowflakeAdapter
    conn=Connection(); conn.running=True
    adapter=SnowflakeAdapter({'main':{}},lambda **kw:conn)
    async def run():
        task=asyncio.create_task(adapter.metadata(context()) if operation=='metadata' else adapter.compile('SELECT 1',context()))
        # Synchronous submission alone is not cancellable while waiting; require async query IDs.
        for _ in range(100):
            if conn.submitted: break
            await asyncio.sleep(.001)
        assert conn.submitted
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        for _ in range(100):
            if conn.closed: break
            await asyncio.sleep(.01)
        assert conn.closed
    asyncio.run(run())
    assert ('SELECT SYSTEM$CANCEL_QUERY(%s)',('query-fixture',)) in conn.executed
