"""Database adapters; Snowflake uses its official synchronous connector in bounded workers."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
import json
from time import monotonic, sleep
import threading
import contextvars

from execution_context import BudgetExceeded

_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix='nexussql')
# Bound queued work as well as running work. Capacity is released by the real worker.
_SLOTS = threading.BoundedSemaphore(8)

async def worker(fn, *args, timeout=90, **kwargs):
    started = monotonic()
    while not _SLOTS.acquire(blocking=False):
        if monotonic() - started >= timeout: raise BudgetExceeded('Worker capacity deadline exceeded')
        await asyncio.sleep(0.01)
    def run():
        try: return fn(*args, **kwargs)
        finally: _SLOTS.release()
    inherited_context = contextvars.copy_context()
    future = _POOL.submit(inherited_context.run, run)
    return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout=max(.01, timeout-(monotonic()-started)))


def _json_value(v):
    if isinstance(v, (date, datetime)): return v.isoformat()
    if isinstance(v, Decimal): return str(v)  # preserve numeric precision
    if isinstance(v, bytes): return v.hex()
    if isinstance(v, (str, int, float, bool, list, dict)) or v is None: return v
    return str(v)


def bounded_result(columns, raw_rows, *, max_rows=500, max_bytes=10*1024*1024, query_id=None, elapsed=0):
    result = dict(columns=list(columns), rows=[], row_count=0, truncated=False,
                  execution_time_ms=round(elapsed, 2), query_id=query_id,
                  fetched_at=datetime.now().astimezone().isoformat(), freshness='executed')
    size = len(json.dumps(result).encode())
    if size > max_bytes: raise BudgetExceeded('Result metadata exceeds response budget')
    for raw in raw_rows:
        if len(result['rows']) >= max_rows:
            result['truncated'] = True; break
        row = [_json_value(v) for v in raw]
        row_size = len(json.dumps(row, ensure_ascii=True).encode()) + 2
        if size + row_size > max_bytes:
            result['truncated'] = True; break
        result['rows'].append(row); size += row_size
    result['row_count'] = len(result['rows'])
    return result

def bind_values(sql, dialect):
    """Bind validated literal values without changing identifier or literal case."""
    import sqlglot
    from sqlglot import exp
    tree = sqlglot.parse_one(sql, read=dialect)
    values = {}
    def replace(node):
        if not isinstance(node, exp.Literal): return node
        name = 'p' + str(len(values))
        values[name] = node.to_py()
        return exp.Var(this='__NEXUS_BIND_' + name + '__')
    rendered = tree.transform(replace).sql(dialect=dialect)
    if dialect == 'snowflake': rendered = rendered.replace('%','%%')
    for name in values:
        rendered = rendered.replace('__NEXUS_BIND_' + name + '__', '%(' + name + ')s' if dialect == 'snowflake' else ':' + name)
    return rendered, values


class ExecutionFailure(Exception):
    def __init__(self, code='execution', repairable=False, query_id=None):
        super().__init__('Database request failed')
        self.code, self.repairable, self.query_id = code, repairable, query_id

class SnowflakeAdapter:
    def __init__(self, profiles, connect=None):
        self.profiles = profiles
        self._connect_fn = connect

    def _connect(self, context):
        profile = dict(self.profiles[context.connection_profile])
        # Roles and scope are server-created, never query request values.
        profile.pop('dialect', None)
        profile.pop('allowed_roles', None)
        profile.pop('database_url', None)
        import os
        for option, variable in profile.pop('credentials_env', {}).items():
            value = os.environ.get(variable)
            if not value: raise ExecutionFailure('configuration')
            profile[option] = value
        profile.update(role=context.role, database=context.database, schema=context.schema,
                       login_timeout=min(10, max(1, int(context.budget.remaining()))), network_timeout=10,
                       session_parameters={
                           'STATEMENT_TIMEOUT_IN_SECONDS': context.budget.statement_seconds,
                           'STATEMENT_QUEUED_TIMEOUT_IN_SECONDS': context.budget.queue_seconds,
                           'QUERY_TAG': context.request_id,
                           'ABORT_DETACHED_QUERY': True,
                           'USE_CACHED_RESULT': False,
                       })
        connect = self._connect_fn
        if connect is None:
            import snowflake.connector
            connect = snowflake.connector.connect
        conn = connect(**profile)
        # Avoid secondary roles broadening the effective request role.
        try:
            with conn.cursor() as cur: cur.execute('USE SECONDARY ROLES NONE')
            return conn
        except BaseException:
            conn.close()
            raise

    async def metadata(self, context):
        stop = threading.Event()
        try:
            return await worker(self._metadata, context, stop, timeout=context.budget.remaining())
        except (asyncio.CancelledError, asyncio.TimeoutError):
            stop.set()
            raise

    def _metadata(self, context, stop):
        conn = self._connect(context)
        query_id = None
        started = monotonic()
        try:
            with conn.cursor() as cur:
                database = '"' + context.database.replace('"', '""') + '"'
                cur.execute_async(f'SELECT TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,DATA_TYPE,IS_NULLABLE FROM {database}.INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = %s', (context.schema,))
                query_id = cur.sfqid
                import mlflow
                span = mlflow.get_current_active_span()
                if span: span.set_attributes({"snowflake.query_id": query_id})
                self._wait(conn, query_id, context, stop, started)
                cur.get_results_from_sfqid(query_id)
                schema = {}
                while True:
                    rows = cur.fetchmany(500)
                    if not rows: break
                    if stop.is_set(): raise BudgetExceeded("Request cancelled")
                    context.budget.remaining()
                    for catalog, namespace, table, column, typ, nullable in rows:
                        name = '.'.join([catalog, namespace, table])
                        if name not in context.allowed_objects: continue
                        info = schema.setdefault(name, {'columns': [], 'foreign_keys': [], 'primary_key': [], 'row_count': 'unknown'})
                        info['columns'].append({'name': column, 'type': typ, 'nullable': nullable == 'YES'})
                return schema
        except BaseException:
            if query_id:
                try: self.cancel(conn, query_id)
                except Exception: pass
            raise
        finally: conn.close()

    async def compile(self, sql, context, params=None):
        return await self._run(sql, context, params, compile_only=True)

    async def execute(self, sql, context, params=None):
        context.budget.submit()
        return await self._run(sql, context, params)

    async def _run(self, sql, context, params, compile_only=False):
        stop = threading.Event()
        try:
            return await worker(self._execute, sql, context, params, stop, compile_only,
                                timeout=context.budget.remaining())
        except (asyncio.CancelledError, asyncio.TimeoutError):
            stop.set()
            raise

    @staticmethod
    def _wait(conn, query_id, context, stop, started):
        while True:
            if stop.is_set() or monotonic() - started >= context.budget.statement_seconds:
                raise BudgetExceeded('Statement deadline exceeded')
            context.budget.remaining()
            status = conn.get_query_status_throw_if_error(query_id)
            if not conn.is_still_running(status): break
            sleep(.05)

    @staticmethod
    def cancel(conn, query_id):
        # ID obtained only from this request's cursor; bound, never concatenated.
        with conn.cursor() as cur:
            cur.execute('SELECT SYSTEM$CANCEL_QUERY(%s)', (query_id,))

    def _execute(self, sql, context, params, stop, compile_only):
        conn = self._connect(context)
        query_id = None
        start = monotonic()
        try:
            with conn.cursor() as cur:
                if stop.is_set(): raise BudgetExceeded('Request cancelled')
                cur.execute_async(('EXPLAIN USING TEXT ' + sql) if compile_only else sql, params)
                query_id = cur.sfqid
                import mlflow
                span = mlflow.get_current_active_span()
                if span: span.set_attributes({"snowflake.query_id": query_id})
                self._wait(conn, query_id, context, stop, start)
                cur.get_results_from_sfqid(query_id)
                if compile_only: return {'compiled': True, 'query_id':query_id}
                columns = [d[0] for d in cur.description]
                raw = cur.fetchmany(context.budget.max_rows + 1)
                if stop.is_set(): raise BudgetExceeded("Request cancelled")
                context.budget.remaining()
                return bounded_result(columns, raw, max_rows=context.budget.max_rows,
                    max_bytes=context.budget.max_bytes - 65536, query_id=query_id,
                    elapsed=(monotonic()-start)*1000)
        except BaseException as exc:
            if query_id:
                try: self.cancel(conn, query_id)
                except Exception: pass
            if isinstance(exc, (BudgetExceeded, asyncio.CancelledError)):
                exc.query_id = query_id
                raise
            state = str(getattr(exc, 'sqlstate', ''))
            code = 'authorization' if state == '42501' else 'compilation' if state.startswith('42') else 'execution'
            raise ExecutionFailure(code, repairable=code == 'compilation', query_id=query_id) from exc
        finally: conn.close()

class LegacyAdapter:
    """Uncertified legacy adapter with the same scope boundary; no row filter injection."""
    def __init__(self, database_url):
        from sqlalchemy.ext.asyncio import create_async_engine
        self.engine = create_async_engine(database_url)

    async def metadata(self, context):
        from sqlalchemy import inspect
        def inspect_schema(conn):
            inspector = inspect(conn)
            tables = {}
            for name in inspector.get_table_names():
                if name.lower() not in context.allowed_objects: continue
                tables[name.lower()] = {'columns': [dict(name=c['name'].lower(), type=str(c['type']), nullable=c.get('nullable', True)) for c in inspector.get_columns(name)],
                    'foreign_keys': [], 'primary_key': [], 'row_count': 'unknown'}
            return tables
        async with self.engine.connect() as conn:
            return await conn.run_sync(inspect_schema)

    async def compile(self, sql, context, params=None):
        # AST validation is available; DB compilation is Snowflake-specific.
        return {'compiled': False, 'limitation': 'Legacy adapter is not Snowflake certified'}

    async def execute(self, sql, context, params=None):
        from sqlalchemy import text
        context.budget.submit()
        start = monotonic()
        try:
            async with self.engine.connect() as conn:
                result = await asyncio.wait_for(conn.stream(text(sql), params or {}),
                    min(context.budget.statement_seconds, context.budget.remaining()))
                rows = await result.fetchmany(context.budget.max_rows + 1)
                return bounded_result(list(result.keys()), rows, max_rows=context.budget.max_rows,
                    max_bytes=context.budget.max_bytes - 65536, elapsed=(monotonic()-start)*1000)
        except asyncio.TimeoutError: raise BudgetExceeded('Statement deadline exceeded')
        except Exception as exc:
            if isinstance(exc, BudgetExceeded): raise
            from sqlalchemy.exc import ProgrammingError, OperationalError
            repairable = isinstance(exc, (ProgrammingError, OperationalError)) and any(
                word in str(exc).lower() for word in ('syntax error', 'no such column', 'no such table'))
            raise ExecutionFailure('compilation' if repairable else 'execution', repairable) from exc
