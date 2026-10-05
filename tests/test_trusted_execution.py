"""Boundary regressions: never execute outside scope or silently rewrite meaning."""
import asyncio
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))


def context(**changes):
    from execution_context import ExecutionContext
    values = dict(principal='alice', tenant='acme', connection_profile='main', role='ANALYST',
                  database='ANALYTICS', schema='PUBLIC', policy_version='1',
                  allowed_objects=frozenset({'ANALYTICS.PUBLIC.ORDERS'}))
    values.update(changes)
    return ExecutionContext(**values)

SCHEMA = {'ANALYTICS.PUBLIC.ORDERS': {'columns': [
    {'name': 'ID', 'type': 'INT'}, {'name': 'AMOUNT', 'type': 'NUMBER'},
    {'name': 'BOOKED_AT', 'type': 'DATE'}, {'name': 'TENANT_ID', 'type': 'TEXT'}]}}

@pytest.mark.parametrize('sql', [
    'SELECT 1; SELECT 2', 'DELETE FROM orders',
    'WITH x AS (SELECT * FROM secret) SELECT * FROM x',
    'SELECT * FROM OTHER.PUBLIC.ORDERS', 'SELECT * FROM @stage',
    "SELECT SYSTEM$CANCEL_QUERY('id')", 'SELECT private_udf(amount) FROM orders',
    'SELECT * INTO other FROM orders', 'SELECT missing FROM orders',
])
def test_forbidden_or_unresolved_sql_is_blocked(sql):
    from trusted_validator import validate, ValidationError
    with pytest.raises(ValidationError):
        validate(sql, context(), SCHEMA)

@pytest.mark.parametrize('sql', [
    'SELECT id FROM orders',
    'WITH x AS (SELECT id FROM orders) SELECT id FROM x',
    'SELECT id FROM orders UNION ALL SELECT id FROM orders',
    'SELECT o.id, SUM(o.amount) OVER () AS total FROM orders o',
])
def test_readonly_ctes_sets_and_windows(sql):
    from trusted_validator import validate
    assert validate(sql, context(), SCHEMA)['readonly'] is True


def test_quoted_identifiers_cannot_alias_another_object():
    from trusted_validator import validate, ValidationError
    with pytest.raises(ValidationError):
        validate('SELECT * FROM "orders"', context(), SCHEMA)


def test_conversation_and_cache_scope_preserve_identity_and_literal_case():
    from execution_context import cache_key
    a = context()
    assert a.conversation_key('same') != context(principal='bob').conversation_key('same')
    assert a.conversation_key('same') != context(tenant='beta').conversation_key('same')
    assert cache_key(a, "SELECT 'US'") != cache_key(a, "SELECT 'us'")
    assert cache_key(a, 'SELECT 1') != cache_key(context(policy_version='2'), 'SELECT 1')


def test_metadata_is_filtered_before_retrieval():
    from execution_context import authorized_schema
    assert list(authorized_schema({**SCHEMA, 'ANALYTICS.PUBLIC.SECRET': {'columns': []}}, context())) == list(SCHEMA)


def test_request_submission_budget():
    from execution_context import RequestBudget, BudgetExceeded
    budget = RequestBudget(max_submissions=3)
    for _ in range(3): budget.submit()
    with pytest.raises(BudgetExceeded): budget.submit()


def test_result_bytes_and_rows_are_bounded():
    from adapters import bounded_result
    result = bounded_result(['v'], [('x'*100,)]*10, max_rows=5, max_bytes=300)
    assert result['truncated'] is True
    assert len(result['rows']) < 5
    assert result['row_count'] == len(result['rows'])


def test_invalid_key_never_falls_back_to_anonymous(tmp_path):
    from execution_context import authenticate, AuthenticationError
    from tenant import TenantRegistry
    registry = TenantRegistry(str(tmp_path/'tenants.db'))
    with pytest.raises(AuthenticationError):
        authenticate('invalid', registry, allow_anonymous=True)
    with pytest.raises(AuthenticationError):
        authenticate(None, registry, allow_anonymous=False)


def test_api_key_rotation_revokes_old_key(tmp_path):
    from tenant import TenantRegistry, TenantConfig
    registry = TenantRegistry(str(tmp_path/'tenants.db'))
    registry.register(TenantConfig('acme', 'old'))
    registry.register(TenantConfig('acme', 'new'))
    assert registry.get_by_api_key('old') is None
    assert registry.get_by_api_key('new').tenant_id == 'acme'


def test_bound_values_preserve_case_and_cannot_become_sql():
    from adapters import bind_values
    sql, values = bind_values("SELECT id FROM orders WHERE tenant_id='US' AND amount > 12", 'snowflake')
    assert 'US' not in sql and sorted(values.values(),key=str)==[12,'US']
    assert '%(p0)s' in sql and '%(p1)s' in sql

@pytest.mark.parametrize('function',['READ_CSV','READ_PARQUET'])
def test_parser_known_cross_dialect_function_is_not_an_approved_builtin(function):
    from trusted_validator import validate,ValidationError
    with pytest.raises(ValidationError):
        validate(f"SELECT {function}('https://example.com') FROM orders",context(),SCHEMA)
