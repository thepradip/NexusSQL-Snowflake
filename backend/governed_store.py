"""Persistent scoped assets. Feedback and administrator approval are separate records."""
from hashlib import sha256
import json
import secrets
import sqlite3
import time
from execution_context import cache_key


def identity(context):
    return context.conversation_key('assets:' + context.policy_version)

class GovernedStore:
    def __init__(self, path='.governed.db'):
        self.path = path
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS assets (
                    scope TEXT, kind TEXT, id TEXT, payload TEXT, created REAL,
                    PRIMARY KEY(scope,kind,id));
                CREATE TABLE IF NOT EXISTS feedback (
                    scope TEXT, trace_id TEXT, payload TEXT, created REAL);
            ''')

    def connection(self): return sqlite3.connect(self.path, timeout=10)

    def _put(self, scope, kind, key, payload):
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO assets VALUES (?,?,?,?,?)',
                       (scope, kind, key, json.dumps(payload), time.time()))

    def _get(self, scope, kind, key):
        with self.connection() as db:
            row = db.execute('SELECT payload FROM assets WHERE scope=? AND kind=? AND id=?', (scope, kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def _list(self, scope, kind):
        with self.connection() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM assets WHERE scope=? AND kind=? ORDER BY id', (scope, kind))]

    def history(self, context, conversation):
        history = self._get(context.conversation_key(conversation), 'history', 'messages') or []
        return [{'role':m['role'], 'content':m['content']} for m in history
                if m.get('schema_version') == context.schema_version and m.get('semantic_version') == context.semantic_version]

    def append(self, context, conversation, question, answer):
        # Read-modify-write in one transaction prevents concurrent lost updates.
        scope = context.conversation_key(conversation)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM assets WHERE scope=? AND kind=? AND id=?', (scope, 'history', 'messages')).fetchone()
            history = json.loads(row[0]) if row else []
            history.extend([{'role': 'user', 'content': question, 'schema_version':context.schema_version, 'semantic_version':context.semantic_version},
                            {'role': 'assistant', 'content': answer, 'schema_version':context.schema_version, 'semantic_version':context.semantic_version}])
            db.execute('INSERT OR REPLACE INTO assets VALUES (?,?,?,?,?)', (scope, 'history', 'messages', json.dumps(history[-20:]), time.time()))

    def clear_conversation(self, context, conversation):
        with self.connection() as db:
            db.execute('DELETE FROM assets WHERE scope=?', (context.conversation_key(conversation),))

    def clarify(self, context, conversation, payload):
        key = secrets.token_urlsafe(24)
        self._put(context.conversation_key(conversation), 'clarification', key,
                  {**payload, 'expires': time.time()+3600, 'policy_version': context.policy_version,
                   'schema_version': context.schema_version, 'semantic_version': context.semantic_version})
        return key

    def get_clarification(self, context, conversation, key):
        value = self._get(context.conversation_key(conversation), 'clarification', key)
        if not value or value['expires'] < time.time(): return None
        if any(value.get(k) != getattr(context, k) for k in ('policy_version', 'schema_version', 'semantic_version')): return None
        return value

    def consume_clarification(self, context, conversation, key):
        with self.connection() as db:
            db.execute('DELETE FROM assets WHERE scope=? AND kind=? AND id=?', (context.conversation_key(conversation), 'clarification', key))

    def definitions(self, context): return self._list(identity(context), 'definition')

    def put_definition(self, context, definition, reviewer):
        required = ('id', 'name', 'formula', 'grain', 'dimensions', 'joins', 'date_column',
                    'timezone', 'fiscal_calendar', 'objects', 'sql_template', 'parameters')
        if any(k not in definition for k in required): raise ValueError('Definition is incomplete')
        if not set(definition['objects']).issubset(context.allowed_objects):
            raise ValueError('Definition references unauthorized objects')
        self._put(identity(context), 'definition', definition['id'],
                  {**definition, 'reviewer': reviewer, 'reviewed_at': time.time()})

    def sql(self, context, question): return self._get(context.scope, 'sql', cache_key(context, question))

    def put_sql(self, context, question, sql):
        self._put(context.scope, 'sql', cache_key(context, question), {'sql': sql, 'stored_at': time.time()})

    def invalidate_sql(self, context, question):
        with self.connection() as db:
            db.execute('DELETE FROM assets WHERE scope=? AND kind=? AND id=?', (context.scope, 'sql', cache_key(context, question)))

    def examples(self, context): return self._list(context.scope, 'example')

    def approve_example(self, context, question, sql, reviewer):
        self._put(context.scope, 'example', cache_key(context, question),
                  {'question': question, 'sql': sql, 'reviewer': reviewer})

    def record_trace(self, context, trace_id):
        self._put(identity(context), 'trace', trace_id, {'owner': context.principal})

    def feedback(self, context, trace_id, value, comment=None):
        if not self._get(identity(context), 'trace', trace_id): return False
        with self.connection() as db:
            db.execute('INSERT INTO feedback VALUES (?,?,?,?)', (identity(context), trace_id,
                json.dumps({'value': value, 'comment': comment, 'principal': context.principal}), time.time()))
        return True

    def admin_clear(self, context, kind='sql'):
        with self.connection() as db:
            cur = db.execute('DELETE FROM assets WHERE scope=? AND kind=?', (context.scope, kind))
            return cur.rowcount
