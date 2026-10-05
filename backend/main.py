"""Governed REST API. Both query routes use one server-owned execution boundary."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from hashlib import sha256
import csv
import hmac
import io
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from config import get_settings
from models import QueryRequest, QueryResponse, ExportRequest, FeedbackRequest, DetailedFeedbackRequest
from tenant import TenantRegistry, TenantConfig
from execution_context import ExecutionContext, RequestBudget, AuthenticationError, authenticate
from adapters import SnowflakeAdapter, LegacyAdapter
from governed_store import GovernedStore, identity
from governed_runtime import GovernedRuntime
from llm_providers import get_provider
from tracing import init_tracing
from trusted_validator import validate, ValidationError

settings = get_settings()
_tenant_registry = TenantRegistry()
_store = GovernedStore()
_profiles = json.loads(Path(settings.connection_profiles_file).read_text()) if settings.connection_profiles_file else {
    'main': {'dialect': 'sqlite' if 'sqlite' in settings.database_url else 'postgres' if 'postgres' in settings.database_url else 'mysql',
             'database_url': settings.database_url}}
_adapters = {}
_provider = None


def _context(tenant):
    profile = _profiles.get(tenant.connection_profile)
    if not profile: raise AuthenticationError('Connection profile unavailable')
    dialect = profile.get('dialect', 'snowflake')
    if tenant.row_filters:
        raise AuthenticationError('Tenant requires database-enforced row policies; SQL filter injection is disabled')
    if dialect == 'snowflake':
        if not tenant.role or tenant.role not in profile.get('allowed_roles', []):
            raise AuthenticationError('Role is not authorized for this connection profile')
        database, schema = tenant.database or profile.get('database'), tenant.schema or profile.get('schema')
        if not database or not schema: raise AuthenticationError('Database scope is unavailable')
        allowed = frozenset(tenant.allowed_tables or ())
        if any(len(o.split('.')) != 3 for o in allowed):
            raise AuthenticationError('Snowflake objects must be fully qualified')
    else:
        database, schema = '', ''
        allowed = frozenset(t.lower() for t in (tenant.allowed_tables or settings.local_allowed_tables.split(',')) if t)
    if not allowed: raise AuthenticationError('No objects authorized; configure an explicit allowlist')
    return ExecutionContext(principal=tenant.principal or tenant.tenant_id, tenant=tenant.tenant_id,
        connection_profile=tenant.connection_profile, role=tenant.role, database=database, schema=schema,
        policy_version=tenant.policy_version, allowed_objects=allowed, dialect=dialect,
        budget=RequestBudget(max_rows=min(500, max(1, tenant.max_result_rows))))


def _adapter(context):
    key = context.connection_profile
    if key not in _adapters:
        _adapters[key] = SnowflakeAdapter(_profiles) if context.dialect == 'snowflake' else LegacyAdapter(_profiles[key]['database_url'])
    return _adapters[key]


def _runtime_for(context):
    global _provider
    if _provider is None: _provider = get_provider(settings.llm_provider, settings)
    return GovernedRuntime(_provider, _adapter(context), _store, settings)


@asynccontextmanager
async def lifespan(app):
    init_tracing()
    yield
    for adapter in _adapters.values():
        if isinstance(adapter, LegacyAdapter): await adapter.engine.dispose()

app = FastAPI(title='NexusSQL', version='3.0.0', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_url], allow_credentials=True,
                   allow_methods=['GET','POST','DELETE'], allow_headers=['Content-Type','X-API-Key','X-Admin-Key'])

_ADMIN_PREFIXES = ('/tenants', '/train', '/cache', '/evaluate', '/database', '/drift', '/semantics', '/examples')

@app.middleware('http')
async def authorize(request: Request, call_next):
    if request.method == 'OPTIONS' or request.url.path in ('/health', '/docs', '/openapi.json', '/redoc'):
        return await call_next(request)
    if request.url.path.startswith(_ADMIN_PREFIXES):
        key = request.headers.get('X-Admin-Key', '')
        if not settings.admin_api_key or not hmac.compare_digest(key, settings.admin_api_key):
            return JSONResponse(status_code=403, content={'detail':'Administrator authorization required'})
    else:
        try:
            tenant = authenticate(request.headers.get('X-API-Key'), _tenant_registry,
                allow_anonymous=settings.app_environment == 'local' and settings.allow_anonymous_local)
            request.state.context = _context(tenant)
        except AuthenticationError as exc:
            return JSONResponse(status_code=401, content={'detail':str(exc)})
    return await call_next(request)

@app.get('/health')
async def health():
    return {'status':'ok', 'database':'configured', 'tables':[], 'mlflow_experiment':'NexusSQL',
            'visualization_service_enabled':False}

@app.get('/schema')
async def schema(request: Request):
    context = request.state.context
    try:
        metadata = await _adapter(context).metadata(context)
        return {'schema_text':json.dumps(metadata, default=str)}
    except Exception: raise HTTPException(503, 'Authorized metadata is unavailable')

async def _run_query(body, request, progress=None):
    context = request.state.context
    coroutine = _runtime_for(context).query(body.query, context, body.conversation_id,
        force_agentic=body.force_agentic, clarification_id=body.clarification_id,
        clarification_answers=body.clarification_answers, progress=progress)
    if progress is not None:
        result = await coroutine  # SSE response owns its disconnect cancellation.
    else:
        task = asyncio.create_task(coroutine)
        stop_monitor = asyncio.Event()
        async def disconnected():
            while not stop_monitor.is_set():
                if await request.is_disconnected(): return
                await asyncio.sleep(.05)
        monitor = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait({task,monitor},return_when=asyncio.FIRST_COMPLETED)
            if task in done: result = await task
            else: raise HTTPException(499,'Request disconnected')
        finally:
            stop_monitor.set()
            task.cancel(); monitor.cancel()
            await asyncio.gather(task,monitor,return_exceptions=True)
    # Return exactly the same envelope from both endpoints.
    return QueryResponse(**result).model_dump()

@app.post('/query', response_model=QueryResponse)
async def query(body: QueryRequest, request: Request):
    return await _run_query(body, request)

@app.post('/query/stream')
async def query_stream(body: QueryRequest, request: Request):
    async def events():
        queue = asyncio.Queue(maxsize=16)
        async def progress(stage): await queue.put({'stage':stage})
        async def run():
            try:
                result = await _run_query(body, request, progress)
                await queue.put({'stage':'complete', 'result':result})
            except asyncio.CancelledError: raise
            except Exception:
                result = QueryResponse(sql='',data=None,response='The analysis could not be completed safely.',
                    success=False,status='failed',conversation_id=body.conversation_id).model_dump()
                await queue.put({'stage':'complete','result':result})
        task = asyncio.create_task(run())
        try:
            while True:
                event = await queue.get()
                yield 'data: ' + json.dumps(event, default=str) + '\n\n'
                if event['stage'] == 'complete': break
        finally:
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
    return StreamingResponse(events(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.delete('/conversations/{conversation_id}')
async def clear_conversation(conversation_id: str, request: Request):
    _store.clear_conversation(request.state.context, conversation_id)
    return {'status':'cleared'}

@app.post('/feedback')
async def feedback(body: FeedbackRequest, request: Request):
    if not _store.feedback(request.state.context, body.trace_id, body.value, body.comment):
        raise HTTPException(404, 'Trace unavailable')
    return {'status':'ok','cache_verified':False}

@app.post('/feedback/detailed')
async def detailed_feedback(body: DetailedFeedbackRequest, request: Request):
    if not _store.feedback(request.state.context,body.trace_id,
            {'accuracy':body.accuracy,'relevance':body.relevance,'sql_quality':body.sql_quality},body.comment):
        raise HTTPException(404,'Trace unavailable')
    return {'status':'ok'}

@app.post('/export/csv')
async def export(body: ExportRequest):
    output = io.StringIO(); writer = csv.writer(output)
    def safe(value):
        text = str(value) if value is not None else ''
        return "'" + text if text.startswith(('=','+','-','@')) else text
    writer.writerow([safe(v) for v in body.columns])
    writer.writerows([safe(v) for v in row] for row in body.rows)
    return StreamingResponse(io.BytesIO(output.getvalue().encode('utf-8-sig')),media_type='text/csv',
        headers={'Content-Disposition':'attachment; filename="query_results.csv"'})

@app.post('/tenants')
async def register(body: dict):
    try:
        tenant = TenantConfig(**{**body, 'allowed_tables':set(body.get('allowed_tables') or [])})
        _context(tenant)  # verify server profile, role, explicit allowlist, no filter injection
    except (TypeError, AuthenticationError): raise HTTPException(400,'Invalid tenant configuration')
    _tenant_registry.register(tenant)
    return {'status':'ok','tenant_id':tenant.tenant_id}

@app.get('/tenants')
async def tenants(): return {'tenants':_tenant_registry.list_tenants()}


def admin_context(tenant_id):
    tenant = _tenant_registry.get(tenant_id)
    if not tenant: raise HTTPException(404,'Tenant unavailable')
    try: return _context(tenant)
    except AuthenticationError: raise HTTPException(400,'Tenant configuration unavailable')

async def versioned_context(tenant_id):
    context = admin_context(tenant_id)
    schema = await _adapter(context).metadata(context)
    context = replace(context,schema_version=sha256(json.dumps(schema,sort_keys=True,default=str).encode()).hexdigest())
    definitions = _store.definitions(context)
    return replace(context,semantic_version=sha256(json.dumps(definitions,sort_keys=True).encode()).hexdigest()), schema

@app.post('/semantics')
async def semantics(body: dict):
    context, metadata = await versioned_context(body.get('tenant_id'))
    definition = body.get('definition', {})
    try:
        # Validate reviewed templates using representative parameter values.
        from semantic_contract import render_template
        test_intent = definition.get('review_intent', {})
        sql, _ = render_template(definition,test_intent,context.dialect)
        validate(sql,context,metadata,approved_joins=definition.get('joins', []),reviewed_template=True)
        _store.put_definition(context,definition,'administrator')
    except (ValueError, KeyError, ValidationError): raise HTTPException(400,'Definition requires a valid reviewed query contract and review intent')
    return {'status':'reviewed','definition_id':definition['id']}

@app.get('/semantics')
async def list_semantics(tenant_id: str): return {'definitions':_store.definitions(admin_context(tenant_id))}

@app.post('/examples/approve')
async def approve_example(body: dict):
    context, metadata = await versioned_context(body.get('tenant_id'))
    try: validate(body['sql'],context,metadata)
    except (KeyError,ValidationError): raise HTTPException(400,'Example is invalid')
    _store.approve_example(context,body['question'],body['sql'],'administrator')
    return {'status':'reviewed'}

@app.post('/train/{kind}')
async def train(kind: str, body: dict):
    context = admin_context(body.get('tenant_id'))
    if kind == 'sql': return await approve_example(body)
    if kind not in ('ddl','documentation'): raise HTTPException(404,'Training type unavailable')
    value = body.get('ddl' if kind == 'ddl' else 'text', '')
    if not value: raise HTTPException(400,'Training content required')
    key = sha256(value.encode()).hexdigest()
    _store._put(identity(context),'training',key,{'id':key,'type':kind,'text':value,'approved':False})
    return {'status':'stored_untrusted','id':key}

@app.get('/train/list')
async def list_training(tenant_id: str): return {'items':_store._list(identity(admin_context(tenant_id)),'training')}

@app.delete('/train/{item_id}')
async def delete_training(item_id: str, tenant_id: str):
    with _store.connection() as db:
        db.execute('DELETE FROM assets WHERE scope=? AND kind=? AND id=?',(identity(admin_context(tenant_id)),'training',item_id))
    return {'status':'ok'}

@app.get('/cache/stats')
async def cache_stats(tenant_id: str):
    context, _ = await versioned_context(tenant_id)
    return {'scope':context.scope,'sql_entries':len(_store._list(context.scope,'sql')),'result_cache_enabled':False}

@app.delete('/cache/results')
async def cache_results(): return {'status':'disabled','purged_entries':0}

@app.delete('/cache/all')
async def cache_all(tenant_id: str):
    context, _ = await versioned_context(tenant_id)
    return {'status':'ok','purged_entries':_store.admin_clear(context)}

@app.post('/database/test')
async def database_test(body: dict):
    # Credentials and destinations come exclusively from configured server profiles.
    context = admin_context(body.get('tenant_id'))
    try: metadata = await _adapter(context).metadata(context)
    except Exception: raise HTTPException(422,'Connection failed; consult internal service logs')
    return {'status':'ok','table_count':len(metadata),'tables_preview':list(metadata)[:10]}

@app.get('/database/status')
async def database_status(tenant_id: str): return await database_test({'tenant_id':tenant_id})

@app.post('/evaluate')
async def evaluate():
    return {'status':'offline_acceptance_required','message':'Use the versioned Snowflake holdout harness; live certification is pending.'}

@app.api_route('/drift/{action}',methods=['GET','POST'])
async def drift(action: str):
    return {'status':'not_configured','message':'Governed query stage metrics are recorded in MLflow. Live drift monitoring requires a staging baseline.'}
