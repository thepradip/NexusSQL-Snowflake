"""Real runtime with deterministic model and adapter doubles at external boundaries."""
import asyncio
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from tests.test_trusted_execution import context, SCHEMA

class Provider:
    supports_tool_calling = True
    name = 'fixture'
    def __init__(self, answers): self.answers = list(answers); self.calls = []
    def complete(self, messages, max_tokens=2000):
        self.calls.append(messages)
        return self.answers.pop(0)

class Adapter:
    def __init__(self, failures=()): self.calls = []; self.failures = list(failures); self.value = 12
    async def metadata(self, ctx): return SCHEMA
    async def compile(self, sql, ctx, params=None): return {'compiled': True}
    async def execute(self, sql, ctx, params=None):
        ctx.budget.submit(); self.calls.append(sql)
        if self.failures: raise self.failures.pop(0)
        return dict(columns=['TOTAL'], rows=[[self.value]], row_count=1, truncated=False,
                    execution_time_ms=1, query_id='fixture-id', freshness='executed', fetched_at='now')

INTENT = json.dumps(dict(metrics=[], dimensions=[], objects=list(SCHEMA), expressions=['ID'], filters=[], time_boundaries={}, grain='detail', unresolved=[]))
SUM_INTENT = json.dumps(dict(metrics=[], dimensions=[], objects=list(SCHEMA), expressions=['SUM(amount) AS total'], filters=[], time_boundaries={}, grain='total', unresolved=[]))

def runtime(tmp_path, answers, adapter=None):
    from governed_runtime import GovernedRuntime
    from governed_store import GovernedStore
    return GovernedRuntime(Provider(answers), adapter or Adapter(), GovernedStore(str(tmp_path/'store.db')))


def test_narration_comes_from_new_execution_on_cache_hit(tmp_path):
    async def run():
        r = runtime(tmp_path, [SUM_INTENT, 'SELECT SUM(amount) AS total FROM orders', '12', SUM_INTENT, '19'])
        first = await r.query('sum amounts', context(), 'c')
        r.adapter.value = 19
        second = await r.query('sum amounts', context(), 'other')
        assert first['status'] == second['status'] == 'answered'
        assert first['response'] == '12' and second['response'] == '19'
        assert second['data']['rows'] == [[19]] and len(r.adapter.calls) == 2
    asyncio.run(run())


def test_missing_business_definition_clarifies_without_execution(tmp_path):
    intent = json.dumps(dict(metrics=['revenue'], dimensions=[], filters=[], time_boundaries={}, grain='total', unresolved=[]))
    r = runtime(tmp_path, [intent])
    out = asyncio.run(r.query('what is revenue?', context(), 'c'))
    assert out['status'] == 'needs_clarification' and not out['success']
    assert out['clarification']['id'] and r.adapter.calls == []


def test_clarification_cannot_be_consumed_by_another_principal(tmp_path):
    from governed_store import GovernedStore
    store = GovernedStore(str(tmp_path/'s.db'))
    cid = store.clarify(context(), 'c', {'question': 'revenue?', 'choices': []})
    assert store.get_clarification(context(principal='bob'), 'c', cid) is None
    assert store.get_clarification(context(), 'different', cid) is None
    assert store.get_clarification(context(), 'c', cid)['question'] == 'revenue?'


def test_authorization_and_timeout_failures_are_never_repaired(tmp_path):
    from adapters import ExecutionFailure
    from execution_context import BudgetExceeded
    for failure in (ExecutionFailure('authorization'), BudgetExceeded('timeout')):
        r = runtime(tmp_path, [INTENT, 'SELECT id FROM orders'], Adapter([failure]))
        out = asyncio.run(r.query('ids', context(), 'c'))
        assert out['status'] in ('blocked', 'failed') and not out['success']
        assert len(r.adapter.calls) == 1 and len(r.provider.calls) == 2


def test_repair_is_revalidated_and_cannot_write(tmp_path):
    from adapters import ExecutionFailure
    r = runtime(tmp_path, [INTENT, 'SELECT id FROM orders', 'DELETE FROM orders'],
                Adapter([ExecutionFailure('compilation', True)]))
    out = asyncio.run(r.query('ids', context(), 'c'))
    assert out['status'] == 'blocked' and len(r.adapter.calls) == 1


def test_fabricated_agent_completion_never_answers(tmp_path):
    r = runtime(tmp_path, [INTENT, 'The answer is 42'])
    out = asyncio.run(r.query('ids', context(), 'c', force_agentic=True))
    assert not out['success'] and r.adapter.calls == []


def test_reviewed_metric_cannot_change_formula(tmp_path):
    intent = json.dumps(dict(metrics=['booked revenue'], dimensions=[], filters=[], time_boundaries={}, grain='total', unresolved=[]))
    r = runtime(tmp_path, [intent, 'SELECT AVG(amount) FROM orders'])
    r.store.put_definition(context(), {'id': 'booked', 'name': 'booked revenue', 'synonyms': [],
        'formula': 'SUM(AMOUNT)', 'grain': 'total', 'dimensions': [], 'joins': [],
        'date_column': 'BOOKED_AT', 'timezone': 'UTC', 'fiscal_calendar': 'calendar',
        'objects': list(SCHEMA), 'sql_template': 'SELECT SUM(amount) FROM orders', 'parameters': []}, 'admin')
    out = asyncio.run(r.query('booked revenue', context(), 'c'))
    assert out['status'] == 'blocked' and r.adapter.calls == []


def test_feedback_is_owned_and_never_approves_example(tmp_path):
    from governed_store import GovernedStore
    s = GovernedStore(str(tmp_path/'s.db'))
    s.record_trace(context(), 'trace')
    assert s.feedback(context(principal='bob'), 'trace', True) is False
    assert s.feedback(context(), 'trace', True) is True
    assert s.examples(context()) == []


def test_agent_tools_execute_once_and_ignore_fabricated_final_sql(tmp_path):
    class ToolProvider(Provider):
        def __init__(self): super().__init__([SUM_INTENT,'12']); self.turn=0
        def complete_with_tools(self,messages,tools,max_tokens=2000):
            self.turn += 1
            if self.turn == 1:
                return {'role':'assistant','content':''},[{'id':'1','name':'execute_sql','arguments':{'sql':'SELECT SUM(amount) AS total FROM orders'}}]
            return {'role':'assistant','content':''},[{'id':'2','name':'final_answer','arguments':{'sql':'SELECT 999','answer':'999'}}]
    r=runtime(tmp_path,[]); r.provider=ToolProvider()
    out=asyncio.run(r.query('sum amounts',context(),'c',force_agentic=True))
    assert out['status']=='answered' and out['agent_mode']=='react'
    assert out['sql']=='SELECT SUM(amount) AS total FROM orders'
    assert out['response']=='12' and len(r.adapter.calls)==1


def test_agent_final_without_execution_is_blocked(tmp_path):
    class ToolProvider(Provider):
        def __init__(self): super().__init__([INTENT])
        def complete_with_tools(self,*args,**kwargs):
            return {'role':'assistant','content':''},[{'id':'1','name':'final_answer','arguments':{'answer':'42'}}]
    r=runtime(tmp_path,[]);r.provider=ToolProvider()
    out=asyncio.run(r.query('ids',context(),'c',force_agentic=True))
    assert out['status']=='blocked' and r.adapter.calls==[]


def test_generic_query_cannot_drop_population_or_dates(tmp_path):
    intent=json.dumps(dict(metrics=[],dimensions=[],filters=[{'field':'TENANT_ID','value':'acme'}],
        time_boundaries={'start':'2025-01-01','end':'2025-02-01'},date_column='BOOKED_AT',
        expressions=['SUM(AMOUNT)'],grain='total',unresolved=[]))
    r=runtime(tmp_path,[intent,'SELECT SUM(amount) FROM orders'])
    out=asyncio.run(r.query('sum acme amounts in January',context(),'c'))
    assert out['status']=='blocked' and r.adapter.calls==[]


def test_generic_preserves_exact_projection_filters_and_range(tmp_path):
    intent=json.dumps(dict(metrics=[],dimensions=[],objects=list(SCHEMA),expressions=['SUM(AMOUNT) AS total'],
        filters=[{'field':'TENANT_ID','value':'acme'}],time_boundaries={'start':'2025-01-01','end':'2025-02-01'},
        date_column='BOOKED_AT',grain='total',unresolved=[]))
    sql="SELECT SUM(AMOUNT) AS total FROM orders WHERE TENANT_ID='acme' AND BOOKED_AT >= '2025-01-01' AND BOOKED_AT < '2025-02-01'"
    r=runtime(tmp_path,[intent,sql,'12'])
    out=asyncio.run(r.query('sum January amounts',context(),'c'))
    assert out['status']=='answered'
    assert 'acme' not in r.adapter.calls[0]  # submitted via bound values


def test_stale_schema_and_definitions_do_not_reuse_history(tmp_path):
    from governed_store import GovernedStore
    store=GovernedStore(str(tmp_path/'s.db'))
    a=context(schema_version='v1',semantic_version='d1')
    store.append(a,'c','old question','private old meaning')
    assert store.history(a,'c')[1]['content']=='private old meaning'
    assert store.history(context(schema_version='v2',semantic_version='d1'),'c')==[]
    assert store.history(context(schema_version='v1',semantic_version='d2'),'c')==[]

@pytest.mark.parametrize('sql', ['SELECT ID FROM orders', 'SELECT ID FROM payments LIMIT 5',
                                'SELECT ID FROM orders SAMPLE (10) LIMIT 5',
                                "SELECT ID FROM orders AT (TIMESTAMP => '2025-01-01'::TIMESTAMP) LIMIT 5"])
def test_generic_source_and_requested_limit_cannot_change(tmp_path,sql):
    from tests.test_trusted_execution import SCHEMA
    class ExpandedAdapter(Adapter):
        async def metadata(self,context): return {**SCHEMA,'ANALYTICS.PUBLIC.PAYMENTS':SCHEMA['ANALYTICS.PUBLIC.ORDERS']}
    intent=json.dumps(dict(metrics=[],dimensions=[],expressions=['ID'],objects=['ANALYTICS.PUBLIC.ORDERS'],
        limit=5,filters=[],time_boundaries={},grain='detail',unresolved=[]))
    r=runtime(tmp_path,[intent,sql],ExpandedAdapter())
    out=asyncio.run(r.query('five order ids',context(allowed_objects=frozenset({'ANALYTICS.PUBLIC.ORDERS','ANALYTICS.PUBLIC.PAYMENTS'})),'c'))
    assert out['status']=='blocked' and r.adapter.calls==[]
