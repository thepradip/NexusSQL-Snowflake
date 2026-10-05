import importlib
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('AZURE_OPENAI_ENDPOINT', 'https://fixture.openai.azure.com')
    monkeypatch.setenv('AZURE_OPENAI_API_KEY', 'fixture')
    monkeypatch.setenv('ALLOW_ANONYMOUS_LOCAL', 'false')
    monkeypatch.setenv('ADMIN_API_KEY', 'admin-fixture')
    monkeypatch.setenv('DATABASE_URL', 'sqlite+aiosqlite:///:memory:')
    for name in ('config', 'main'): sys.modules.pop(name, None)
    mod = importlib.import_module('main')
    from fastapi.testclient import TestClient
    return mod, TestClient(mod.app)

@pytest.mark.parametrize('path,method', [('/query','post'), ('/query/stream','post'),
    ('/schema','get'), ('/feedback','post'), ('/conversations/c','delete')])
def test_data_endpoints_require_authentication(api, path, method):
    _, client = api
    assert getattr(client, method)(path).status_code == 401

@pytest.mark.parametrize('path,method', [('/tenants','get'),('/tenants','post'),
    ('/train/sql','post'),('/train/documentation','post'),('/train/ddl','post'),
    ('/train/list','get'),('/train/id','delete'),('/cache/all','delete'),
    ('/cache/results','delete'),('/cache/stats','get'),('/evaluate','post'),
    ('/database/test','post'),('/database/status','get'),('/drift/configure','post')])
def test_administration_requires_separate_admin_key(api, path, method):
    _, client = api
    assert getattr(client, method)(path).status_code == 403


def test_query_and_stream_have_same_contract_and_unique_missing_conversations(api):
    mod, client = api
    from tenant import TenantConfig
    mod._tenant_registry.register(TenantConfig('acme', 'fixture', allowed_tables={'orders'}, principal='alice'))
    class Runtime:
        async def query(self, question, context, conversation_id, **kwargs):
            import uuid
            return dict(sql='SELECT 1', data=None, response='No rows matched.', success=True,
                        status='answered', conversation_id=conversation_id or str(uuid.uuid4()))
    mod._runtime_for = lambda context: Runtime()
    headers = {'X-API-Key':'fixture'}
    a = client.post('/query', headers=headers, json={'query':'ids'}).json()
    b = client.post('/query', headers=headers, json={'query':'ids'}).json()
    assert a['conversation_id'] != b['conversation_id']
    stream = client.post('/query/stream', headers=headers, json={'query':'ids','conversation_id':a['conversation_id']})
    import json
    events = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith('data: ')]
    assert events[-1]['result'] == a


def test_invalid_credentials_are_rejected_even_in_local_mode(api):
    mod, client = api
    mod.settings.allow_anonymous_local = True
    mod.settings.app_environment = 'local'
    assert client.post('/query',headers={'X-API-Key':'wrong'},json={'query':'ids'}).status_code == 401


def test_plain_http_disconnect_cancels_runtime(api):
    import asyncio
    import types
    mod,_=api
    stopped=asyncio.Event()
    class Runtime:
        async def query(self,*args,**kwargs):
            try: await asyncio.sleep(10)
            finally: stopped.set()
    mod._runtime_for=lambda context:Runtime()
    class Request:
        state=types.SimpleNamespace(context=None)
        async def is_disconnected(self): return True
    async def run():
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as error:
            await mod._run_query(mod.QueryRequest(query='ids'),Request())
        assert error.value.status_code==499 and stopped.is_set()
    asyncio.run(run())
